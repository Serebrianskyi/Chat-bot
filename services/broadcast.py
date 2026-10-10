"""Sending one message to many members, and posting into the channel.

Two jobs, both admin-initiated:

* ``send_broadcast`` — the admin's text to every member of one group, optionally followed by a
  second message carrying a live payment link. Written for the people who started the bot and
  never paid, where the text alone is no use without a way back to paying.
* ``post_to_channel`` — the bot publishes an admin's text in the private channel, so the club
  can talk to members as well as the other way round.

**Telegram's rate limit is the hard constraint** (standing rule 10). Broadcasts stay at or under
``MESSAGES_PER_SECOND`` and honour ``RetryAfter`` without skipping or duplicating anybody: a
member whose send is rate-limited is waited for and retried, never dropped and never sent twice.

Invoices are issued one per recipient, because the amount is per user — a discount makes one
member's link different from another's. An invoice that is still open and fresh is reused rather
than replaced, so a broadcast does not litter the ``payments`` table with orders nobody will pay.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker

import texts
from db.models import DiscountKind, Payment, PaymentStatus, Subscription, utcnow
from services import billing
from services import discounts as discount_service
from services import subscriptions as subs
from services.audit import Action, record_action
from services.subscriptions import CAPTION_LIMIT
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)

#: Telegram's documented ceiling for bulk sending is 30 messages/second; the plan's risk table
#: holds this to 20 to leave headroom for everything else the bot is doing at the time.
MESSAGES_PER_SECOND = 20

#: Seconds between sends. One divided by the rate, rather than a sleep chosen by feel.
SEND_INTERVAL = 1 / MESSAGES_PER_SECOND


@dataclass(frozen=True)
class PriceOffer:
    """The price the broadcast's payment links should charge.

    Applied by granting each recipient a real ``Discount``, so the amount flows through
    ``effective_price`` like every other price in the system rather than through a second,
    parallel pricing path. It is therefore visible in 🎟 Знижки, revocable, and it governs the
    member's renewals too until it expires.

    ``days=None`` means the price has no end date.
    """

    kind: DiscountKind
    currency: str
    percent_off: int | None = None
    fixed_price: Decimal | None = None
    days: int | None = None
    note: str | None = None


@dataclass
class BroadcastResult:
    """What a broadcast actually did. Reported to the admin who started it."""

    sent: int = 0
    blocked: int = 0
    failed: int = 0
    invoiced: int = 0
    #: How many were given the special price.
    repriced: int = 0
    #: Members the payment link could not be built for, so an admin can follow up by hand.
    no_invoice: list[int] = field(default_factory=list)

    def as_counts(self) -> dict[str, int]:
        return {
            "sent": self.sent,
            "blocked": self.blocked,
            "failed": self.failed,
            "invoiced": self.invoiced,
        }


def pay_keyboard(invoice_url: str) -> InlineKeyboardMarkup:
    """The owner's wording for the way back to payment: «Стати частиною клубу!»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=texts.JOIN_CLUB_BUTTON, url=invoice_url)]]
    )


async def _attempt(call, chat_id: int) -> str:
    """One API call, honouring ``RetryAfter``. Returns 'sent', 'blocked' or 'failed'.

    The wait is obeyed rather than the member skipped: dropping somebody from a broadcast
    because Telegram asked us to slow down is the failure mode rule 10 exists to prevent.
    """
    for attempt in (1, 2, 3):
        try:
            await call()
            return "sent"
        except TelegramForbiddenError:
            # Blocked the bot, or never started it. Not an error worth failing a broadcast over.
            return "blocked"
        except TelegramRetryAfter as exc:
            if attempt == 3:
                log.error("Rate-limited three times sending to %s; giving up on them", chat_id)
                return "failed"
            await asyncio.sleep(exc.retry_after)
        except Exception:
            log.exception("Broadcast send failed for %s", chat_id)
            return "failed"
    return "failed"


async def _send(bot: Bot, chat_id: int, text: str, *, photo: str | None = None, **kwargs) -> str:
    """Send one composed message. Returns 'sent', 'blocked' or 'failed'.

    With ``photo`` — a Telegram ``file_id`` — the text travels as the image's caption. A
    ``file_id`` is reusable, so the admin's upload is sent once to Telegram and then referenced
    for every recipient rather than re-uploaded per member.

    **A caption longer than ``CAPTION_LIMIT`` is split**, picture first and words second, because
    Telegram allows 4096 characters in a message and only 1024 on a caption — and a Premium
    account can type a longer caption than a bot is allowed to send. Unsplit, the send fails with
    ``Bad Request: message caption is too long``, which is how a real broadcast preview died
    (2026-10-10). Truncating instead would silently eat the admin's words.

    When it splits, the keyboard rides on the text: it is the last thing the member reads, and a
    pay button above the message explaining the offer reads backwards.
    """
    if photo is not None and len(text) > CAPTION_LIMIT:
        outcome = await _attempt(lambda: bot.send_photo(chat_id, photo=photo), chat_id)
        if outcome != "sent":
            return outcome
        return await _attempt(lambda: bot.send_message(chat_id, text, **kwargs), chat_id)

    if photo is not None:
        return await _attempt(
            lambda: bot.send_photo(chat_id, photo=photo, caption=text or None, **kwargs),
            chat_id,
        )
    return await _attempt(lambda: bot.send_message(chat_id, text, **kwargs), chat_id)


async def send_parts(bot: Bot, chat_id: int, parts: list[dict], *, reply_markup=None) -> str:
    """Send a composed post, part by part, in order. Returns 'sent', 'blocked' or 'failed'.

    ``reply_markup`` goes on the **last** part: it is the last thing the member reads, and a pay
    button above the words explaining the offer reads backwards.

    The pause between parts is the same one used between recipients, so the rate limit counts
    every message the club actually sends rather than every person it sends to (standing rule
    10) — a three-part broadcast to fifty members is a hundred and fifty messages.
    """
    outcome = "sent"
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        extra = {"reply_markup": reply_markup} if (last and reply_markup is not None) else {}
        outcome = await _send(bot, chat_id, part.get("text", ""), photo=part.get("photo"), **extra)
        if outcome != "sent":
            # Stop at the first failure: the rest would land out of context anyway, and a
            # blocked member will block every remaining part too.
            return outcome
        if not last:
            await asyncio.sleep(SEND_INTERVAL)
    return outcome


async def _invoice(
    session,
    client: WayForPayClient,
    *,
    subscription: Subscription,
    config: billing.BillingConfig,
    now: datetime,
    reuse_open: bool = True,
) -> Payment | None:
    """A usable invoice for one member: the open one if it is still fresh, else a new one.

    An open invoice older than the gateway's ``orderTimeout`` is dead at WayForPay even though
    our row still looks open, so reusing it would hand the member a link that fails. Anything
    younger is reused, which keeps a broadcast from creating a second order for every member who
    already had one. ``reuse_open=False`` when a new price is being applied: the open invoice
    carries the old amount, so reusing it would advertise the offer and charge the old price.

    The row is returned rather than just its URL, so the sum quoted in the message is the sum on
    the invoice. Recomputing the price separately is how a message ends up advertising one amount
    and charging another.
    """
    if reuse_open:
        existing = await subs.open_payment(session, subscription.id)
        if existing is not None and existing.invoice_url:
            if now - existing.created_at < config.invoice_timeout:
                return existing

    try:
        return await billing.issue_invoice(
            session, client, subscription=subscription, config=config, now=now
        )
    except Exception:
        log.exception("Broadcast: could not invoice %s", subscription.user_id)
        return None


async def _prepare_link(
    session,
    client: WayForPayClient,
    *,
    user_id: int,
    actor_id: int,
    offer: "PriceOffer | None",
    config: billing.BillingConfig,
    now: datetime,
    result: "BroadcastResult",
) -> str | None:
    """Reprice if there is an offer, then return a usable payment URL — or None.

    None is recorded in ``result.no_invoice`` rather than swallowed: the admin's message still
    goes out, but somebody has to know which members got it without a way to pay.
    """
    subscription = await subs.get_subscription(session, user_id)
    if subscription is None:
        result.no_invoice.append(user_id)
        return None

    if offer is not None:
        # The open invoice, if any, quotes the price from before this offer. Leaving it payable
        # would let the member pay the old amount from an older message, so it is written off
        # here: the new link replaces it.
        stale = await subs.open_payment(session, subscription.id)
        if stale is not None:
            stale.status = PaymentStatus.CANCELED

        # Granted before the invoice is built, because issue_invoice computes the amount from
        # whatever discount is live at that moment. grant() revokes whatever the member had
        # before — one active discount per person — which the admin saw warned at the preview.
        try:
            await discount_service.grant(
                session,
                actor_id=actor_id,
                telegram_id=user_id,
                kind=offer.kind,
                percent_off=offer.percent_off,
                fixed_price=offer.fixed_price,
                currency=offer.currency,
                days=offer.days,
                note=offer.note,
                now=now,
            )
            await session.flush()
            result.repriced += 1
        except Exception:
            log.exception("Broadcast: could not reprice %s", user_id)

    payment = await _invoice(
        session,
        client,
        subscription=subscription,
        config=config,
        now=now,
        reuse_open=offer is None,
    )
    if payment is None or not payment.invoice_url:
        result.no_invoice.append(user_id)
        await session.rollback()
        return None

    url = payment.invoice_url  # read before the commit expires the instance
    await session.commit()
    return url


async def send_broadcast(
    session_factory: async_sessionmaker,
    bot: Bot,
    *,
    user_ids: list[int],
    parts: list[dict],
    actor_id: int,
    audience: str,
    with_invoice: bool = False,
    offer: PriceOffer | None = None,
    client: WayForPayClient | None = None,
    config: billing.BillingConfig | None = None,
    now: datetime | None = None,
) -> BroadcastResult:
    """Send the composed ``parts`` to each of ``user_ids``, the pay button on the last part.

    The recipient list is passed in rather than queried here, so the admin confirms sending to
    exactly the people they were shown a count for — re-running the query at send time could
    quietly include somebody who signed up in between.

    A member who has blocked the bot is counted and skipped. The payment link is only attempted
    for members who have a subscription to invoice against; one that cannot be built is recorded
    in ``no_invoice`` rather than silently dropped, because the text without the link is the half
    of the message that does not work.

    One ``audit_log`` row for the whole broadcast (S6), carrying the audience and the text: "what
    did we send them, and when" is the question that gets asked afterwards.
    """
    now = now or utcnow()
    result = BroadcastResult()

    async with session_factory() as session:
        await record_action(
            session,
            actor_id=actor_id,
            action=Action.BROADCAST_SENT,
            details={
                "audience": audience,
                "recipients": len(user_ids),
                "with_invoice": with_invoice,
                "parts": parts,
                "offer": None
                if offer is None
                else {
                    "kind": offer.kind.value,
                    "percent_off": offer.percent_off,
                    "fixed_price": None if offer.fixed_price is None else str(offer.fixed_price),
                    "currency": offer.currency,
                    "days": offer.days,
                },
            },
            now=now,
        )
        await session.commit()

    for user_id in user_ids:
        # The payment link, when this audience gets one. Built first because it rides on the
        # admin's own message: Telegram will not send a keyboard attached to no text, and the
        # owner's decision of 2026-10-10 is that nothing of the bot's own goes above the button
        # — these people have read the pitch once already at /start.
        url: str | None = None
        if with_invoice and client is not None and config is not None:
            # A fresh session per member: an invoice failure for one must not roll back the
            # invoices already written for the others.
            async with session_factory() as session:
                url = await _prepare_link(
                    session,
                    client,
                    user_id=user_id,
                    actor_id=actor_id,
                    offer=offer,
                    config=config,
                    now=now,
                    result=result,
                )

        outcome = await send_parts(
            bot, user_id, parts, reply_markup=pay_keyboard(url) if url else None
        )
        if outcome == "blocked":
            result.blocked += 1
        elif outcome == "failed":
            result.failed += 1
        else:
            # Counted as sent whether or not it carried a button: the admin's words arriving
            # without a link is a partial success, not a failure, and `no_invoice` already
            # names whoever needs following up by hand.
            result.sent += 1
            if url:
                result.invoiced += 1

        await asyncio.sleep(SEND_INTERVAL)

    if result.no_invoice:
        log.warning(
            "Broadcast: no payment link for %s member(s): %s",
            len(result.no_invoice),
            result.no_invoice,
        )
    log.info("Broadcast to %s: %s", audience, result.as_counts())
    return result


async def post_to_channel(
    session_factory: async_sessionmaker,
    bot: Bot,
    *,
    channel_id: str,
    parts: list[dict],
    actor_id: int,
    now: datetime | None = None,
) -> bool:
    """Publish an admin's text in the channel as the bot. Returns whether it posted.

    The other direction of the same conversation the broadcast covers: members hear from the club
    in their DMs, and the club speaks in the channel. Recorded either way, because a post in a
    private channel is not otherwise attributable to whoever asked for it.
    """
    now = now or utcnow()
    posted = await send_parts(bot, channel_id, parts) == "sent"
    if not posted:
        log.error("Could not post to channel %s", channel_id)

    async with session_factory() as session:
        await record_action(
            session,
            actor_id=actor_id,
            action=Action.CHANNEL_POST,
            details={"channel_id": channel_id, "posted": posted, "parts": parts},
            now=now,
        )
        await session.commit()

    log.info("Admin %s posted to the channel (posted=%s)", actor_id, posted)
    return posted
