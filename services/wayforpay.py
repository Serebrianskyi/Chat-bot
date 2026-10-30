"""WayForPay client: signatures, CREATE_INVOICE, CHECK_STATUS. Phase 2A.

Every call is ``POST https://api.wayforpay.com/api``, differing only by ``transactionType``.

Asking for money is **Purchase** and only Purchase (``secure.wayforpay.com/pay?behavior=offline``),
because it is the one method that can carry a monthly schedule. ``CREATE_INVOICE`` was removed on
2026-10-29: it cannot renew anything, and two ways of asking for money where only one renews is
how a member ends up on a link that quietly never repeats.

``CHECK_STATUS`` confirms a payment the callback did not reach us about. ``CHARGE`` renews from a
stored ``recToken`` and is unreachable on this merchant — tokenisation is not available (support,
2026-10-29) — kept because it is correct code should that ever change.

Signature rules, all HMAC-MD5 over ``;``-joined UTF-8 keyed with the merchant secret:

* Purchase / CHARGE request:
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

#: Where a **Purchase** is created. ``behavior=offline`` makes it an API call rather than a form
#: submission: it answers ``{"url": ...}`` with the payment page to send the member to, which is
#: the only shape usable from a bot — a Telegram button cannot POST a form.
#:
#: Purchase exists here for one reason: it is the **only** method that accepts the recurring
#: parameters. ``CREATE_INVOICE``'s documented parameter list has none of them (wiki 608996852),
#: so a gateway-side monthly schedule is unreachable from the invoice flow. Confirmed with
#: WayForPay support on 2026-10-29, who also said card tokenisation is not available on this
#: merchant — which leaves this as the only route to automatic renewal.
PURCHASE_URL = "https://secure.wayforpay.com/pay?behavior=offline"

#: Managing an existing schedule: STATUS, SUSPEND, RESUME, REMOVE, EDIT (wiki 852496).
#: Authenticated with ``merchantPassword``, a 32-character value obtained from WayForPay —
#: **not** the secret key and not its MD5. Verified against the live API on 2026-10-29: the key
#: itself is refused for length, and its MD5 answers "Can`t authorize merchant with Password".
REGULAR_API_URL = "https://api.wayforpay.com/regularApi"

#: The monthly schedule a Purchase can carry. ``monthly`` is the only period this club bills in.
REGULAR_MODE_MONTHLY = "monthly"

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


def _text(value: Any) -> str | None:
    """A gateway field as a string, with blank and absent both becoming None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def charge_key(body: dict[str, Any]) -> str | None:
    """What identifies one charge inside one order, or None if nothing in the body does.

    ``authCode`` is the bank's authorisation code and is unique per approved charge, so it is the
    natural answer. A refusal carries none — there was no authorisation — so for those the varying
    fields are digested instead: the same decline reported twice has the same digest, while next
    month's decline has a different ``processingDate``.

    This exists because ``payments.order_reference`` is UNIQUE and a monthly schedule may report
    every charge under the original order number. Without a per-charge identity, month two reads
    as a duplicate of month one and nobody's access is extended (rule 5).
    """
    auth_code = _text(body.get("authCode"))
    if auth_code:
        return auth_code[:64]
    varying = ";".join(
        str(body.get(name, "") or "")
        for name in ("transactionStatus", "reasonCode", "processingDate", "createdDate", "amount")
    )
    if not varying.strip(";"):
        return None
    return hashlib.sha256(varying.encode()).hexdigest()[:32]


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
class Purchase:
    """A payment page, and the schedule attached to it."""

    order_reference: str
    page_url: str
    regular_mode: str | None
    date_next: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RegularPayment:
    """What ``regularApi`` STATUS says about a schedule."""

    order_reference: str
    status: str | None
    mode: str | None
    amount: Decimal | None
    currency: str | None
    next_payment_date: str | None
    last_payed_date: str | None
    last_payed_status: str | None
    reason_code: str | None
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
    #: Identifies **this charge**, not this order. A monthly schedule can report every charge
    #: under the order number of the Purchase that created it, and that is the one field which
    #: separates next month's money from a repeat notification about last month's.
    charge_key: str | None = None
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
        merchant_password: str | None = None,
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
        # Only `regularApi` needs this, and it is a credential WayForPay issues separately from
        # the secret key. None means a schedule can be created but not cancelled through the API —
        # the caller must say so rather than pretend the cancellation happened.
        self._merchant_password = merchant_password
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

    @property
    def service_url(self) -> str | None:
        """Where the gateway posts results, or None when nothing can receive them."""
        return self._service_url

    @property
    def can_manage_schedules(self) -> bool:
        """Whether ``regularApi`` can be called at all — i.e. a merchantPassword is configured."""
        return bool(self._merchant_password)

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

    async def purchase(
        self,
        *,
        order_reference: str,
        order_date: int,
        amount: Decimal,
        currency: str,
        product_name: str,
        recurring: bool = False,
        regular_amount: Decimal | None = None,
        date_next: str | None = None,
        order_timeout: int | None = None,
        client_email: str | None = None,
        language: str = "UA",
    ) -> Purchase:
        """Create a payment page and return its URL. The only way this bot asks for money.

        ``recurring=True`` attaches a **monthly schedule**: one payment by the member settles this
        period and registers a recurring charge on their card. Every month after, WayForPay charges
        them and posts the result to ``serviceUrl`` — there is no second link and nothing for the
        member to do again.

        ``recurring=False`` is a single payment and nothing more, for the cases where a schedule
        would be wrong: a member who already has one (a second would charge them twice a month)
        and an admin-issued one-off. ``regularMode`` is simply omitted — the gateway treats an
        absent one as "no recurring".

        This replaced ``CREATE_INVOICE`` outright on 2026-10-29. That method cannot carry the
        recurring parameters at all (its documented table has none of them, wiki 608996852), so
        keeping it would have meant two ways of asking for money where only one can renew.
        Everything the invoice call offered — ``orderTimeout``, ``clientEmail``, ``serviceUrl`` —
        Purchase takes too.

        ``regularOn=1`` ties the schedule to the payment rather than offering it as a checkbox,
        and ``regularBehavior=preset`` stops the member editing the amount or the period on the
        page. ``dateNext`` is when the **next** charge falls, so it must be a future date — the
        period being paid for now is covered by ``amount``.

        **A serviceUrl is mandatory here.** A schedule whose charges nothing is listening for is
        money taken with no access granted, every month, silently. Polling cannot substitute: the
        poller only asks about orders it already knows, and a gateway-side charge creates one it
        has never heard of. So this raises rather than quietly creating an unmonitored schedule.

        The signature covers exactly the fields CREATE_INVOICE's does, in the same order (wiki
        852102) — the recurring fields are **not** signed. Verified against the live endpoint on
        2026-10-29.
        """
        if recurring and not self._service_url:
            msg = (
                "refusing to create a recurring Purchase with no serviceUrl: the gateway would "
                "charge the card monthly and nothing would hear about it"
            )
            raise WayForPayError(msg)
        if recurring and (regular_amount is None or date_next is None):
            msg = "a recurring Purchase needs both regular_amount and date_next"
            raise WayForPayError(msg)

        payload: dict[str, Any] = {
            "merchantAccount": self._merchant_account,
            "merchantDomainName": self._merchant_domain,
            # AUTO is the only documented value for Purchase, and it is what lets 3DS run —
            # unlike CHARGE, there *is* a cardholder on this page.
            "merchantTransactionSecureType": "AUTO",
            "apiVersion": 1,
            "language": language,
            "orderReference": order_reference,
            "orderDate": order_date,
            "amount": format_amount(amount),
            "currency": currency,
            "productName": [product_name],
            "productCount": [1],
            "productPrice": [format_amount(amount)],
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
        if recurring:
            payload.update(
                {
                    "regularMode": REGULAR_MODE_MONTHLY,
                    "regularOn": 1,
                    "regularBehavior": "preset",
                    "regularAmount": format_amount(regular_amount),  # type: ignore[arg-type]
                    "dateNext": date_next,
                }
            )
        if order_timeout is not None:
            payload["orderTimeout"] = order_timeout
        if client_email:
            payload["clientEmail"] = client_email
        if self._service_url:
            # Where WayForPay posts the result. Not part of the signature. Mandatory for a
            # schedule (checked above) and worth sending anyway: it confirms a payment the moment
            # it happens instead of up to two minutes later.
            payload["serviceUrl"] = self._service_url

        data = await self._post(payload, url=PURCHASE_URL)
        page_url = data.get("url")
        if not page_url:
            msg = (
                f"Purchase returned no url for {order_reference}: "
                f"reasonCode={data.get('reasonCode')} reason={data.get('reason')!r}"
            )
            raise WayForPayError(msg)

        return Purchase(
            order_reference=order_reference,
            page_url=str(page_url),
            regular_mode=REGULAR_MODE_MONTHLY if recurring else None,
            date_next=date_next if recurring else None,
            raw=data,
        )

    # --- managing a schedule once it exists ------------------------------------------------

    async def regular_request(self, request_type: str, **fields: Any) -> dict[str, Any]:
        """One ``regularApi`` call. Raises if no merchantPassword is configured.

        Not signed: this API authenticates with the merchant password instead, which is why it
        must never be logged. Raising on a missing password is deliberate — the caller has to
        handle "cannot cancel" rather than report a cancellation that did not happen.
        """
        if not self._merchant_password:
            msg = (
                f"{request_type} needs a WayForPay merchantPassword, which is not configured; "
                f"the schedule must be stopped by hand in the dashboard"
            )
            raise WayForPayError(msg)
        payload = {
            "requestType": request_type,
            "merchantAccount": self._merchant_account,
            "merchantPassword": self._merchant_password,
            **fields,
        }
        return await self._post(payload, url=REGULAR_API_URL)

    async def regular_status(self, *, order_reference: str) -> RegularPayment:
        """What the gateway thinks the schedule is doing.

        Statuses: Active, Suspended, Created, Removed, Confirmed, Completed (wiki 852526). The
        response describes the schedule, not its individual charges — only the last one.
        """
        data = await self.regular_request("STATUS", orderReference=order_reference)
        return RegularPayment(
            order_reference=order_reference,
            status=_text(data.get("status")),
            mode=_text(data.get("mode")),
            amount=parse_amount(
                data.get("amount"),
                order_reference=order_reference,
                gateway_status=_text(data.get("status")),
            ),
            currency=_text(data.get("currency")),
            next_payment_date=_text(data.get("nextPaymentDate")),
            last_payed_date=_text(data.get("lastPayedDate")),
            last_payed_status=_text(data.get("lastPayedStatus")),
            reason_code=_text(data.get("reasonCode")),
            raw=data,
        )

    async def regular_remove(self, *, order_reference: str) -> dict[str, Any]:
        """Stop a schedule for good. There is no undo — a new Purchase starts a new one."""
        return await self.regular_request("REMOVE", orderReference=order_reference)

    async def regular_suspend(self, *, order_reference: str) -> dict[str, Any]:
        """Pause a schedule, keeping it resumable."""
        return await self.regular_request("SUSPEND", orderReference=order_reference)

    async def regular_resume(self, *, order_reference: str) -> dict[str, Any]:
        """Restart a suspended schedule."""
        return await self.regular_request("RESUME", orderReference=order_reference)

    async def regular_edit_amount(
        self, *, order_reference: str, amount: Decimal, currency: str
    ) -> dict[str, Any]:
        """Change what the schedule charges.

        Needed because ``regularAmount`` is fixed when the schedule is created, while this club's
        prices are computed per invoice — a discount that ends, or a promo that runs out, changes
        what the member should pay without touching the gateway's copy of the figure.
        """
        return await self.regular_request(
            "EDIT",
            orderReference=order_reference,
            amount=format_amount(amount),
            currency=currency,
        )

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
            charge_key=charge_key(data),
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

    async def _post(self, payload: dict[str, Any], *, url: str | None = None) -> dict[str, Any]:
        """One request. ``url`` overrides the API endpoint for Purchase and regularApi.

        The payload is never logged: it carries the signature, which is the secret's output, and
        for regularApi the merchant password itself.
        """
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            response = await client.post(url or self._api_url, json=payload)
        if response.status_code != 200:
            msg = f"WayForPay returned HTTP {response.status_code}"
            raise WayForPayError(msg)
        try:
            return response.json()
        except ValueError as exc:
            msg = "WayForPay returned a body that is not JSON"
            raise WayForPayError(msg) from exc
