"""Issuing invoices and running the two periodic jobs. Phase 2A.

This is the only module that touches WayForPay, Telegram and the database in one place, so the
services beneath it stay independently testable.

Two jobs, both idempotent (A.20):

* ``poll_open_payments`` — every couple of minutes. Asks CHECK_STATUS about invoices that have
  not settled, and applies the answer.
* ``process_due_subscriptions`` — daily. Invoices whoever has come due, and notifies an admin
  about anyone who has not paid by the end of grace.

Neither job removes a member from the community. That is deferred; see ``TODO(removal)``.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import texts
from db.models import (
    Payment,
    PaymentStatus,
    Subscription,
    SubscriptionStatus,
    User,
    utcnow,
)
from services import discounts as discount_service
from services import subscriptions as subs
from services.audit import Action, record_action
from services.wayforpay import WayForPayClient, WayForPayError

log = logging.getLogger(__name__)

#: How long an unpaid invoice stays worth polling before it is written off as abandoned.
INVOICE_TIMEOUT = timedelta(hours=2)

#: How long after the due date a member is chased before an admin is told.
DEFAULT_GRACE = timedelta(days=3)

# Member-facing wording lives in texts.py.


@dataclass
class BillingConfig:
    """Everything the jobs need that is not in the database."""

    period_days: int
    grace: timedelta = DEFAULT_GRACE
    invoice_timeout: timedelta = INVOICE_TIMEOUT
    product_name: str = "Club subscription"
    display_timezone: str = "UTC"
    #: The private channel a paid member is invited into. None means invites cannot be sent.
    channel_id: str | None = None


def _pay_keyboard(invoice_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.PAY_BUTTON.format(club=texts.CLUB_NAME), url=invoice_url
                )
            ]
        ]
    )


async def issue_invoice(
    session: AsyncSession,
    client: WayForPayClient,
    *,
    subscription: Subscription,
    config: BillingConfig,
    now: datetime,
) -> Payment:
    """Create an invoice for one subscription and persist it.

    The ``payments`` row is written **before** the button reaches the user, so a member who pays
    instantly can always be matched back to an order we know about.

    The amount is computed here rather than read off the subscription, because a discount can
    expire: the same member is billed the reduced amount while it lives and the base amount
    afterwards, with no job needed to expire anything. Whatever is charged is recorded on the
    payment row, and that row is what the gateway's answer is checked against (A.14).
    """
    order_reference = subs.build_order_reference(subscription.user_id, now)

    user = await session.get(User, subscription.user_id)
    amount, currency, discount = await discount_service.effective_price(
        session,
        subscription=subscription,
        username=user.username if user else None,
        now=now,
    )
    if discount is not None:
        log.info(
            "Invoice for %s uses discount %s: %s %s instead of %s %s",
            subscription.user_id,
            discount.id,
            amount,
            currency,
            subscription.price,
            subscription.currency,
        )

    payment = Payment(
        user_id=subscription.user_id,
        subscription_id=subscription.id,
        order_reference=order_reference,
        amount=amount,
        currency=currency,
        status=PaymentStatus.PENDING_PAYMENT,
        created_at=now,
    )
    session.add(payment)
    await session.flush()

    try:
        invoice = await client.create_invoice(
            order_reference=order_reference,
            order_date=int(now.timestamp()),
            amount=amount,
            currency=currency,
            product_name=config.product_name,
            order_timeout=int(config.invoice_timeout.total_seconds()),
        )
    except Exception:
        # No invoice exists at the gateway, so this row describes nothing. Left behind it would
        # be worse than useless: the poller would CHECK_STATUS an order WayForPay never created,
        # and open_payment() would treat it as "already invoiced" and never retry this member
        # until it timed out. Remove it and let the next run start cleanly.
        await session.delete(payment)
        await session.flush()
        raise

    payment.invoice_url = invoice.invoice_url
    payment.raw_response = invoice.raw
    return payment


async def send_payment_link(
    bot: Bot, *, subscription: Subscription, payment: Payment, message: str | None = None
) -> bool:
    """DM the payment button. False when the user has blocked the bot or never started it."""
    # The invoice's amount, not the base price: that is what the member will actually be charged.
    amount = texts.money(payment.amount, payment.currency)
    text = message or texts.PAY_PROMPT.format(club=texts.CLUB_NAME, amount=amount)
    try:
        await bot.send_message(
            subscription.user_id,
            text,
            reply_markup=_pay_keyboard(payment.invoice_url or ""),
        )
    except TelegramForbiddenError:
        # A bot cannot open a conversation the user never started, and cannot reach one who has
        # blocked it. Neither is an error worth failing the job over.
        log.warning(
            "Cannot DM %s: they have not started the bot, or blocked it", subscription.user_id
        )
        return False
    return True


async def notify_admins(bot: Bot, admin_ids: frozenset[int], text: str) -> int:
    """Message every admin, returning how many were actually reached.

    An admin who has never started the bot cannot be DMed. That is logged rather than raised,
    because a missing-payment alert that vanishes silently is worse than none (A.18).
    """
    reached = 0
    for admin_id in sorted(admin_ids):
        try:
            await bot.send_message(admin_id, text)
            reached += 1
        except TelegramForbiddenError:
            log.error(
                "Admin %s cannot be notified: they have never started the bot. "
                "Ask them to open it so alerts can be delivered.",
                admin_id,
            )
        except Exception:  # noqa: BLE001 - one bad admin must not stop the others
            log.exception("Failed to notify admin %s", admin_id)
    return reached


# --- job 1: confirm payments ---------------------------------------------------------------


async def deliver_invite(
    session: AsyncSession,
    bot: Bot,
    *,
    subscription: Subscription,
    channel_id: str | None,
    admin_ids: frozenset[int],
    now: datetime,
) -> bool:
    """Send a paid-up member their single-use channel invite.

    Nothing here is allowed to fail loudly. The money is already taken, so every failure path
    ends with the member told something true and an admin told what to fix — never silence, and
    never a lost payment.
    """
    if not channel_id:
        log.error("Paid member %s cannot be invited: CHANNEL_ID is not set.", subscription.user_id)
        await _invite_fallback(session, bot, subscription=subscription, admin_ids=admin_ids)
        return False

    link = await subs.invite_to_community(bot, chat_id=channel_id, user_id=subscription.user_id)
    if link is None:
        await _invite_fallback(session, bot, subscription=subscription, admin_ids=admin_ids)
        return False

    try:
        await bot.send_message(
            subscription.user_id,
            texts.INVITE_TO_COMMUNITY.format(
                club=texts.CLUB_NAME, link=link, days=subs.INVITE_VALID_DAYS
            ),
        )
    except TelegramForbiddenError:
        log.warning("Invite created for %s but they are unreachable", subscription.user_id)
        return False

    await record_action(
        session,
        actor_id=subscription.user_id,
        action=Action.INVITE_SENT,
        target_user_id=subscription.user_id,
        details={"channel_id": channel_id},
    )
    log.info("Invite sent to %s", subscription.user_id)
    return True


async def _invite_fallback(
    session: AsyncSession, bot: Bot, *, subscription: Subscription, admin_ids: frozenset[int]
) -> None:
    """Tell the payer the truth, and tell an admin what to fix."""
    try:
        await bot.send_message(subscription.user_id, texts.INVITE_UNAVAILABLE)
    except TelegramForbiddenError:
        log.warning("Could not even warn %s about the missing invite", subscription.user_id)

    user = await session.get(User, subscription.user_id)
    handle = f"@{user.username}" if user and user.username else "(без username)"
    await notify_admins(
        bot,
        admin_ids,
        texts.ADMIN_INVITE_FAILED.format(handle=handle, user_id=subscription.user_id),
    )


async def poll_open_payments(
    session_factory: async_sessionmaker,
    client: WayForPayClient,
    bot: Bot,
    *,
    config: BillingConfig,
    admin_ids: frozenset[int] = frozenset(),
    now: datetime | None = None,
) -> dict[str, int]:
    """Ask WayForPay about every invoice that has not settled yet.

    Idempotent: a payment already ``complete`` is skipped by ``apply_payment_result``, so running
    this twice grants nothing twice (A.11, A.20).
    """
    now = now or utcnow()
    counts = {"checked": 0, "completed": 0, "failed": 0, "abandoned": 0}

    async with session_factory() as session:
        open_rows = (
            (
                await session.execute(
                    select(Payment).where(
                        Payment.status.in_(
                            [
                                PaymentStatus.PENDING_PAYMENT,
                                PaymentStatus.PENDING,
                                PaymentStatus.ERROR,
                            ]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

        for payment in open_rows:
            age = now - payment.created_at
            if payment.status is PaymentStatus.PENDING_PAYMENT and age > config.invoice_timeout:
                # Never paid within the window. Terminal, so it stops being polled.
                payment.status = PaymentStatus.CANCELED
                counts["abandoned"] += 1
                continue

            counts["checked"] += 1
            try:
                result = await client.check_status(order_reference=payment.order_reference)
            except WayForPayError:
                # Includes a signature mismatch: never act on a body we cannot trust.
                log.exception("Status check failed for %s", payment.order_reference)
                payment.status = PaymentStatus.ERROR
                payment.last_checked_at = now
                continue

            extended = await subs.apply_payment_result(
                session,
                payment=payment,
                status=result.status,
                gateway_status=result.gateway_status,
                amount=result.amount,
                currency=result.currency,
                reason_code=result.reason_code,
                rec_token=result.rec_token,
                raw=result.raw,
                now=now,
            )

            if extended:
                counts["completed"] += 1
                subscription = await session.get(Subscription, payment.subscription_id)
                if subscription is not None:
                    # A first payment and a renewal read differently to the member: one is a
                    # welcome, the other a receipt. free_period_granted tells them apart.
                    template = (
                        texts.PAYMENT_RENEWED
                        if subscription.free_period_granted
                        else texts.PAYMENT_FIRST_CONFIRMED
                    )
                    try:
                        await bot.send_message(
                            subscription.user_id,
                            template.format(
                                club=texts.CLUB_NAME,
                                until=texts.day(subscription.expires_at.date()),
                            ),
                        )
                    except TelegramForbiddenError:
                        log.warning("Paid but unreachable: %s", subscription.user_id)

                    await deliver_invite(
                        session,
                        bot,
                        subscription=subscription,
                        channel_id=config.channel_id,
                        admin_ids=admin_ids,
                        now=now,
                    )
            elif payment.status is PaymentStatus.DENIED:
                counts["failed"] += 1

        await session.commit()

    log.info("poll_open_payments: %s", counts)
    return counts


# --- job 2: chase due subscriptions --------------------------------------------------------


async def process_due_subscriptions(
    session_factory: async_sessionmaker,
    client: WayForPayClient,
    bot: Bot,
    *,
    admin_ids: frozenset[int],
    config: BillingConfig,
    now: datetime | None = None,
) -> dict[str, int]:
    """Invoice whoever has come due, then alert admins about whoever has not paid.

    Idempotent (A.20): a subscription with an open payment is not invoiced again, and
    ``admin_notified_at`` stops the same overdue member being reported twice.
    """
    now = now or utcnow()
    counts = {"invoiced": 0, "unreachable": 0, "escalated": 0}

    async with session_factory() as session:
        due = (
            (
                await session.execute(
                    select(Subscription).where(
                        Subscription.expires_at <= now,
                        Subscription.status.in_(
                            [
                                SubscriptionStatus.TRIAL,
                                SubscriptionStatus.ACTIVE,
                                SubscriptionStatus.PAST_DUE,
                                SubscriptionStatus.EXPIRED,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

        for subscription in due:
            if subscription.grace_until is None:
                subscription.grace_until = subscription.expires_at + config.grace
            if subscription.status is not SubscriptionStatus.PAST_DUE:
                subscription.status = SubscriptionStatus.PAST_DUE

            existing = await subs.open_payment(session, subscription.id)
            if existing is None:
                try:
                    payment = await issue_invoice(
                        session,
                        client,
                        subscription=subscription,
                        config=config,
                        now=now,
                    )
                except WayForPayError:
                    log.exception("Could not invoice %s", subscription.user_id)
                    continue

                delivered = await send_payment_link(bot, subscription=subscription, payment=payment)
                counts["invoiced"] += 1
                if not delivered:
                    counts["unreachable"] += 1
                subscription.last_reminder_at = now

            # Grace exhausted and still unpaid: tell an admin. Do not remove the member.
            if (
                subscription.grace_until is not None
                and subscription.grace_until <= now
                and subscription.admin_notified_at is None
            ):
                user = await session.get(User, subscription.user_id)
                handle = f"@{user.username}" if user and user.username else "(без username)"
                owed_amount, owed_currency, _ = await discount_service.effective_price(
                    session,
                    subscription=subscription,
                    username=user.username if user else None,
                    now=now,
                )
                await notify_admins(
                    bot,
                    admin_ids,
                    texts.ADMIN_PAYMENT_MISSING.format(
                        handle=handle,
                        user_id=subscription.user_id,
                        amount=texts.money(owed_amount, owed_currency),
                        since=texts.day(subscription.expires_at.date()),
                        tier=subscription.price_tier.value,
                    ),
                )
                # actor is the bot acting on the club's behalf; the first admin id owns the record
                await subs.mark_admin_notified(
                    session,
                    subscription=subscription,
                    actor_id=min(admin_ids) if admin_ids else subscription.user_id,
                    now=now,
                )
                counts["escalated"] += 1
                # TODO(removal): once enabled and tested, remove the member here instead of
                #                only alerting. Gate it with the plan's G3.6 and G3.8.

        await session.commit()

    log.info("process_due_subscriptions: %s", counts)
    return counts
