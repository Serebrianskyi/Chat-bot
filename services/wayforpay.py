"""WayForPay client: signatures, CREATE_INVOICE, CHECK_STATUS. Phase 2A.

Every call is ``POST https://api.wayforpay.com/api``, differing only by ``transactionType``.

Only two transaction types are implemented. ``CHARGE`` (renewal from a stored ``recToken``) is
deliberately left out of this slice — see docs/phase-2a-scope.md — but ``recToken`` is captured
whenever WayForPay returns it, so adding renewals later needs no migration.

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
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._merchant_account = merchant_account
        self._merchant_domain = merchant_domain
        self._secret_key = secret_key
        self._api_url = api_url
        self._timeout = timeout
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

        data = await self._post(payload)

        invoice_url = data.get("invoiceUrl")
        if not invoice_url:
            msg = (
                f"CREATE_INVOICE returned no invoiceUrl for {order_reference}: "
                f"reasonCode={data.get('reasonCode')} reason={data.get('reason')!r}"
            )
            raise WayForPayError(msg)

        return Invoice(order_reference=order_reference, invoice_url=invoice_url, raw=data)

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

        gateway_status = data.get("transactionStatus")
        return TransactionStatus(
            order_reference=order_reference,
            status=map_status(gateway_status),
            gateway_status=gateway_status,
            amount=parse_amount(
                data.get("amount"),
                order_reference=order_reference,
                gateway_status=gateway_status,
            ),
            currency=data.get("currency"),
            reason_code=str(data["reasonCode"]) if data.get("reasonCode") is not None else None,
            rec_token=data.get("recToken"),
            raw=data,
        )

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
