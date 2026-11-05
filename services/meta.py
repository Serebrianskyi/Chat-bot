"""Meta Conversions API: telling Meta Ads which clicks became members. Marketing, not money.

**Why the server and not a pixel.** A Meta Pixel is JavaScript in a browser, and a member who taps
an ad lands inside the Telegram app — there is no page to put a script on. The Conversions API is
the same reporting done server to server, which is the only shape available here.

Two events, matching the two moments worth optimising an ad campaign for:

* ``Lead`` — somebody ran ``/start`` after tapping an ad. Sent once per person.
* ``Purchase`` — they paid, with the real amount and currency.

**Three rules this module keeps.**

1. **Personal data is hashed, never sent raw** (SHA-256 over the trimmed, lowercased value), which
   is Meta's requirement for customer information parameters. We hold no email or phone, so in
   practice the only identifier is the hashed Telegram id — enough for Meta to deduplicate a
   person across events without us handing over anything about them.
2. **Nothing here may affect the member.** Every call is wrapped and timeboxed: a Meta outage, a
   revoked token or a change of API version must not delay a payment link or break ``/start``.
   Failures are logged and dropped.
3. **The access token is never logged**, like every other secret (standing gate S5) — including in
   the error path, where the temptation is to print the request that failed.

``event_id`` is sent so Meta can deduplicate: the same ``/start`` reported twice, or a payment
confirmed by both the callback and the poller, must count once. It is derived from what the event
is about — the member for a lead, the order for a purchase — not from the clock.

Source: developers.facebook.com/docs/marketing-api/conversions-api, read 2026-11-05.
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

log = logging.getLogger(__name__)

#: Pinned, like every other third-party contract here: a floating version is a silent change to
#: the payload shape on somebody else's release schedule.
API_VERSION = "v21.0"

GRAPH_URL = "https://graph.facebook.com"

#: Meta's own names. Not ours to invent: the Events Manager matches on these exact strings.
EVENT_LEAD = "Lead"
EVENT_PURCHASE = "Purchase"

#: Where the event happened, from Meta's fixed vocabulary. ``chat`` is the one that describes a
#: conversation in a messaging app.
ACTION_SOURCE = "chat"

#: A short timeout on purpose. This runs inside a member's ``/start`` and inside the payment
#: confirmation path, so a slow Meta must cost a second, not a visible pause.
TIMEOUT = 5.0


def hashed(value: str | None) -> str | None:
    """SHA-256 of a trimmed, lowercased value — Meta's format for customer information.

    Returns None for a blank, so an absent field is omitted from the payload rather than sent as
    the hash of an empty string, which would match every other blank in Meta's system.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return hashlib.sha256(text.encode()).hexdigest()


def click_id(fbclid: str | None, *, clicked_at_ms: int | None = None) -> str | None:
    """Meta's ``fbc`` parameter, built from the ``fbclid`` an ad appended to the deep link.

    Format: ``fb.1.<click time in milliseconds>.<fbclid>``. The timestamp is **when the click
    happened**, which is why it is stored with the id rather than taken from the clock at send
    time — a Purchase reported a month later must still carry the original click.
    """
    if not fbclid:
        return None
    stamp = clicked_at_ms if clicked_at_ms is not None else int(time.time() * 1000)
    return f"fb.1.{stamp}.{fbclid}"


@dataclass
class MetaConversions:
    """Thin client. Built by ``build_meta_client`` from settings, injected like the gateway."""

    pixel_id: str
    access_token: str
    api_version: str = API_VERSION
    base_url: str = GRAPH_URL
    timeout: float = TIMEOUT
    #: Meta's **Test Events** code, from Events Manager → Test Events. With it set, every event
    #: appears in that tab within seconds and is **kept out of** the dataset's real figures — which
    #: is the only honest way to verify this works: the alternative is polluting a live campaign
    #: and waiting for aggregation to see whether anything arrived. Leave unset in production.
    test_event_code: str | None = None
    transport: httpx.AsyncBaseTransport | None = None

    def payload(
        self,
        *,
        event_name: str,
        telegram_id: int,
        event_time: int,
        event_id: str,
        fbc: str | None = None,
        value: Decimal | None = None,
        currency: str | None = None,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict:
        """The request body, built in one place so it can be asserted in a test.

        Only fields we actually have are included. Meta ignores unknown keys but counts empty
        ones against match quality, and an empty hash is worse than a missing field: it is a
        value that collides with every other blank.
        """
        user_data: dict[str, object] = {"external_id": [hashed(str(telegram_id))]}
        if fbc:
            user_data["fbc"] = fbc
        if (digest := hashed(email)) is not None:
            user_data["em"] = [digest]
        if (digest := hashed(phone)) is not None:
            user_data["ph"] = [digest]

        event: dict[str, object] = {
            "event_name": event_name,
            "event_time": event_time,
            "event_id": event_id,
            "action_source": ACTION_SOURCE,
            "user_data": user_data,
        }
        if value is not None:
            event["custom_data"] = {
                "currency": (currency or "").upper(),
                # Meta wants a number, and money is a Decimal here: rendered rather than cast,
                # so 8.40 cannot arrive as 8.4000000000000004.
                "value": float(round(value, 2)),
            }
        body: dict[str, object] = {"data": [event], "access_token": self.access_token}
        if self.test_event_code:
            body["test_event_code"] = self.test_event_code
        return body

    async def send(self, **kwargs) -> bool:
        """Report one event. True when Meta accepted it. Never raises.

        Returning a bool rather than the response body is deliberate: no caller has any decision
        to make on the contents, and a marketing report must not become a reason to fail a
        payment.
        """
        body = self.payload(**kwargs)
        url = f"{self.base_url}/{self.api_version}/{self.pixel_id}/events"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(url, json=body)
        except Exception:
            # The payload carries the access token, so the request is never logged — only what
            # the event was about (standing gate S5).
            log.warning(
                "Meta CAPI unreachable for %s (%s); the event is lost and nothing else changes",
                kwargs.get("event_name"),
                kwargs.get("event_id"),
            )
            return False

        if response.status_code != 200:
            # Meta's error bodies name the field at fault and do not echo the token.
            log.warning(
                "Meta CAPI refused %s (%s): HTTP %s %s",
                kwargs.get("event_name"),
                kwargs.get("event_id"),
                response.status_code,
                response.text[:300],
            )
            return False

        log.info(
            "Meta CAPI accepted %s (%s)%s",
            kwargs.get("event_name"),
            kwargs.get("event_id"),
            " [TEST EVENT — not counted in the dataset]" if self.test_event_code else "",
        )
        return True


# --- the two moments worth reporting -----------------------------------------------------------


async def report_lead(
    session,
    client: "MetaConversions | None",
    *,
    telegram_id: int,
    fbclid: str | None,
    now,
) -> bool:
    """Report ``Lead`` for somebody who has just run ``/start``. Once per person.

    Takes the click id from the deep link when there is one and stores it, so a Purchase weeks
    later can still be attributed to the ad. Without a click id the event is still worth sending:
    Meta matches on the hashed external id, which is how a member who came back through a second
    ad is not counted as two people.
    """
    from db.models import User

    user = await session.get(User, telegram_id)
    if user is None:
        return False

    if fbclid and not user.fb_click_id:
        user.fb_click_id = fbclid[:255]
        user.fb_clicked_at = now
        log.info("Member %s arrived from a Meta ad", telegram_id)

    if client is None or user.fb_lead_sent_at is not None:
        return False

    sent = await client.send(
        event_name=EVENT_LEAD,
        telegram_id=telegram_id,
        event_time=int(now.timestamp()),
        # One lead per member, so the id is the member. A repeated /start cannot inflate the
        # campaign's numbers even if this is somehow called twice.
        event_id=f"lead-{telegram_id}",
        fbc=_stored_click(user),
    )
    if sent:
        user.fb_lead_sent_at = now
    return sent


async def report_purchase(
    session,
    client: "MetaConversions | None",
    *,
    telegram_id: int,
    order_reference: str,
    amount: Decimal,
    currency: str,
    now,
) -> bool:
    """Report ``Purchase`` with the real sum. One event per order.

    Every renewal is a purchase in Meta's sense, which is what makes a campaign's return
    measurable beyond the first payment. ``event_id`` is the order reference, so the callback and
    the poller confirming the same money cannot count it twice.
    """
    from db.models import User

    if client is None:
        return False
    user = await session.get(User, telegram_id)

    return await client.send(
        event_name=EVENT_PURCHASE,
        telegram_id=telegram_id,
        event_time=int(now.timestamp()),
        event_id=f"purchase-{order_reference}",
        fbc=_stored_click(user) if user is not None else None,
        value=amount,
        currency=currency,
    )


def _stored_click(user) -> str | None:
    """The member's ``fbc``, rebuilt from the click this bot recorded."""
    if not user.fb_click_id:
        return None
    stamp = int(user.fb_clicked_at.timestamp() * 1000) if user.fb_clicked_at else None
    return click_id(user.fb_click_id, clicked_at_ms=stamp)
