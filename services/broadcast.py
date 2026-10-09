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

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import async_sessionmaker

import texts
from db.models import Payment, Subscription, utcnow
from services import billing
from services import subscriptions as subs
from services.audit import Action, record_action
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)

#: Telegram's documented ceiling for bulk sending is 30 messages/second; the plan's risk table
#: holds this to 20 to leave headroom for everything else the bot is doing at the time.
MESSAGES_PER_SECOND = 20

#: Seconds between sends. One divided by the rate, rather than a sleep chosen by feel.
SEND_INTERVAL = 1 / MESSAGES_PER_SECOND


@dataclass
class BroadcastResult:
    """What a broadcast actually did. Reported to the admin who started it."""

    sent: int = 0
    blocked: int = 0
    failed: int = 0
    invoiced: int = 0
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


async def _send(bot: Bot, chat_id: int, text: str, **kwargs) -> str:
    """Send one message, honouring ``RetryAfter``. Returns 'sent', 'blocked' or 'failed'.

    The wait is obeyed rather than the member skipped: dropping somebody from a broadcast
    because Telegram asked us to slow down is the failure mode rule 10 exists to prevent.
    """
    for attempt in (1, 2, 3):
        try:
            await bot.send_message(chat_id, text, **kwargs)
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


async def _invoice(
    session,
    client: WayForPayClient,
    *,
    subscription: Subscription,
    config: billing.BillingConfig,
    now: datetime,
) -> Payment | None:
    """A usable invoice for one member: the open one if it is still fresh, else a new one.

    An open invoice older than the gateway's ``orderTimeout`` is dead at WayForPay even though
    our row still looks open, so reusing it would hand the member a link that fails. Anything
    younger is reused, which keeps a broadcast from creating a second order for every member who
    already had one.

    The row is returned rather than just its URL, so the sum quoted in the message is the sum on
    the invoice. Recomputing the price separately is how a message ends up advertising one amount
    and charging another.
    """
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


async def send_broadcast(
    session_factory: async_sessionmaker,
    bot: Bot,
    *,
    user_ids: list[int],
    text: str,
    actor_id: int,
    audience: str,
    with_invoice: bool = False,
    client: WayForPayClient | None = None,
    config: billing.BillingConfig | None = None,
    now: datetime | None = None,
) -> BroadcastResult:
    """Send ``text`` to each of ``user_ids``, then a payment link if ``with_invoice``.

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
                "text": text,
            },
            now=now,
        )
        await session.commit()

    for user_id in user_ids:
        outcome = await _send(bot, user_id, text)
        if outcome == "blocked":
            result.blocked += 1
            continue
        if outcome == "failed":
            result.failed += 1
            continue
        result.sent += 1

        if with_invoice and client is not None and config is not None:
            # A fresh session per member: an invoice failure for one must not roll back the
            # invoices already written for the others.
            async with session_factory() as session:
                subscription = await subs.get_subscription(session, user_id)
                if subscription is None:
                    result.no_invoice.append(user_id)
                else:
                    payment = await _invoice(
                        session, client, subscription=subscription, config=config, now=now
                    )
                    if payment is None or not payment.invoice_url:
                        result.no_invoice.append(user_id)
                        await session.rollback()
                    else:
                        # Read before the commit expires the instance.
                        url = payment.invoice_url
                        quoted = texts.money(payment.amount, payment.currency)
                        period = subscription.period_days
                        await session.commit()

                        follow_up = await _send(
                            bot,
                            user_id,
                            texts.BROADCAST_PAY_PROMPT.format(
                                club=texts.CLUB_NAME, amount=quoted, period=period
                            ),
                            reply_markup=pay_keyboard(url),
                        )
                        if follow_up == "sent":
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
    text: str,
    actor_id: int,
    now: datetime | None = None,
) -> bool:
    """Publish an admin's text in the channel as the bot. Returns whether it posted.

    The other direction of the same conversation the broadcast covers: members hear from the club
    in their DMs, and the club speaks in the channel. Recorded either way, because a post in a
    private channel is not otherwise attributable to whoever asked for it.
    """
    now = now or utcnow()
    posted = True
    try:
        await bot.send_message(channel_id, text)
    except Exception:
        log.exception("Could not post to channel %s", channel_id)
        posted = False

    async with session_factory() as session:
        await record_action(
            session,
            actor_id=actor_id,
            action=Action.CHANNEL_POST,
            details={"channel_id": channel_id, "posted": posted, "text": text},
            now=now,
        )
        await session.commit()

    log.info("Admin %s posted to the channel (posted=%s)", actor_id, posted)
    return posted
