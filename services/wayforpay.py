"""WayForPay client: signatures, CREATE_INVOICE, CHECK_STATUS. Phase 2A.

Every call is ``POST https://api.wayforpay.com/api``, differing only by ``transactionType``.

Three transaction types: ``CREATE_INVOICE`` (first payment), ``CHECK_STATUS`` (confirmation) and
``CHARGE`` (renewal from a stored ``recToken``). ``CHARGE`` answers synchronously, so a renewal
needs no polling; the other two are a request and a later question.

Signature rules, all HMAC-MD5 over ``;``-joined UTF-8 keyed with the merchant secret:

* CREATE_INVOICE / CHARGE request:
  ``merchantAccount;merchantDomainName;orderReference;orderDate;amount;currency;
  productName[0..n];productCount[0..n];productPrice[0..n]``
  — all names, then all counts, then all prices.
* CHECK_STATUS request: ``merchantAccount;orderReference``
* Any response: ``merchantAccount;orderReference;amount;currency;authCode;cardPan;
  transactionStatus;reasonCode``

``amount`` must be byte-identical between the signature and the payload. ``"300"`` against
``"300.00"`` is the single most common cause of a rejected signature, which is why
``format_amount`` exists and is the only way this module renders money.

Sources: wiki.wayforpay.com /en/view/608996852 (create invoice), /en/view/852117 (check status),
/en/view/852102 (response signature field list).
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from db.models import PaymentStatus

log = logging.getLogger(__name__)

API_URL = "https://api.wayforpay.com/api"

#: Field order of the response signature. Shared by CHECK_STATUS, CHARGE and the (unused) callback.
RESPONSE_SIGNATURE_FIELDS = (
    "merchantAccount",
    "orderReference",
    "amount",
    "currency",
    "authCode",
    "cardPan",
    "transactionStatus",
    "reasonCode",
)

#: WayForPay ``transactionStatus`` -> our payment status. See the design doc's status table.
STATUS_MAP = {
    "Approved": PaymentStatus.COMPLETE,
    "Declined": PaymentStatus.DENIED,
    "InProcessing": PaymentStatus.PENDING,
    "Pending": PaymentStatus.PENDING,
    "Expired": PaymentStatus.CANCELED,
    "Refunded": PaymentStatus.REFUNDED,
    "Voided": PaymentStatus.REVERSED,
    "RefundInProcessing": PaymentStatus.PENDING,
}


class WayForPayError(RuntimeError):
    """A call failed, or a response could not be trusted."""


class SignatureMismatch(WayForPayError):
    """The response signature did not match. Treat the body as forged and do not act on it."""


def format_amount(amount: Decimal) -> str:
    """Render money the one way this module ever renders it: two decimal places.

    Used for both the signature and the payload so they cannot disagree.
    """
    return str(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def parse_amount(value: Any, *, order_reference: str, gateway_status: str | None) -> Decimal | None:
    """Read a gateway ``amount`` into a Decimal, or None when there is no number in it.

    WayForPay sends an empty ``amount`` for an order that never carried money — an invoice that
    expired unpaid is the case seen in production — and the response is signed with that blank in
    place, so the body is genuine and must not be rejected. Returning None rather than raising
    keeps the poller alive: ``apply_payment_result`` already refuses to grant access without an
    amount, so nothing loosens on the money path.

    The offending value is logged because the crash this replaces happened before the raw body was
    persisted, leaving nothing to inspect afterwards. Only the amount and the status are logged —
    never the body, which carries the signature (standing gate S5).
    """
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        log.warning(
            "Unparseable amount %r for order %s (transactionStatus=%r); treating it as absent",
            value,
            order_reference,
            gateway_status,
        )
        return None


def sign(secret_key: str, fields: list[str]) -> str:
    """HMAC-MD5 of ``;``-joined fields. MD5 is WayForPay's choice, not ours."""
    message = ";".join(fields).encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), message, hashlib.md5).hexdigest()


def refine_status(
    mapped: PaymentStatus, *, gateway_status: str | None, amount: Decimal | None, card_pan: str
) -> PaymentStatus:
    """Demote a ``Declined`` that carries no payment detail back to "not paid yet".

    Production, 2026-10-09: a single poll run checked 77 invoices created one minute earlier and
    WayForPay answered ``Declined`` with a blank ``amount`` for every one of them. Taken at face
    value that is 77 refused cards; in reality nobody had opened the links yet.

    The consequence was severe, because ``DENIED`` is terminal: the order left the poll query for
    good roughly a minute after being issued, so a member who then paid — the link stays valid for
    ``orderTimeout``, two hours — was never asked about again. Their money was taken and no invite
    was ever sent. This is the shape of "paid but never joined the channel".

    A real refusal and an untouched invoice are told apart by what the gateway sends alongside the
    status: a card that was actually refused comes back with the ``amount`` it was refused for and
    a masked ``cardPan``. Neither is present when nothing has been attempted, and a signed
    response is not required to carry them (``verify_response`` signs absent fields as empty).

    So a detail-less ``Declined`` becomes ``PENDING_PAYMENT``, which keeps the order pollable and
    still lets the invoice-timeout rule write it off as abandoned two hours later. A refusal that
    does carry detail stays ``DENIED``.
    """
    if mapped is not PaymentStatus.DENIED:
        return mapped
    if amount is not None or card_pan.strip():
        return mapped
    log.info(
        "Treating %r with no amount and no cardPan as not-yet-paid rather than refused",
        gateway_status,
    )
    return PaymentStatus.PENDING_PAYMENT


#: What the reply must say for WayForPay to stop retrying.
CALLBACK_ACCEPT = "accept"

#: **The documentation contradicts itself here**, and this is the one line that decides whether
#: WayForPay accepts our acknowledgement or retries the callback for ever:
#:
#: * ``/en/view/608996852`` (create invoice — our flow) says the signed string is
#:   ``orderReference;status;time``
#: * ``/en/view/852194`` (charge) says it is ``orderReference;status;merchantSecretKey``
#:
#: ``time`` is chosen. It is the create-invoice page, which is the flow we use; ``time`` is a
#: field of the reply while ``merchantSecretKey`` is already the HMAC key, so signing it as a
#: *field* as well would be redundant; and 852194 reads like a copy-paste of the request
#: signature's wording.
#:
#: **If callbacks arrive repeatedly in production, this is the first thing to flip.** Both forms
#: are implemented below so swapping is a one-word change, not a rewrite.
CALLBACK_SIGNATURE_USES_TIME = True


def callback_reply(secret_key: str, *, order_reference: str, now: int) -> dict[str, Any]:
    """The body to answer a ``serviceUrl`` notification with.

    WayForPay repeats a callback until it is acknowledged, so this is not optional: an
    unacknowledged payment arrives again and again. ``status`` is always ``accept`` — it
    acknowledges *receipt*, not agreement, and there is nothing to decline.

    Source: wiki.wayforpay.com/en/view/608996852 (the serviceUrl section), read 2026-10-28.
    See ``CALLBACK_SIGNATURE_USES_TIME`` — the documented field list is not consistent between
    pages, and that constant is where the choice lives.
    """
    third = str(now) if CALLBACK_SIGNATURE_USES_TIME else secret_key
    return {
        "orderReference": order_reference,
        "status": CALLBACK_ACCEPT,
        "time": now,
        "signature": sign(secret_key, [order_reference, CALLBACK_ACCEPT, third]),
    }


def map_status(transaction_status: str | None) -> PaymentStatus:
    """Translate a gateway status, defaulting to ERROR for anything unrecognised.

    Unknown maps to ERROR rather than DENIED on purpose: ERROR is non-terminal, so the poller
    tries again instead of writing off a payment we simply failed to understand.
    """
    if transaction_status is None:
        return PaymentStatus.ERROR
    mapped = STATUS_MAP.get(transaction_status)
    if mapped is None:
        log.warning("Unrecognised WayForPay transactionStatus %r", transaction_status)
        return PaymentStatus.ERROR
    return mapped


@dataclass
class Invoice:
    order_reference: str
    invoice_url: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class TransactionStatus:
    order_reference: str
    status: PaymentStatus
    gateway_status: str | None
    amount: Decimal | None
    currency: str | None
    reason_code: str | None
    rec_token: str | None
    raw: dict[str, Any] = field(default_factory=dict)


class WayForPayClient:
    """Thin, explicit client. No retries here — the polling job owns retry policy."""

    def __init__(
        self,
        *,
        merchant_account: str,
        merchant_domain: str,
        secret_key: str,
        api_url: str = API_URL,
        timeout: float = 30.0,
        service_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._merchant_account = merchant_account
        self._merchant_domain = merchant_domain
        self._secret_key = secret_key
        self._api_url = api_url
        self._timeout = timeout
        # None while the bot polls: without a public HTTPS endpoint there is nowhere to post to,
        # and sending an unreachable serviceUrl would have WayForPay retrying into the void.
        self._service_url = service_url
        # A seam for tests: httpx.MockTransport lets the request-building and response-parsing
        # paths be exercised without a network, which is worth having on the money paths.
        self._transport = transport

    # --- signature construction, kept public so tests can assert the field order ----------

    def invoice_signature_fields(
        self,
        *,
        order_reference: str,
        order_date: int,
        amount: Decimal,
        currency: str,
        product_name: str,
        product_count: int,
    ) -> list[str]:
        return [
            self._merchant_account,
            self._merchant_domain,
            order_reference,
            str(order_date),
            format_amount(amount),
            currency,
            product_name,
            str(product_count),
            format_amount(amount),
        ]

    def status_signature_fields(self, *, order_reference: str) -> list[str]:
        return [self._merchant_account, order_reference]

    def verify_response(self, payload: dict[str, Any]) -> None:
        """Raise unless the response signature matches.

        Missing fields are signed as empty strings, which is how WayForPay treats absent values;
        a declined transaction has no ``authCode`` or ``cardPan``, and its signature still covers
        their positions.
        """
        received = payload.get("merchantSignature")
        if not received:
            msg = "response carried no merchantSignature"
            raise SignatureMismatch(msg)

        fields = [str(payload.get(name, "") or "") for name in RESPONSE_SIGNATURE_FIELDS]
        expected = sign(self._secret_key, fields)
        if not hmac.compare_digest(expected, str(received)):
            msg = (
                f"response signature mismatch for order "
                f"{payload.get('orderReference')!r}: refusing to trust the body"
            )
            raise SignatureMismatch(msg)

    # --- calls ---------------------------------------------------------------------------

    async def create_invoice(
        self,
        *,
        order_reference: str,
        order_date: int,
        amount: Decimal,
        currency: str,
        product_name: str,
        order_timeout: int | None = None,
        client_email: str | None = None,
    ) -> Invoice:
        """Create an invoice and return its payment URL.

        The response to CREATE_INVOICE carries no transaction yet, so it is not signature-verified
        — there is nothing financial to trust in it. ``invoiceUrl`` is only a link; the money is
        confirmed later by CHECK_STATUS, which *is* verified.
        """
        payload: dict[str, Any] = {
            "transactionType": "CREATE_INVOICE",
            "merchantAccount": self._merchant_account,
            "merchantDomainName": self._merchant_domain,
            "apiVersion": 1,
            "orderReference": order_reference,
            "orderDate": order_date,
            "amount": format_amount(amount),
            "currency": currency,
            "productName": [product_name],
            "productPrice": [format_amount(amount)],
            "productCount": [1],
            "merchantSignature": sign(
                self._secret_key,
                self.invoice_signature_fields(
                    order_reference=order_reference,
                    order_date=order_date,
                    amount=amount,
                    currency=currency,
                    product_name=product_name,
                    product_count=1,
                ),
            ),
        }
        if order_timeout is not None:
            payload["orderTimeout"] = order_timeout
        if client_email:
            payload["clientEmail"] = client_email
        if self._service_url:
            # Where WayForPay posts the result — and the **only** place a `recToken` is ever
            # delivered for an invoice payment, which is what makes automatic renewal possible
            # at all. Not part of the signature.
            payload["serviceUrl"] = self._service_url

        data = await self._post(payload)

        invoice_url = data.get("invoiceUrl")
        if not invoice_url:
            msg = (
                f"CREATE_INVOICE returned no invoiceUrl for {order_reference}: "
                f"reasonCode={data.get('reasonCode')} reason={data.get('reason')!r}"
            )
            raise WayForPayError(msg)

        return Invoice(order_reference=order_reference, invoice_url=invoice_url, raw=data)

    async def charge(
        self,
        *,
        order_reference: str,
        order_date: int,
        amount: Decimal,
        currency: str,
        product_name: str,
        rec_token: str,
    ) -> TransactionStatus:
        """Take money from a stored card. The renewal path.

        ``CHARGE`` is **synchronous**: the body that comes back carries the real
        ``transactionStatus``, so unlike ``CREATE_INVOICE`` there is nothing to poll for and the
        answer is financial — which is why it is signature-verified before anything is believed.

        The request signature is the same field list as ``CREATE_INVOICE`` (see
        docs/telegram-bot-payments-design.md §2); ``recToken`` is not part of it.

        No WayForPay-side subscription is involved. The club's own ``expires_at`` is the
        schedule and this executes one charge against it, which is what makes per-user pricing
        possible — the amount is this member's amount, not a figure fixed at enrolment.

        Source: wiki.wayforpay.com/en/view/852194 (charge, host2host), read 2026-10-20.
        """
        payload: dict[str, Any] = {
            "transactionType": "CHARGE",
            "merchantAccount": self._merchant_account,
            "merchantDomainName": self._merchant_domain,
            "apiVersion": 1,
            # Both mandatory for CHARGE, unlike CREATE_INVOICE — omitting them is rejected.
            # SALE takes the money now; AUTH would only hold it and need a later Settle.
            "merchantTransactionType": "SALE",
            # NON3DS because nobody is present: a renewal runs from a cron job, and a 3-D Secure
            # challenge needs a cardholder to answer it. AUTO lets WayForPay decide and may
            # therefore pick a challenge that can never be completed. If an acquirer insists on
            # 3DS for stored-card charges this is the field to revisit.
            "merchantTransactionSecureType": "NON3DS",
            "orderReference": order_reference,
            "orderDate": order_date,
            "amount": format_amount(amount),
            "currency": currency,
            "productName": [product_name],
            "productPrice": [format_amount(amount)],
            "productCount": [1],
            "recToken": rec_token,
            "merchantSignature": sign(
                self._secret_key,
                self.invoice_signature_fields(
                    order_reference=order_reference,
                    order_date=order_date,
                    amount=amount,
                    currency=currency,
                    product_name=product_name,
                    product_count=1,
                ),
            ),
        }

        data = await self._post(payload)
        self.verify_response(data)
        return self._read_transaction(order_reference, data)

    async def check_status(self, *, order_reference: str) -> TransactionStatus:
        """Ask whether an order was actually paid. The authoritative answer."""
        payload = {
            "transactionType": "CHECK_STATUS",
            "merchantAccount": self._merchant_account,
            "orderReference": order_reference,
            "apiVersion": 1,
            "merchantSignature": sign(
                self._secret_key, self.status_signature_fields(order_reference=order_reference)
            ),
        }
        data = await self._post(payload)
        self.verify_response(data)
        return self._read_transaction(order_reference, data)

    def _read_transaction(self, order_reference: str, data: dict[str, Any]) -> TransactionStatus:
        """Turn a verified response body into a TransactionStatus.

        Shared by ``CHECK_STATUS`` and ``CHARGE`` on purpose: both answer the same question and
        both must read a blank amount, an unrecognised status and a detail-less ``Declined`` the
        same way. Two copies would drift, and the thing they would drift on is money.
        """
        gateway_status = data.get("transactionStatus")
        amount = parse_amount(
            data.get("amount"),
            order_reference=order_reference,
            gateway_status=gateway_status,
        )
        return TransactionStatus(
            order_reference=order_reference,
            status=refine_status(
                map_status(gateway_status),
                gateway_status=gateway_status,
                amount=amount,
                card_pan=str(data.get("cardPan") or ""),
            ),
            gateway_status=gateway_status,
            amount=amount,
            currency=data.get("currency"),
            reason_code=str(data["reasonCode"]) if data.get("reasonCode") is not None else None,
            rec_token=data.get("recToken"),
            raw=data,
        )

    def read_callback(self, payload: dict[str, Any]) -> TransactionStatus:
        """Verify a ``serviceUrl`` notification and read it. Raises if it cannot be trusted.

        The notification's own signature covers the same fields as any other response
        (``RESPONSE_SIGNATURE_FIELDS``), so ``verify_response`` works on it unchanged — which is
        why this is three lines rather than a parallel implementation.

        **This is where a `recToken` finally arrives.** `CHECK_STATUS` does not carry one for an
        invoice payment (proven in production, 2026-10-28); the callback does. See the payments
        design doc §1a.
        """
        self.verify_response(payload)
        order_reference = str(payload.get("orderReference") or "")
        return self._read_transaction(order_reference, payload)

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One request.

        The payload is never logged: it carries the signature, which is the secret's output.
        """
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.post(self._api_url, json=payload)
        if response.status_code != 200:
            msg = f"WayForPay returned HTTP {response.status_code}"
            raise WayForPayError(msg)
        try:
            return response.json()
        except ValueError as exc:
            msg = "WayForPay returned a body that is not JSON"
            raise WayForPayError(msg) from exc
