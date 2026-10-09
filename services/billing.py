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

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import texts
from db.models import (
    AuditLog,
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
            except Exception:
                # Anything else is a bug, not a gateway answer — an unparseable field, say. One
                # such row used to abort the whole job before ``session.commit()``, so no other
                # open payment was ever checked and any status learned earlier in the run was
                # rolled back. Skip the row instead: ERROR is non-terminal, so it is retried.
                log.exception("Status check crashed for %s", payment.order_reference)
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
                # Telling the member is separate from having granted them the period. A failure
                # here is logged and dropped, and must never write back to ``payment.status``:
                # downgrading a COMPLETE row would put it back in the poll query, and
                # ``apply_payment_result``'s idempotency guard only holds while it reads COMPLETE,
                # so the next Approved would extend the subscription a second time (A.11).
                try:
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
                except Exception:
                    log.exception(
                        "Paid and extended, but announcing it failed for %s",
                        payment.order_reference,
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


# --- job 3: reconcile orders that were written off ------------------------------------------


async def reconcile_written_off(
    session_factory: async_sessionmaker,
    client: WayForPayClient,
    bot: Bot,
    *,
    admin_ids: frozenset[int],
    config: BillingConfig,
    now: datetime | None = None,
    apply: bool = False,
) -> dict[str, int]:
    """Ask WayForPay again about orders this bot wrote off, and credit the ones that were paid.

    Why this exists: until 2026-10-09 a ``Declined`` carrying no payment detail was taken as a
    refusal and `DENIED` is terminal, so orders left the poll query about a minute after being
    issued while their payment links stayed live for two hours. Anyone paying after that first
    poll was never asked about again. ``refine_status`` stops it happening to new invoices; this
    recovers the ones already lost, which are unreachable by the poller by definition.

    **Only members who currently have no access are considered.** A subscription whose
    ``expires_at`` is in the future has already been credited — by a real payment or by hand —
    and re-crediting it would add a second period for one payment (standing rule 5). That is also
    precisely why the two hand-written credits are safe from this sweep.

    **Nobody who turns out not to have paid is messaged.** A member who never paid hears nothing
    from this job, by the owner's decision of 2026-10-09: they are to be approached with a
    discount offer later, not chased now.

    ``apply=False`` (the default) reports what it would credit and writes nothing — a sweep that
    moves money paths should be read before it is run.
    """
    now = now or utcnow()
    counts = {"checked": 0, "paid": 0, "still_unpaid": 0, "errors": 0}

    async with session_factory() as session:
        # Terminal rows, newest first, for members who have no access right now.
        rows = (
            await session.execute(
                select(Payment, Subscription)
                .join(Subscription, Payment.subscription_id == Subscription.id)
                .where(
                    Payment.status.in_([PaymentStatus.DENIED, PaymentStatus.CANCELED]),
                    Subscription.expires_at <= now,
                )
                .order_by(Payment.created_at.desc())
            )
        ).all()

        for payment, subscription in rows:
            counts["checked"] += 1
            try:
                result = await client.check_status(order_reference=payment.order_reference)
            except Exception:
                log.exception("Reconcile failed for %s", payment.order_reference)
                counts["errors"] += 1
                continue

            if result.status is not PaymentStatus.COMPLETE:
                counts["still_unpaid"] += 1
                # The gateway's answer is kept for the record (standing rule 6) but the row stays
                # terminal: this sweep must not resurrect dead orders into the poll queue.
                payment.wayforpay_status = result.gateway_status
                payment.reason_code = result.reason_code
                payment.raw_response = result.raw
                payment.last_checked_at = now
                continue

            log.warning(
                "Reconcile: %s was paid after all (%s %s)",
                payment.order_reference,
                result.amount,
                result.currency,
            )
            counts["paid"] += 1
            if not apply:
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
            if not extended:
                # Refused by the amount check, or the row had already been settled. Either way
                # ``apply_payment_result`` has recorded why; nothing is sent to the member.
                continue

            try:
                await bot.send_message(
                    subscription.user_id,
                    texts.PAYMENT_FIRST_CONFIRMED.format(
                        club=texts.CLUB_NAME, until=texts.day(subscription.expires_at.date())
                    ),
                )
            except TelegramForbiddenError:
                log.warning("Reconciled but unreachable: %s", subscription.user_id)
            except Exception:
                log.exception("Reconciled but could not message %s", subscription.user_id)

            try:
                await deliver_invite(
                    session,
                    bot,
                    subscription=subscription,
                    channel_id=config.channel_id,
                    admin_ids=admin_ids,
                    now=now,
                )
            except Exception:
                # The period is granted and committed below; the invite is retried by
                # ``retry_missing_invites``.
                log.exception("Reconciled %s but the invite failed", subscription.user_id)

        if apply:
            await session.commit()
        else:
            await session.rollback()

    log.info("reconcile_written_off(apply=%s): %s", apply, counts)
    return counts


# --- job 4: get a link to paid members who are still outside the channel ---------------------


#: Chat member statuses that mean the member is inside the channel.
IN_CHANNEL = frozenset({"creator", "administrator", "member", "restricted"})

#: Subscription statuses that mean "this member has paid for the access they hold". TRIAL is
#: excluded on purpose: a free first period is not a payment, and the owner's instruction of
#: 2026-10-09 is that members who have not paid are not to be messaged yet.
PAID_STATUSES = (SubscriptionStatus.ACTIVE, SubscriptionStatus.CANCELLED)


async def retry_missing_invites(
    session_factory: async_sessionmaker,
    bot: Bot,
    *,
    channel_id: str | None,
    admin_ids: frozenset[int],
    now: datetime | None = None,
) -> dict[str, int]:
    """Get a channel link to every paid-up member who is not in the channel.

    The automatic invite fires exactly once, on the poll that confirms a payment
    (``poll_open_payments`` → ``deliver_invite``), and never again: a repeat ``complete`` stops at
    the idempotency guard. So a link that failed to send, or that expired unused, used to leave a
    paying member permanently outside with nothing watching.

    One retry, then a person. Per the owner's instruction of 2026-10-09: if the automatic retry
    does not get them in either, an admin is told directly — handle, id, name and reason — and
    this job stops touching that member. Nobody is pestered on a loop, and nothing is dropped.

    Covered: every member holding paid access (ACTIVE or CANCELLED, not yet expired), **plus**
    anyone with a confirmed payment who has never been sent a link at all, even if their period
    has since lapsed — otherwise a payer whose invite failed would drop out of scope the moment
    their month ran out, and never be seen again.

    Not covered, deliberately: a free trial is not a payment, and an unpaid member is not
    messaged at all (see PAID_STATUSES and the owner's instruction of 2026-10-09).
    """
    now = now or utcnow()
    counts = {"checked": 0, "in_channel": 0, "retried": 0, "escalated": 0, "skipped": 0}

    if not channel_id:
        log.error("retry_missing_invites: CHANNEL_ID is not set; cannot check or invite anyone.")
        return counts

    async with session_factory() as session:
        candidates = list(
            (
                await session.execute(
                    select(Subscription.user_id).where(
                        Subscription.status.in_(PAID_STATUSES),
                        Subscription.expires_at > now,
                    )
                )
            )
            .scalars()
            .all()
        )

        # Safety net for the one way a payer could otherwise be missed for good: they paid, the
        # invite failed, and their month ran out before anybody noticed — at which point the
        # filter above stops seeing them. Anyone with a confirmed payment who has never once been
        # sent a link is included regardless of expiry, because what they bought was never
        # delivered. It cannot reach a member who has not paid: a `complete` payment is required.
        held = set(candidates)
        confirmed_payers = (
            (
                await session.execute(
                    select(Payment.user_id)
                    .where(Payment.status == PaymentStatus.COMPLETE)
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        for payer in confirmed_payers:
            if payer in held:
                continue
            if await _has_action(session, payer, Action.INVITE_SENT) or await _has_action(
                session, payer, Action.INVITE_RETRIED
            ):
                continue
            log.warning("Paid but never invited and the period has lapsed: %s", payer)
            candidates.append(payer)
            held.add(payer)

        for user_id in candidates:
            counts["checked"] += 1

            already_retried = await _has_action(session, user_id, Action.INVITE_RETRIED)
            already_escalated = await _has_action(session, user_id, Action.INVITE_ESCALATED)
            if already_escalated:
                # A human was told about this one. Leave it to them.
                counts["skipped"] += 1
                continue

            status, error = await _channel_status(bot, channel_id, user_id)
            if status in IN_CHANNEL:
                counts["in_channel"] += 1
                continue

            ever_invited = await _has_action(session, user_id, Action.INVITE_SENT)
            if error is not None:
                reason = texts.INVITE_REASON_UNKNOWN.format(error=error)
            elif not ever_invited:
                reason = texts.INVITE_REASON_NEVER_SENT
            else:
                reason = texts.INVITE_REASON_NOT_USED

            if not already_retried:
                delivered, link = await subs.send_manual_invite(
                    session,
                    bot,
                    user_id=user_id,
                    # The bot acts for the club here; the lowest admin id owns the record, as in
                    # the missing-payment alert.
                    actor_id=min(admin_ids) if admin_ids else user_id,
                    channel_id=channel_id,
                    now=now,
                )
                await record_action(
                    session,
                    actor_id=min(admin_ids) if admin_ids else user_id,
                    action=Action.INVITE_RETRIED,
                    target_user_id=user_id,
                    details={"delivered": delivered, "had_link": link is not None},
                )
                if delivered:
                    counts["retried"] += 1
                    continue
                # The retry itself failed, so there is no point waiting for a second pass.
                reason = (
                    texts.INVITE_REASON_NO_LINK if link is None else texts.INVITE_REASON_UNREACHABLE
                )

            user = await session.get(User, user_id)
            handle = f"@{user.username}" if user and user.username else "(без username)"
            name = (user.first_name if user and user.first_name else "—") or "—"
            await notify_admins(
                bot,
                admin_ids,
                texts.ADMIN_INVITE_ESCALATION.format(
                    handle=handle, user_id=user_id, name=name, reason=reason
                ),
            )
            await record_action(
                session,
                actor_id=min(admin_ids) if admin_ids else user_id,
                action=Action.INVITE_ESCALATED,
                target_user_id=user_id,
                details={"reason": reason},
            )
            counts["escalated"] += 1

        await session.commit()

    log.info("retry_missing_invites: %s", counts)
    return counts


async def _has_action(session: AsyncSession, user_id: int, action: str) -> bool:
    """Whether this member already has an audit row for ``action``.

    The audit log is the memory of what has been tried, so no column had to be added for it.
    """
    found = await session.scalar(
        select(AuditLog.id)
        .where(AuditLog.target_user_id == user_id, AuditLog.action == action)
        .limit(1)
    )
    return found is not None


async def _channel_status(bot: Bot, channel_id: str, user_id: int) -> tuple[str | None, str | None]:
    """``(status, error)`` for one member. Never raises: one unreadable member cannot stop the job.

    ``RetryAfter`` is honoured rather than skipped (standing rule 10): the member is retried once
    after the wait Telegram asks for, so a rate limit does not quietly drop them from the sweep.
    """
    for attempt in (1, 2):
        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            return member.status, None
        except TelegramRetryAfter as exc:
            if attempt == 2:
                return None, type(exc).__name__
            await asyncio.sleep(exc.retry_after)
        except Exception as exc:  # noqa: BLE001 - the reason is reported to an admin
            return None, type(exc).__name__
    return None, "RetryAfter"
