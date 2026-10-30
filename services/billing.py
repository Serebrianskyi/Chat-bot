"""Issuing invoices and running the two periodic jobs. Phase 2A.

This is the only module that touches WayForPay, Telegram and the database in one place, so the
services beneath it stay independently testable.

Two jobs, both idempotent (A.20):

* ``poll_open_payments`` — every couple of minutes. Asks CHECK_STATUS about invoices that have
  not settled, and applies the answer.
* ``process_due_subscriptions`` — daily. Invoices whoever has come due, and notifies an admin
  about anyone who has not paid by the end of grace.

``remove_lapsed_members`` takes anyone whose access has ended out of the channel — but only when
``REMOVAL_ENABLED`` is set, which it is not by default. See config.py for why.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import texts
from db.models import (
    AuditLog,
    Discount,
    Payment,
    PaymentStatus,
    PromoCode,
    PromoRedemption,
    Subscription,
    SubscriptionStatus,
    User,
    utcnow,
)
from services import discounts as discount_service
from services import promos, users
from services import subscriptions as subs
from services.audit import Action, record_action
from services.pricing import extend
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
    #: Whether a payment link should also set up a monthly schedule at WayForPay. Default **on**,
    #: matching `config.auto_renew_enabled`: from 2026-10-29 every link handed out is meant to
    #: renew. `build_billing_config` still turns it off when the gateway has nowhere to report a
    #: charge to (polling mode), which is the condition that actually matters.
    auto_renew: bool = True


#: Our own vocabulary for a gateway-side schedule. `requested` has no gateway equivalent and is
#: the one that matters most: the Purchase page was handed over but nobody has paid it, so the
#: member is still owed a link and the daily sweep must keep chasing them.
SCHEDULE_REQUESTED = "requested"
SCHEDULE_ACTIVE = "active"
SCHEDULE_SUSPENDED = "suspended"
SCHEDULE_REMOVED = "removed"

#: How late a gateway-side charge may be before the member is chased by hand as well. The charge
#: falls on the due date itself, and a declined one leaves the schedule looking active, so some
#: tolerance is needed — but not unlimited, or a dead card means silent removal.
SCHEDULE_CHASE_AFTER = timedelta(days=2)

#: Statuses in which a schedule is worth acting on: `stop_schedule` asks the gateway about either,
#: and an unknown order reference is only credited to a member who has one.
#:
#: **Not** the test for "give them a one-off instead" — that is `SCHEDULE_ACTIVE` alone, since
#: only an active schedule will actually charge the card and `requested` never clears without a
#: payment. See `issue_invoice`.
LIVE_SCHEDULE_STATUSES = frozenset({SCHEDULE_REQUESTED, SCHEDULE_ACTIVE})


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


async def _unique_order_reference(session: AsyncSession, user_id: int, now: datetime) -> str:
    """An order reference nothing else holds.

    The reference carries a whole-second timestamp, so a second order for the same member inside
    the same second would collide on the UNIQUE column and abort. That is not hypothetical: a
    broadcast reprices and re-invoices members the daily job may have touched moments earlier,
    and a renewal charge lands on the same morning as the due-date sweep. Suffixed rather than
    randomised, so the reference stays readable for reconciliation against the dashboard.
    """
    base = subs.build_order_reference(user_id, now)
    reference = base
    attempt = 1
    while await session.scalar(select(Payment.id).where(Payment.order_reference == reference)):
        attempt += 1
        reference = f"{base}-{attempt}"
    return reference


async def issue_invoice(
    session: AsyncSession,
    client: WayForPayClient,
    *,
    subscription: Subscription,
    config: BillingConfig,
    now: datetime,
    recurring: bool | None = None,
) -> Payment:
    """Create an invoice for one subscription and persist it.

    The ``payments`` row is written **before** the button reaches the user, so a member who pays
    instantly can always be matched back to an order we know about.

    The amount is computed here rather than read off the subscription, because a discount can
    expire: the same member is billed the reduced amount while it lives and the base amount
    afterwards, with no job needed to expire anything. Whatever is charged is recorded on the
    payment row, and that row is what the gateway's answer is checked against (A.14).

    **Every link is a Purchase.** With `config.auto_renew` on it carries `regularMode=monthly`, so
    the one payment settles this period *and* registers a monthly charge on the member's card —
    the only automatic renewal available on this merchant (tokenisation is not: WayForPay,
    2026-10-29). Without it, the same call without the recurring fields. `CREATE_INVOICE` is gone
    entirely: it cannot carry a schedule, so keeping it meant a second way of asking for money
    that quietly never repeats. `recurring` overrides the config for one call — used for a member
    who already has a schedule, where a second would charge them twice a month.
    """
    order_reference = await _unique_order_reference(session, subscription.user_id, now)

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

    # A promo period is spent where the price is **charged**, not where the code was redeemed:
    # a member who redeems today and pays in three weeks should get a full period from the day
    # they pay. Inside this transaction, so an invoice and its period cannot diverge.
    await promos.consume_period(session, user_id=subscription.user_id, now=now)
    await session.flush()

    # A second schedule for somebody the gateway is already charging would take the money twice,
    # so an **active** one turns this into a one-off page.
    #
    # `requested` deliberately does not: it means a schedule page was handed over and nobody paid
    # it. Treating that as "already has one" would mean a member who opens the page, wanders off
    # and comes back tomorrow is given a one-off instead — and `requested` never clears without a
    # payment, so they could never get a schedule again. That is exactly how "every link from now
    # on renews" would fail, silently, for anyone who does not pay on the first try.
    schedule = config.auto_renew if recurring is None else recurring
    if schedule and subscription.regular_status == SCHEDULE_ACTIVE:
        schedule = False

    # `dateNext` is when the **next** charge falls, so it is this period's end, not the expiry the
    # member holds now — which for anybody past due is in the past, and the gateway refuses a date
    # that is not in the future. The same `extend` the payment itself will apply, so the gateway's
    # schedule and `expires_at` agree by construction.
    next_at = subs.extend(subscription.expires_at, now=now, period_days=subscription.period_days)

    try:
        page = await client.purchase(
            order_reference=order_reference,
            order_date=int(now.timestamp()),
            amount=amount,
            currency=currency,
            product_name=config.product_name,
            recurring=schedule,
            regular_amount=amount if schedule else None,
            # The gateway wants DD.MM.YYYY, which is exactly how `texts.day` renders a date, in
            # the same zone members read it in (rule 1). Rendering it any other way would have the
            # bot promise one date and the card charged on another.
            date_next=texts.day(next_at) if schedule else None,
            order_timeout=int(config.invoice_timeout.total_seconds()),
        )
    except Exception:
        # No order exists at the gateway, so this row describes nothing. Left behind it would
        # be worse than useless: the poller would CHECK_STATUS an order WayForPay never created,
        # and open_payment() would treat it as "already invoiced" and never retry this member
        # until it timed out. Remove it and let the next run start cleanly.
        await session.delete(payment)
        await session.flush()
        raise

    payment.invoice_url = page.page_url
    payment.raw_response = page.raw
    if schedule:
        # `requested`, not `active`: the schedule exists only once somebody pays the page. Until
        # then this member is still owed a payment link like anyone else, and the daily sweep must
        # keep chasing them.
        subscription.regular_order_reference = order_reference
        subscription.regular_status = SCHEDULE_REQUESTED
        subscription.regular_next_at = next_at
        log.info(
            "Order %s is a monthly schedule for %s; next charge %s",
            order_reference,
            subscription.user_id,
            subscription.regular_next_at,
        )
    return payment


async def ensure_invoice(
    session: AsyncSession,
    client: WayForPayClient,
    *,
    subscription: Subscription,
    config: BillingConfig,
    now: datetime,
    reuse_open: bool = True,
) -> Payment | None:
    """The member's current payable invoice: the open one if still live, otherwise a new one.

    An invoice older than the gateway's ``orderTimeout`` is dead at WayForPay even though our row
    still looks open, so handing that link back would give the member a button that fails.
    Anything younger is reused — otherwise every ``/start`` from somebody who owes money would
    mint another order, and a member tapping /start three times would leave three live invoices.

    ``reuse_open=False`` when the price has just changed: the open invoice quotes the old amount,
    so reusing it would advertise one figure and charge another.

    None when the gateway could not be reached; the caller tells the member something true.
    """
    if reuse_open:
        existing = await subs.open_payment(session, subscription.id)
        if existing is not None and existing.invoice_url:
            if now - existing.created_at < config.invoice_timeout:
                return existing

    try:
        return await issue_invoice(
            session, client, subscription=subscription, config=config, now=now
        )
    except Exception:
        log.exception("Could not invoice %s", subscription.user_id)
        return None


async def settle_without_payment(
    session: AsyncSession,
    bot: Bot,
    *,
    subscription: Subscription,
    admin_ids: frozenset[int],
    config: BillingConfig,
    now: datetime,
) -> bool:
    """Grant the period when the price works out to nothing. Returns True when it was granted.

    A 100% discount computes to 0.00, and a zero-amount ``CREATE_INVOICE`` is not a payment
    request — WayForPay will not take it, and the member would be left looking at a price of
    nothing with no button and no access. Reported 2026-10-20 by exactly that route.

    So the period is granted outright, the channel link follows, and no ``payments`` row is
    written: nothing was ever owed, and inventing a settled payment for 0.00 would put a fiction
    into the table the gateway is reconciled against.
    """
    subscription.expires_at = extend(
        subscription.expires_at, now=now, period_days=subscription.period_days
    )
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.grace_until = None
    subscription.last_reminder_at = None
    subscription.admin_notified_at = None

    await record_action(
        session,
        actor_id=subscription.user_id,
        action=Action.SUBSCRIPTION_EXTENDED,
        target_user_id=subscription.user_id,
        details={
            "reason": "price is zero after discount",
            "expires_at": subscription.expires_at.isoformat(),
        },
        now=now,
    )

    await _tell(
        bot,
        subscription.user_id,
        texts.FREE_BY_DISCOUNT.format(
            club=texts.CLUB_NAME, until=texts.day(subscription.expires_at)
        ),
    )
    await deliver_invite(
        session,
        bot,
        subscription=subscription,
        channel_id=config.channel_id,
        admin_ids=admin_ids,
        now=now,
    )
    log.info(
        "Granted %s a free period (price is zero after discount), now expires %s",
        subscription.user_id,
        subscription.expires_at,
    )
    return True


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


async def announce_confirmed_payment(
    session: AsyncSession,
    bot: Bot,
    *,
    payment: Payment,
    admin_ids: frozenset[int],
    config: BillingConfig,
    now: datetime,
    send_invite: bool = True,
) -> None:
    """Tell a member their payment landed, and get them into the channel.

    Shared by the poller and the ``serviceUrl`` callback, which both answer the same question and
    must do the same thing about it. Two copies would drift, and what they would drift on is
    whether a paying member gets their invite.

    Telling the member is separate from having granted them the period. A failure here is logged
    and dropped, and must never write back to ``payment.status``: downgrading a COMPLETE row
    would put it back in the poll query, and ``apply_payment_result``'s idempotency guard only
    holds while it reads COMPLETE, so the next Approved would extend the subscription a second
    time (A.11).
    """
    try:
        subscription = await session.get(Subscription, payment.subscription_id)
        if subscription is None:
            return

        # A first payment and a renewal read differently to the member: one is a welcome, the
        # other a receipt. free_period_granted tells them apart.
        template = (
            texts.PAYMENT_RENEWED
            if subscription.free_period_granted
            else texts.PAYMENT_FIRST_CONFIRMED
        )
        try:
            await bot.send_message(
                subscription.user_id,
                template.format(club=texts.CLUB_NAME, until=texts.day(subscription.expires_at)),
            )
        except TelegramForbiddenError:
            log.warning("Paid but unreachable: %s", subscription.user_id)

        if send_invite:
            await deliver_invite(
                session,
                bot,
                subscription=subscription,
                channel_id=config.channel_id,
                admin_ids=admin_ids,
                now=now,
            )
    except Exception:
        log.exception("Paid and extended, but announcing it failed for %s", payment.order_reference)


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
                result = await client.check_status(order_reference=gateway_reference(payment))
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
                await announce_confirmed_payment(
                    session,
                    bot,
                    payment=payment,
                    admin_ids=admin_ids,
                    config=config,
                    now=now,
                )
            elif payment.status is PaymentStatus.DENIED:
                counts["failed"] += 1

        await session.commit()

    log.info("poll_open_payments: %s", counts)
    return counts


#: How long before a declined card is charged again. Matches `texts.RETRY_AFTER_HOURS`, which is
#: what the member is told, so the promise and the behaviour cannot drift.
CHARGE_RETRY_AFTER = timedelta(hours=texts.RETRY_AFTER_HOURS)

#: How many times a card is charged for one period before the subscription is cancelled. Owner's
#: decision, 2026-10-20: declined once says «Наступна спроба: <tomorrow>», declined twice says
#: «Підписка скасована». Charging a failing card every morning indefinitely is what this replaces.
MAX_CHARGE_ATTEMPTS = 2

#: How long before expiry the member is warned that a charge is coming. One day, because the copy
#: says «Завтра відбудеться автоматичне списання».
REMINDER_LEAD = timedelta(days=1)


async def attempt_charge(
    session: AsyncSession,
    client: WayForPayClient,
    bot: Bot,
    *,
    subscription: Subscription,
    config: BillingConfig,
    now: datetime,
) -> bool:
    """Take this period's money from the stored card. True when it worked.

    The renewal path. ``CHARGE`` is synchronous, so the answer is known here and nothing is left
    for the poller — but the ``payments`` row is written **before** the call, so a charge that
    takes money and then loses the connection is still reconcilable by order reference.

    The amount is computed per charge from this member's own price, discount included, exactly as
    an invoice is. A discount that expires therefore raises the next charge with no job needed.

    Declined is not retried immediately: the member is told «спробуємо ще раз через 24 години» and
    ``CHARGE_RETRY_AFTER`` is what makes that true. Without it the daily job would charge a
    failing card every morning and the copy would be a lie.
    """
    # A charge attempted recently is not repeated — see CHARGE_RETRY_AFTER. Read from the
    # payments table rather than a new column: the row is already the record of the attempt.
    last_attempt = await session.scalar(
        select(Payment.created_at)
        .where(Payment.subscription_id == subscription.id)
        .order_by(Payment.created_at.desc())
        .limit(1)
    )
    if last_attempt is not None and now - last_attempt < CHARGE_RETRY_AFTER:
        log.info(
            "Not charging %s yet: last attempt %s, retry after %s",
            subscription.user_id,
            last_attempt.isoformat(),
            CHARGE_RETRY_AFTER,
        )
        return False

    user = await session.get(User, subscription.user_id)
    amount, currency, _ = await discount_service.effective_price(
        session,
        subscription=subscription,
        username=user.username if user else None,
        now=now,
    )
    order_reference = await _unique_order_reference(session, subscription.user_id, now)

    payment = Payment(
        user_id=subscription.user_id,
        subscription_id=subscription.id,
        order_reference=order_reference,
        amount=amount,
        currency=currency,
        status=PaymentStatus.PENDING,  # money is in flight the moment the request leaves
        created_at=now,
    )
    session.add(payment)

    # Renewals spend a promo period too. Without this an automatic charge would keep using the
    # promo price for ever, because nothing else counts it down.
    await promos.consume_period(session, user_id=subscription.user_id, now=now)
    await session.flush()

    try:
        result = await client.charge(
            order_reference=order_reference,
            order_date=int(now.timestamp()),
            amount=amount,
            currency=currency,
            product_name=config.product_name,
            rec_token=subscription.wayforpay_rec_token or "",
        )
    except Exception:
        # The row stays PENDING on purpose, unlike a failed CREATE_INVOICE which is deleted: a
        # CHARGE that errored may still have taken the money, so the poller must ask about it.
        log.exception("CHARGE failed for %s", order_reference)
        payment.status = PaymentStatus.ERROR
        payment.last_checked_at = now
        return False

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
        # A renewal, not a first payment: they are already in the channel, so no invite.
        try:
            await bot.send_message(
                subscription.user_id,
                texts.PAYMENT_RENEWED.format(until=texts.day(subscription.expires_at)),
            )
        except TelegramForbiddenError:
            log.warning("Renewed but unreachable: %s", subscription.user_id)
        except Exception:
            log.exception("Renewed but could not message %s", subscription.user_id)
        log.info("Renewed %s to %s", subscription.user_id, subscription.expires_at)
        return True

    # Declined. How many times this period's charge has been refused decides what happens next:
    # one more attempt tomorrow, or the subscription ends. Counted from the payments table —
    # `expires_at` does not move on a failure, so it is a stable anchor for "this period".
    attempts = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.subscription_id == subscription.id,
            Payment.status == PaymentStatus.DENIED,
            Payment.created_at >= subscription.expires_at,
        )
    )
    attempts = attempts or 0

    if attempts < MAX_CHARGE_ATTEMPTS:
        message = texts.CHARGE_DECLINED_FIRST.format(
            club=texts.CLUB_NAME,
            amount=texts.money(amount, currency),
            retry_on=texts.day(now + CHARGE_RETRY_AFTER),
        )
    else:
        # Out of attempts. Access ends today and the stored card is forgotten, so nothing can be
        # charged again by accident — the same guarantee `cancel_autorenew` gives.
        subscription.expires_at = now
        subscription.status = SubscriptionStatus.CANCELLED
        subscription.wayforpay_rec_token = None
        subscription.grace_until = None
        await record_action(
            session,
            actor_id=subscription.user_id,
            action=Action.SUBSCRIPTION_CANCELLED,
            target_user_id=subscription.user_id,
            details={
                "reason": "card declined",
                "attempts": attempts,
                "expires_at": now.isoformat(),
            },
            now=now,
        )
        message = texts.CHARGE_DECLINED_FINAL.format(
            club=texts.CLUB_NAME,
            amount=texts.money(amount, currency),
            until=texts.day(now),
        )

    try:
        await bot.send_message(subscription.user_id, message)
    except TelegramForbiddenError:
        log.warning("Charge failed and unreachable: %s", subscription.user_id)
    except Exception:
        log.exception("Charge failed and could not message %s", subscription.user_id)
    log.warning(
        "CHARGE declined for %s (attempt %s of %s): %s / %s",
        subscription.user_id,
        attempts,
        MAX_CHARGE_ATTEMPTS,
        result.gateway_status,
        result.reason_code,
    )
    return False


async def send_renewal_reminders(
    session_factory: async_sessionmaker,
    client: WayForPayClient | None,
    bot: Bot,
    *,
    config: BillingConfig,
    now: datetime | None = None,
) -> dict[str, int]:
    """Warn members a day before their period ends. Daily.

    Two different messages, because two different things are about to happen:

    * **renewing on its own** — a gateway-side monthly schedule (or, in theory, a stored card),
      so the warning is to check the balance («Завтра відбудеться автоматичне списання»);
    * **neither** — nothing will be taken and they must pay by hand, so the reminder carries a
      payment link.

    ``last_reminder_at`` makes it fire once per period (G3.7): a job that runs daily must not
    send the same warning every morning of the window.
    """
    now = now or utcnow()
    counts = {"automatic": 0, "manual": 0, "unreachable": 0, "skipped": 0}

    async with session_factory() as session:
        upcoming = (
            (
                await session.execute(
                    select(Subscription).where(
                        Subscription.status.in_(
                            [SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL]
                        ),
                        Subscription.expires_at > now,
                        Subscription.expires_at <= now + REMINDER_LEAD,
                    )
                )
            )
            .scalars()
            .all()
        )

        for subscription in upcoming:
            # Already warned about *this* period? `last_reminder_at` is reset whenever the period
            # moves, so a stamp inside the current window means the member has been told.
            # Inclusive on purpose: a reminder sent at the exact instant the window opened — the
            # ordinary case when the job and the window line up — belongs to this period.
            if (
                subscription.last_reminder_at is not None
                and subscription.last_reminder_at >= subscription.expires_at - REMINDER_LEAD
            ):
                counts["skipped"] += 1
                continue

            user = await session.get(User, subscription.user_id)
            amount, currency, _ = await discount_service.effective_price(
                session,
                subscription=subscription,
                username=user.username if user else None,
                now=now,
            )

            if renews_automatically(subscription):
                delivered = await _tell(bot, subscription.user_id, texts.RENEWAL_REMINDER)
                counts["automatic"] += delivered
            else:
                # The link is issued now rather than described, so the reminder is actionable.
                payment = None
                if client is not None:
                    existing = await subs.open_payment(session, subscription.id)
                    if existing is not None and existing.invoice_url:
                        payment = existing
                    else:
                        try:
                            payment = await issue_invoice(
                                session,
                                client,
                                subscription=subscription,
                                config=config,
                                now=now,
                            )
                        except Exception:
                            log.exception(
                                "Could not invoice %s for a reminder", subscription.user_id
                            )
                            payment = None

                text = texts.RENEWAL_MANUAL_DUE.format(
                    club=texts.CLUB_NAME,
                    amount=texts.money(amount, currency),
                    period=subscription.period_days,
                )
                delivered = await _tell(
                    bot,
                    subscription.user_id,
                    text,
                    reply_markup=_pay_keyboard(payment.invoice_url or "") if payment else None,
                )
                counts["manual"] += delivered

            if not delivered:
                counts["unreachable"] += 1
            subscription.last_reminder_at = now

        await session.commit()

    log.info("send_renewal_reminders: %s", counts)
    return counts


async def _tell(bot: Bot, chat_id: int, text: str, *, reply_markup=None) -> int:
    """Send one message, returning 1 if it arrived. Unreachable is counted, never raised."""
    try:
        extra = {"reply_markup": reply_markup} if reply_markup else {}
        await bot.send_message(chat_id, text, **extra)
    except TelegramForbiddenError:
        log.warning("Cannot reach %s", chat_id)
        return 0
    except Exception:
        log.exception("Could not message %s", chat_id)
        return 0
    return 1


# --- job 2: chase due subscriptions --------------------------------------------------------


async def _should_chase(session: AsyncSession, *, subscription: Subscription) -> bool:
    """Whether this member should be sent a payment link at all.

    Owner's instruction, 2026-10-29: chase only people the club has a paying relationship with.
    Two groups qualify, and nobody else:

    * **anyone with a ``complete`` payment** — a renewal, which is an ordinary billing
      conversation;
    * **anyone whose free period is ending** (``free_period_granted``) — the founding members,
      who have never paid but were promised a first period and must be asked once it runs out.

    Everyone else already received a link at ``/start`` and chose not to pay. Re-sending it every
    morning is what this stops: 151 subscriptions were past due, so the daily job was DMing 151
    people a fresh link a day. They are to be approached deliberately instead — 📣 Розсилка with a
    promo code — not dripped at by a cron job.

    Returning False does not mean "no longer a member": the row is still invoiced-when-due by
    anything an admin triggers by hand, and ``remove_lapsed_members`` still sees them.
    """
    if subscription.free_period_granted:
        return True
    paid_before = await session.scalar(
        select(Payment.id)
        .where(
            Payment.user_id == subscription.user_id,
            Payment.status == PaymentStatus.COMPLETE,
        )
        .limit(1)
    )
    return paid_before is not None


async def process_due_subscriptions(
    session_factory: async_sessionmaker,
    client: WayForPayClient,
    bot: Bot,
    *,
    admin_ids: frozenset[int],
    config: BillingConfig,
    now: datetime | None = None,
) -> dict[str, int]:
    """Renew or invoice whoever has come due, then record whoever has not paid.

    A member with a stored ``recToken`` is **charged**; one without is sent a payment link. That
    split is the design doc's Job 1 table, and it is what makes «оплата автоматична» true.

    Idempotent (A.20): a subscription with an open payment is not invoiced again, and
    ``admin_notified_at`` stops the same overdue member being recorded twice.
    """
    now = now or utcnow()
    counts = {
        "invoiced": 0,
        "renewed": 0,
        "scheduled": 0,
        "charge_failed": 0,
        "free": 0,
        "not_chased": 0,
        "unreachable": 0,
        "overdue": 0,
    }

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

            # Somebody who has never paid and never had a free period has already had their
            # link, at /start. They are not chased again — see `_should_chase`.
            if not await _should_chase(session, subscription=subscription):
                counts["not_chased"] += 1
                continue

            # A stored card means the money is taken, not asked for. This is the whole promise
            # `PAYMENT_FIRST_CONFIRMED` makes — «оплата автоматична» — and until renewals existed
            # the bot was saying something untrue. See the design doc's Job 1 table.
            # An active schedule means WayForPay is charging this card itself, so chasing the
            # member as well would hand them a second payment page and take the money twice.
            #
            # **Only for as long as the schedule is actually paying.** A declined card leaves the
            # schedule `active` — the gateway may retry and we must not cancel what it might still
            # collect — and skipping on that alone would mean this member is never sent a link,
            # lapses, and is removed by `remove_lapsed_members` without once being asked to pay.
            # After the grace window they are chased like anyone else; the schedule is left in
            # place, so a charge that does land later simply extends them again.
            if (
                subscription.regular_status == SCHEDULE_ACTIVE
                and now - subscription.expires_at < SCHEDULE_CHASE_AFTER
            ):
                counts["scheduled"] += 1
                continue

            if subscription.wayforpay_rec_token:
                if await attempt_charge(
                    session,
                    client,
                    bot,
                    subscription=subscription,
                    config=config,
                    now=now,
                ):
                    counts["renewed"] += 1
                    # Paid: expires_at is in the future again, so none of the chasing below
                    # applies and grace from this period is irrelevant.
                    continue
                counts["charge_failed"] += 1
                # Falls through: they keep access through grace and the usual chasing resumes,
                # but no invoice is sent — a member with a card on file is not asked to pay by
                # hand until the retries have run out.
            else:
                # Nothing to pay? Then nothing to invoice. A 100% discount lands here.
                member = await session.get(User, subscription.user_id)
                owed, _currency, _discount = await discount_service.effective_price(
                    session,
                    subscription=subscription,
                    username=member.username if member else None,
                    now=now,
                )
                if owed <= 0:
                    await settle_without_payment(
                        session,
                        bot,
                        subscription=subscription,
                        admin_ids=admin_ids,
                        config=config,
                        now=now,
                    )
                    counts["free"] += 1
                    continue

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

                    delivered = await send_payment_link(
                        bot, subscription=subscription, payment=payment
                    )
                    counts["invoiced"] += 1
                    if not delivered:
                        counts["unreachable"] += 1
                    subscription.last_reminder_at = now

            # Grace exhausted and still unpaid. Recorded, not reported: see `mark_overdue` for
            # why an admin is no longer DMed about it. The member is not removed either.
            if (
                subscription.grace_until is not None
                and subscription.grace_until <= now
                and subscription.admin_notified_at is None
            ):
                # actor is the bot acting on the club's behalf; the first admin id owns the record
                await subs.mark_overdue(
                    session,
                    subscription=subscription,
                    actor_id=min(admin_ids) if admin_ids else subscription.user_id,
                    now=now,
                )
                counts["overdue"] += 1
                # Removal is not done here: `remove_lapsed_members` owns it, because access
                # ending is one condition however it ended and this sweep only sees the statuses
                # it might invoice.

        await session.commit()

    log.info("process_due_subscriptions: %s", counts)
    return counts


# --- job 5: take lapsed members out of the channel ------------------------------------------


async def remove_lapsed_members(
    session_factory: async_sessionmaker,
    bot: Bot,
    *,
    channel_id: str | None,
    admin_ids: frozenset[int],
    config: BillingConfig,
    client: WayForPayClient | None = None,
    now: datetime | None = None,
    enabled: bool = False,
) -> dict[str, int]:
    """Remove members whose access ended and whose grace ran out. Daily.

    Its own job rather than a branch of the due-date sweep, which is a deliberate deviation from
    the design doc's Job 1 table. The reason: that sweep only looks at statuses it might invoice
    (TRIAL / ACTIVE / PAST_DUE / EXPIRED), so a member who **cancelled** would never have been
    removed — they would keep channel access for ever. Access ending is one condition whatever
    led to it, so it is one query.

    ``enabled=False`` reports and changes nothing. That is the default, and it is how the list
    gets checked before anybody is trusted to it (G3.8): removing somebody who has in fact paid
    is the most expensive mistake available, and payment detection has been wrong before.

    **Admins are never removed.** A bug that throws the owner out of their own channel is both
    likely and mortifying, and no subscription state should be able to cause it.
    """
    now = now or utcnow()
    counts = {"lapsed": 0, "removed": 0, "failed": 0, "skipped_admin": 0, "would_remove": 0}

    if not channel_id:
        log.error("remove_lapsed_members: CHANNEL_ID is not set; nobody can be removed.")
        return counts

    async with session_factory() as session:
        lapsed = (
            (
                await session.execute(
                    select(Subscription).where(
                        # Access ended, and the grace period after it has run out too. Written as
                        # `expires_at <= now - grace` rather than `expires_at + grace <= now`:
                        # adding an interval to a column is dialect-specific SQL, and binding a
                        # timedelta through UtcDateTime fails outright.
                        Subscription.expires_at <= now - config.grace,
                        # EXPIRED is the mark of "already taken out", so the sweep is idempotent.
                        Subscription.status != SubscriptionStatus.EXPIRED,
                    )
                )
            )
            .scalars()
            .all()
        )

        for subscription in lapsed:
            if subscription.user_id in admin_ids:
                counts["skipped_admin"] += 1
                continue

            counts["lapsed"] += 1
            if not enabled:
                counts["would_remove"] += 1
                log.info(
                    "Would remove %s (access ended %s) — REMOVAL_ENABLED is off",
                    subscription.user_id,
                    subscription.expires_at.isoformat(),
                )
                continue

            # Stop the gateway charging a card for somebody being shown the door. They are only
            # here because payment failed, so an `active` schedule means a declined card the
            # gateway may still retry — and a charge that succeeded after the removal would take
            # their money for a channel they are no longer in.
            stopped, why = await stop_schedule(session, client, subscription=subscription, now=now)
            if not stopped:
                log.error(
                    "Removing %s but their schedule %s could not be stopped: %s",
                    subscription.user_id,
                    subscription.regular_order_reference,
                    why,
                )
                await notify_admins(
                    bot,
                    admin_ids,
                    texts.ADMIN_SCHEDULE_CANCEL_FAILED.format(
                        user_id=subscription.user_id,
                        order=subscription.regular_order_reference or "—",
                        reason=why or "—",
                    ),
                )

            removed = await subs.remove_from_community(
                bot, chat_id=channel_id, user_id=subscription.user_id
            )
            if not removed:
                counts["failed"] += 1
                continue

            # Marked expired only once they are actually out, so a failed removal is retried
            # tomorrow rather than being recorded as done.
            subscription.status = SubscriptionStatus.EXPIRED
            await record_action(
                session,
                actor_id=min(admin_ids) if admin_ids else subscription.user_id,
                action=Action.SUBSCRIPTION_EXPIRED,
                target_user_id=subscription.user_id,
                details={
                    "reason": "access ended and grace exhausted",
                    "expired_at": subscription.expires_at.isoformat(),
                },
                now=now,
            )
            counts["removed"] += 1

            # Told why, and told they can come back — the message is not a punishment.
            await _tell(bot, subscription.user_id, texts.ACCESS_SUSPENDED)

        await session.commit()

    log.info("remove_lapsed_members(enabled=%s): %s", enabled, counts)
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
    counts = {"checked": 0, "paid": 0, "still_unpaid": 0, "errors": 0, "extra_orders": 0}
    #: Members credited during this sweep. The scope query is evaluated once, so every terminal
    #: order of theirs is already in the result set — without this, a member invoiced on three
    #: days running would be credited three times over and sent three confirmations and three
    #: links. One lost period is what this recovers; a genuine second payment is a question for
    #: a person, so the remaining orders are named in the log and left alone.
    credited: set[int] = set()

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
            if subscription.user_id in credited:
                log.warning(
                    "Reconcile: %s also has %s outstanding; not credited, check the dashboard "
                    "in case they paid more than once",
                    subscription.user_id,
                    payment.order_reference,
                )
                counts["extra_orders"] += 1
                continue

            counts["checked"] += 1
            try:
                result = await client.check_status(order_reference=gateway_reference(payment))
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
            credited.add(subscription.user_id)

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

            # Nothing else records a confirmed payment: the poller relies on `payments` alone.
            # A credit applied by a job with no human in the loop needs a trace of its own, and
            # the recovery report is built from these rows.
            await record_action(
                session,
                actor_id=subscription.user_id,
                action=Action.PAYMENT_COMPLETE,
                target_user_id=subscription.user_id,
                details={
                    "order_reference": payment.order_reference,
                    "recovered": True,
                    "amount": str(result.amount),
                    "currency": result.currency,
                },
                now=now,
            )

            try:
                await bot.send_message(
                    subscription.user_id,
                    texts.PAYMENT_FIRST_CONFIRMED.format(
                        club=texts.CLUB_NAME, until=texts.day(subscription.expires_at)
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

#: Seconds between probes in the monthly purge. Matches the broadcast rate: the probe is an API
#: call per member, and nothing about a monthly tidy-up justifies a burst (rule 10).
PROBE_INTERVAL = 1 / 20

#: How long a freshly sent link is left alone. Without it, a payment confirmed minutes earlier —
#: by the poller or by the daily reconciliation — would be followed straight away by a "retry",
#: and the member would get two links in a row before having had a chance to open either.
INVITE_SETTLE_PERIOD = timedelta(hours=1)


async def retry_missing_invites(
    session_factory: async_sessionmaker,
    bot: Bot,
    *,
    channel_id: str | None,
    admin_ids: frozenset[int],
    now: datetime | None = None,
    alert_per_member: bool = True,
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

    ``alert_per_member=False`` records the escalation but does not DM anybody about it, for a
    caller that sends one summary covering the whole run. Without it the daily sweep would
    send an admin eleven messages to report ten problems.
    """
    now = now or utcnow()
    counts = {
        "checked": 0,
        "in_channel": 0,
        "retried": 0,
        "escalated": 0,
        "skipped": 0,
        "too_soon": 0,
    }

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

            last_sent = await _last_action_at(
                session, user_id, (Action.INVITE_SENT, Action.INVITE_RETRIED)
            )
            if last_sent is not None and now - last_sent < INVITE_SETTLE_PERIOD:
                # They were sent one moments ago and cannot be expected to have used it yet.
                counts["too_soon"] += 1
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
                    manual=False,  # a job did this, not a person; the report says which
                )
                await record_action(
                    session,
                    actor_id=min(admin_ids) if admin_ids else user_id,
                    action=Action.INVITE_RETRIED,
                    target_user_id=user_id,
                    details={"delivered": delivered, "had_link": link is not None},
                    now=now,
                )
                if delivered:
                    counts["retried"] += 1
                    continue
                # The retry itself failed, so there is no point waiting for a second pass.
                reason = (
                    texts.INVITE_REASON_NO_LINK if link is None else texts.INVITE_REASON_UNREACHABLE
                )

            if alert_per_member:
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
                now=now,
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


async def _last_action_at(
    session: AsyncSession, user_id: int, actions: tuple[str, ...]
) -> datetime | None:
    """When any of ``actions`` last happened to this member, or None if never."""
    return await session.scalar(
        select(AuditLog.created_at)
        .where(AuditLog.target_user_id == user_id, AuditLog.action.in_(list(actions)))
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )


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


# --- daily: nobody who paid should still be waiting -----------------------------------------


#: A recovery sweep is worth doing once a day, not once per deploy. Ten deployments in an
#: afternoon used to mean ten sweeps and ten admin reports about the same people; this is the
#: guard that makes "once a day" true however the sweep is triggered.
RECOVERY_MIN_INTERVAL = timedelta(hours=20)


async def recovery_ran_recently(
    session_factory: async_sessionmaker, *, now: datetime
) -> datetime | None:
    """When the last sweep ran, if that was recent enough that this one should be skipped."""
    async with session_factory() as session:
        last = await session.scalar(
            select(AuditLog.created_at)
            .where(AuditLog.action == Action.ACCESS_RECOVERED)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    if last is not None and now - last < RECOVERY_MIN_INTERVAL:
        return last
    return None


#: At most this many members are named per section of the recovery report. Telegram rejects a
#: message over 4096 characters, and a truncated count still tells an admin where to look.
REPORT_LIMIT = 25


async def _recovery_report(
    session_factory: async_sessionmaker, *, since: datetime, failed_steps: list[str]
) -> str | None:
    """Compose the sweep's report from what it actually recorded.

    Built from ``audit_log`` rather than from the jobs' return values on purpose: those rows are
    written after the action succeeded, so the report describes what happened rather than what
    was attempted. Returns None when there is nothing to tell anybody — a deploy that found
    nothing to fix should not DM an admin.
    """
    recovered: list[tuple[str, int, str]] = []
    links: list[tuple[str, int, str]] = []
    needs_you: list[tuple[str, int, str]] = []

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.created_at >= since,
                        AuditLog.action.in_(
                            [
                                Action.PAYMENT_COMPLETE,
                                Action.INVITE_SENT,
                                Action.INVITE_RETRIED,
                                Action.INVITE_ESCALATED,
                            ]
                        ),
                    )
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
        if not rows and not failed_steps:
            return None

        seen: dict[str, set[int]] = {"recovered": set(), "links": set(), "needs_you": set()}
        for row in rows:
            user_id = row.target_user_id
            if user_id is None:
                continue
            user = await session.get(User, user_id)
            handle = (
                f"@{user.username}"
                if user and user.username
                else (user.first_name if user and user.first_name else f"id {user_id}")
            )
            detail = row.details or {}

            if row.action == Action.PAYMENT_COMPLETE and user_id not in seen["recovered"]:
                seen["recovered"].add(user_id)
                recovered.append((handle, user_id, ""))
            elif row.action == Action.INVITE_ESCALATED and user_id not in seen["needs_you"]:
                seen["needs_you"].add(user_id)
                needs_you.append((handle, user_id, f" — {detail.get('reason', '')}"))
            elif row.action in (Action.INVITE_SENT, Action.INVITE_RETRIED):
                if user_id in seen["links"]:
                    continue
                seen["links"].add(user_id)
                extra = "" if detail.get("delivered", True) else texts.ADMIN_STARTUP_UNDELIVERED
                links.append((handle, user_id, extra))

        # A member who needs a person is not also listed as "done".
        links = [row for row in links if row[1] not in seen["needs_you"]]

    parts = [texts.ADMIN_STARTUP_HEADER]
    for header, entries in (
        (texts.ADMIN_STARTUP_RECOVERED, recovered),
        (texts.ADMIN_STARTUP_LINKS, links),
        (texts.ADMIN_STARTUP_NEEDS_YOU, needs_you),
    ):
        if not entries:
            continue
        parts.append(header.format(count=len(entries)))
        for handle, user_id, extra in entries[:REPORT_LIMIT]:
            parts.append(
                texts.ADMIN_STARTUP_LINE.format(handle=handle, user_id=user_id, extra=extra)
            )
        if len(entries) > REPORT_LIMIT:
            parts.append(texts.ADMIN_STARTUP_MORE.format(count=len(entries) - REPORT_LIMIT))

    for step in failed_steps:
        parts.append(texts.ADMIN_STARTUP_STEP_FAILED.format(step=step))

    if len(parts) == 1:
        return None
    parts.append(texts.ADMIN_STARTUP_FOOTER)
    return "".join(parts)


async def recover_access(
    session_factory: async_sessionmaker,
    client: WayForPayClient | None,
    bot: Bot,
    *,
    admin_ids: frozenset[int],
    config: BillingConfig,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, dict[str, int]]:
    """Once a day: find anyone who paid and is not in the channel, and get them their link.

    Two steps, in this order, because the second depends on the first:

    1. ``reconcile_written_off`` — ask WayForPay again about orders this bot wrongly wrote off.
       Anyone who really paid is credited, which is what makes them visible to step 2. Applied
       rather than reported: a payer waiting for access is not something to leave in a report
       nobody runs.
    2. ``retry_missing_invites`` — send a link to every paid member who is not in the channel,
       and hand to an admin anyone a retry cannot get in.

    Both are idempotent and both are safe to repeat on every deploy: step 1 only considers
    members who have **no** access right now, so a credit cannot be applied twice, and step 2
    sends at most one retry per member before escalating to a person.

    **Once a day, not once per deploy.** It used to run at startup, which meant ten deployments
    in an afternoon sent an admin ten reports about the same people. It is on the daily schedule
    now, and ``RECOVERY_MIN_INTERVAL`` enforces the spacing even if something else calls it — a
    deliberate manual run passes ``force=True`` to say so.

    **Nothing here can stop the caller.** Each step is wrapped: a WayForPay outage or a Telegram
    refusal is logged and the bot carries on serving. A sweep that took the bot down with it
    would be worse than the problem it fixes.
    """
    now = now or utcnow()
    results: dict[str, dict[str, int]] = {}
    started = now
    failed_steps: list[str] = []

    if not force:
        last = await recovery_ran_recently(session_factory, now=now)
        if last is not None:
            log.info(
                "Access recovery already ran at %s, inside %s; skipping",
                last.isoformat(),
                RECOVERY_MIN_INTERVAL,
            )
            return {"skipped": {"ran_at": int(last.timestamp())}}

    if client is None:
        log.warning("Access recovery: WayForPay is not configured; skipping reconciliation.")
    else:
        try:
            results["reconciled"] = await reconcile_written_off(
                session_factory,
                client,
                bot,
                admin_ids=admin_ids,
                config=config,
                apply=True,
            )
        except Exception:
            log.exception("Access recovery: reconciliation failed; the bot is still running")
            failed_steps.append("перевірка оплат")

    try:
        results["invites"] = await retry_missing_invites(
            session_factory,
            bot,
            channel_id=config.channel_id,
            admin_ids=admin_ids,
            # One report for the whole sweep, below, instead of a DM per member.
            alert_per_member=False,
        )
    except Exception:
        log.exception("Access recovery: invite retry failed; the bot is still running")
        failed_steps.append("надсилання запрошень")

    # Tell an admin what happened. A sweep that acts on money and access without anybody being
    # told is indistinguishable from one that never ran — and if a step failed, the server log
    # is the only other place that says so.
    try:
        report = await _recovery_report(session_factory, since=started, failed_steps=failed_steps)
        if report is not None:
            await notify_admins(bot, admin_ids, report)
        else:
            log.info("Access recovery: nothing to report, no admin message sent")
    except Exception:
        log.exception("Access recovery: could not report to admins")

    # Written last, and whatever happened: this row is the "already ran" marker, so a failed
    # sweep must still stop the next deploy repeating the whole thing minutes later.
    try:
        async with session_factory() as session:
            await record_action(
                session,
                actor_id=min(admin_ids) if admin_ids else 0,
                action=Action.ACCESS_RECOVERED,
                details={
                    "reconciled": results.get("reconciled"),
                    "invites": results.get("invites"),
                    "failed_steps": failed_steps,
                },
                now=now,
            )
            await session.commit()
    except Exception:
        log.exception("Access recovery: could not record that it ran")

    log.info("Access recovery finished: %s", results)
    return results


# --- the serviceUrl callback: confirmation without polling ----------------------------------


async def process_callback(
    session_factory: async_sessionmaker,
    bot: Bot,
    *,
    client: WayForPayClient,
    payload: dict,
    admin_ids: frozenset[int],
    config: BillingConfig,
    now: datetime | None = None,
) -> bool:
    """Apply a ``serviceUrl`` notification. True when it granted a period.

    This is what makes automatic renewal possible: the callback is the **only** place WayForPay
    delivers a ``recToken`` for an invoice payment — `CHECK_STATUS` does not carry one, proven in
    production on 2026-10-28. See the payments design doc §1a.

    It is also a better confirmation path than polling in its own right. The poller learns about
    a payment up to two minutes late and only while the order is still in its query; the whole
    77-order write-off of 2026-10-09 happened in that gap. A callback arrives once, immediately,
    and carries the gateway's own signature.

    Everything after the signature check is shared with the poller — `apply_payment_result` for
    the money and `announce_confirmed_payment` for the member — so the two routes cannot drift
    on idempotency, the amount check, or whether a payer gets their invite.

    Raises `SignatureMismatch` if the body cannot be trusted: the caller must not acknowledge a
    forged notification.
    """
    now = now or utcnow()
    result = client.read_callback(payload)  # verifies the signature first

    async with session_factory() as session:
        payment = await _payment_for_callback(
            session, result=result, admin_ids=admin_ids, bot=bot, now=now
        )
        if payment is None:
            return False

        # Keep the gateway's own order number on the row even for a first payment: a schedule
        # that later reports under it must find this row, and `diagnose order` must be able to
        # ask WayForPay with the string WayForPay knows.
        if payment.gateway_order_reference is None and result.order_reference != (
            payment.order_reference
        ):
            payment.gateway_order_reference = result.order_reference
        if result.charge_key and payment.charge_key is None:
            payment.charge_key = result.charge_key

        extended = await subs.apply_payment_result(
            session,
            payment=payment,
            status=result.status,
            gateway_status=result.gateway_status,
            amount=result.amount,
            currency=result.currency,
            rec_token=result.rec_token,
            reason_code=result.reason_code,
            raw=result.raw,
            now=now,
        )
        if extended:
            await _confirm_schedule(session, bot, payment=payment, admin_ids=admin_ids, now=now)
            await announce_confirmed_payment(
                session,
                bot,
                payment=payment,
                admin_ids=admin_ids,
                config=config,
                now=now,
                send_invite=await _needs_invite(
                    bot, payment=payment, config=config, subscription_id=payment.subscription_id
                ),
            )
        elif payment.is_recurring and result.status is not PaymentStatus.PENDING:
            await _report_failed_charge(
                session, bot, payment=payment, result=result, admin_ids=admin_ids, now=now
            )
        await session.commit()

    log.info(
        "Callback %s: %s -> extended=%s, recurring=%s, token=%s",
        result.order_reference,
        result.gateway_status,
        extended,
        payment.is_recurring,
        "yes" if result.rec_token else "no",
    )
    return extended


async def _needs_invite(
    bot: Bot, *, payment: Payment, config: BillingConfig, subscription_id: int | None
) -> bool:
    """Whether this payment should be followed by a channel invite.

    A member-initiated payment always is: it is either their first or they are coming back, and
    `deliver_invite` is the only thing that gets them in.

    A **scheduled** charge usually is not — they are already in the channel, and a second
    single-use link is noise. But not always: somebody whose card declined long enough to be
    removed by `remove_lapsed_members`, and whose card then works, would otherwise be charged,
    extended, and left outside the channel they just paid for. So the gateway is asked. An
    unreadable answer counts as "not in", because sending a spare link costs nothing next to
    silently keeping a payer out.
    """
    if not payment.is_recurring:
        return True
    if not config.channel_id or subscription_id is None:
        return False
    status, error = await _channel_status(bot, config.channel_id, payment.user_id)
    if error is not None:
        log.warning("Could not check %s's membership (%s); inviting anyway", payment.user_id, error)
        return True
    return status not in IN_CHANNEL


async def _confirm_schedule(
    session: AsyncSession,
    bot: Bot,
    *,
    payment: Payment,
    admin_ids: frozenset[int],
    now: datetime,
) -> None:
    """Mark the schedule live, and say so if the gateway's price has drifted from ours.

    A schedule only becomes real when somebody pays the page it came from, so this is where
    `requested` turns into `active` — until then the member is still owed a payment link.

    The drift check is the price of letting the gateway hold its own copy of the amount: it was
    fixed when the schedule was created, while this club computes a price per invoice, so a
    discount ending or a promo running out changes what should be charged without changing what
    will be. Nobody is refused access over it — the money was taken and the body was signed — but
    an admin is told, because the fix is a `regularApi` EDIT and only a person can decide it.
    """
    subscription = await session.get(Subscription, payment.subscription_id)
    if subscription is None or subscription.regular_order_reference is None:
        return

    if subscription.regular_status == SCHEDULE_REQUESTED:
        # Point the schedule at the page that was **paid**, not at the last one issued. A member
        # can be handed a second page after abandoning the first, and whichever they eventually
        # pay is the one WayForPay registered the schedule against — which is the only reference
        # `regularApi` will accept when it comes to cancelling it.
        subscription.regular_order_reference = (
            payment.gateway_order_reference or payment.order_reference
        )
    subscription.regular_status = SCHEDULE_ACTIVE
    subscription.regular_next_at = subscription.expires_at

    user = await session.get(User, subscription.user_id)
    expected, currency, _discount = await discount_service.effective_price(
        session,
        subscription=subscription,
        username=user.username if user else None,
        now=now,
    )
    if payment.amount == expected and payment.currency == currency:
        return

    log.warning(
        "Schedule for %s charged %s %s but the price is now %s %s",
        subscription.user_id,
        payment.amount,
        payment.currency,
        expected,
        currency,
    )
    await notify_admins(
        bot,
        admin_ids,
        texts.ADMIN_SCHEDULE_PRICE_DRIFT.format(
            user_id=subscription.user_id,
            was=texts.money(payment.amount, payment.currency),
            price=texts.money(expected, currency),
            order=subscription.regular_order_reference,
        ),
    )


async def _report_failed_charge(
    session: AsyncSession,
    bot: Bot,
    *,
    payment: Payment,
    result,
    admin_ids: frozenset[int],
    now: datetime,
) -> None:
    """A scheduled charge that did not go through: tell the member, and tell an admin.

    The gateway retries on its own schedule and we do not control it, so the member is given a
    live link rather than a promise — paying it settles the period whatever the schedule does
    next. Silence here is the failure mode that matters: their access lapses in a few days and
    nothing has told them why (rule 10).
    """
    subscription = await session.get(Subscription, payment.subscription_id)
    if subscription is None:
        return

    try:
        await bot.send_message(
            subscription.user_id,
            texts.SCHEDULE_CHARGE_FAILED.format(until=texts.day(subscription.expires_at)),
        )
    except TelegramForbiddenError:
        log.warning("Charge failed and %s has blocked the bot", subscription.user_id)
    except Exception:
        log.exception("Could not tell %s their charge failed", subscription.user_id)

    await notify_admins(
        bot,
        admin_ids,
        texts.ADMIN_SCHEDULE_CHARGE_FAILED.format(
            user_id=subscription.user_id,
            amount=texts.money(payment.amount, payment.currency),
            reason=result.gateway_status or "—",
            until=texts.day(subscription.expires_at),
        ),
    )
    await record_action(
        session,
        actor_id=subscription.user_id,
        action=Action.PAYMENT_FAILED,
        target_user_id=subscription.user_id,
        details={
            "order_reference": payment.order_reference,
            "gateway_order_reference": payment.gateway_order_reference,
            "gateway_status": result.gateway_status,
            "reason_code": result.reason_code,
            "schedule": True,
        },
        now=now,
    )


#: ``reasonCode`` the regularApi answers a successful request with (wiki 852521).
REGULAR_API_OK = "4100"


async def stop_schedule(
    session: AsyncSession,
    client: WayForPayClient | None,
    *,
    subscription: Subscription,
    now: datetime,
) -> tuple[bool, str | None]:
    """Stop this member's gateway-side schedule. ``(stopped, why not)``.

    **Nothing may tell a member their renewal is cancelled until this returns True.** The charges
    happen at WayForPay, so a local flag stops nothing: the card would be debited next month under
    a subscription the bot calls cancelled, which is the worst trust failure available here.

    Returns ``(True, None)`` when there was nothing to stop, which is the ordinary case for
    anybody who has never been on a schedule.

    ``merchantPassword`` is a separate credential WayForPay issues on request, and without it this
    API cannot be called at all — so "cannot cancel" is a real outcome, reported rather than
    papered over.
    """
    reference = subscription.regular_order_reference
    if not reference or subscription.regular_status not in LIVE_SCHEDULE_STATUSES:
        return True, None

    if client is None or not client.can_manage_schedules:
        log.error(
            "Cannot stop the schedule %s for %s: no WayForPay merchantPassword is configured",
            reference,
            subscription.user_id,
        )
        return False, "no-credential"

    try:
        answer = await client.regular_remove(order_reference=reference)
    except Exception as exc:
        log.exception("REMOVE failed for schedule %s", reference)
        return False, type(exc).__name__

    reason_code = str(answer.get("reasonCode") or "")
    if reason_code != REGULAR_API_OK:
        log.error(
            "REMOVE refused for schedule %s: reasonCode=%s reason=%r",
            reference,
            reason_code,
            answer.get("reason"),
        )
        return False, f"{reason_code}: {answer.get('reason')}"

    subscription.regular_status = SCHEDULE_REMOVED
    subscription.regular_next_at = None
    await record_action(
        session,
        actor_id=subscription.user_id,
        action=Action.SUBSCRIPTION_CANCELLED,
        target_user_id=subscription.user_id,
        details={"schedule": reference, "regular_api": "REMOVE"},
        now=now,
    )
    log.info("Schedule %s removed for %s", reference, subscription.user_id)
    return True, None


def gateway_reference(payment: Payment) -> str:
    """The order number to ask WayForPay about.

    Ours and theirs are the same for a payment the member initiated. They are **not** for a charge
    the gateway made on its own schedule: `payments.order_reference` is UNIQUE, so a second charge
    reported under the original number has to be stored under a local reference of its own, with
    the gateway's string kept beside it. Asking CHECK_STATUS with our reference would get "order
    not found" and the row would sit at ERROR for ever.
    """
    return payment.gateway_order_reference or payment.order_reference


def renews_automatically(subscription: Subscription) -> bool:
    """Whether money will arrive without the member doing anything.

    Two mechanisms, only one of which works on this merchant: a stored card we charge ourselves
    (`recToken` — unavailable here, WayForPay 2026-10-29) and a schedule the gateway charges on
    its own. Either way the member must not be asked to pay by hand, and the reminder they get is
    "check your balance" rather than "here is a link".
    """
    return bool(subscription.wayforpay_rec_token) or subscription.regular_status == SCHEDULE_ACTIVE


async def _payment_for_callback(
    session: AsyncSession,
    *,
    result,
    admin_ids: frozenset[int],
    bot: Bot,
    now: datetime,
) -> Payment | None:
    """The ``payments`` row a notification is about, creating one if it reports a new charge.

    Four cases, in the order they are tried:

    1. **A row under that exact order reference, not yet settled** — the ordinary first payment.
       Returned as-is.
    2. **A row under that reference, already settled, and the notification is about the *same*
       charge** — a repeat delivery. None, so nothing is applied twice (rule 5).
    3. **A row under that reference, already settled, and a *different* charge** — the gateway's
       monthly schedule reporting the next month under the original order number. A new row is
       created: ours keyed locally, the gateway's string kept in `gateway_order_reference`.
    4. **No row at all, but the reference belongs to a member we can identify** — the same
       schedule reporting under a reference of its own. Also a new row.

    Cases 3 and 4 exist because **nobody knows which of the two WayForPay does**: the wiki does
    not say, support was asked and the owner chose not to wait. Both are handled, and the branch
    taken is logged at warning level, so the first real charge in production settles it from one
    log line.

    Anything else is reported to an admin rather than dropped: a signed notification about money
    we cannot place is exactly the thing that must not pass quietly.
    """
    existing = await session.scalar(
        select(Payment).where(Payment.order_reference == result.order_reference)
    )
    if existing is None:
        existing = await session.scalar(
            select(Payment)
            .where(Payment.gateway_order_reference == result.order_reference)
            .order_by(Payment.created_at.desc())
            .limit(1)
        )

    if existing is not None and existing.status is not PaymentStatus.COMPLETE:
        return existing

    if existing is not None:
        if result.charge_key is not None and existing.charge_key == result.charge_key:
            log.info(
                "Callback repeats charge %s of order %s; already applied",
                result.charge_key,
                result.order_reference,
            )
            return None
        already = await session.scalar(
            select(Payment.id).where(
                Payment.charge_key == result.charge_key,
                Payment.charge_key.is_not(None),
            )
        )
        if already is not None:
            log.info("Callback repeats charge %s, already stored", result.charge_key)
            return None
        log.warning(
            "Order %s reports a second charge (%s): the schedule reuses the original "
            "orderReference",
            result.order_reference,
            result.charge_key,
        )
        # From the schedule, not from the row that was found: that lookup takes the newest row
        # under this gateway reference, and a row with no subscription_id would propagate into a
        # charge that cannot extend anything ("has no subscription to extend").
        owner = await _schedule_owner(session, result.order_reference)
        subscription_id = owner.id if owner is not None else existing.subscription_id
        return await _new_recurring_payment(
            session,
            subscription_id=subscription_id,
            user_id=owner.user_id if owner is not None else existing.user_id,
            result=result,
            now=now,
        )

    subscription = await _schedule_owner(session, result.order_reference)
    if subscription is None:
        # Genuine (it passed the signature) but unplaceable. Acknowledged so WayForPay stops
        # retrying, and an admin is told, because the alternative is money taken and nobody aware.
        log.error(
            "Callback for an unknown order %s (%s); nothing to apply",
            result.order_reference,
            result.gateway_status,
        )
        await notify_admins(
            bot,
            admin_ids,
            texts.ADMIN_UNKNOWN_CALLBACK.format(
                order=result.order_reference,
                status=result.gateway_status or "—",
                amount=result.amount if result.amount is not None else "—",
            ),
        )
        return None

    log.warning(
        "Order %s is a scheduled charge for %s under a new reference",
        result.order_reference,
        subscription.user_id,
    )
    return await _new_recurring_payment(
        session,
        subscription_id=subscription.id,
        user_id=subscription.user_id,
        result=result,
        now=now,
    )


async def _schedule_owner(session: AsyncSession, order_reference: str) -> Subscription | None:
    """Whose schedule a reference we have never seen belongs to, if it can be told.

    Two ways, both cheap: the schedule's own reference is stored on the subscription, and a
    reference the gateway derived from ours still begins with it. Our references are
    ``sub-<telegram_id>-<timestamp>``, so the member's id is also readable straight out of the
    string — used last, because a prefix match is evidence and a parsed id is a guess.
    """
    subscription = await session.scalar(
        select(Subscription).where(Subscription.regular_order_reference == order_reference)
    )
    if subscription is not None:
        return subscription

    for candidate in await session.scalars(
        select(Subscription).where(Subscription.regular_order_reference.is_not(None))
    ):
        stored = candidate.regular_order_reference or ""
        if stored and order_reference.startswith(stored):
            return candidate

    # Last resort, and deliberately the narrowest: **every** order this bot has ever written has
    # the `sub-<id>-<ts>` shape, including the ~150 ordinary invoices, so matching on the shape
    # alone would turn any late notification about an old one-off order into a free period. Only a
    # member who actually has a live schedule can be credited this way.
    parts = order_reference.split("-")
    if len(parts) >= 2 and parts[0] == "sub" and parts[1].isdigit():
        candidate = await session.scalar(
            select(Subscription).where(Subscription.user_id == int(parts[1]))
        )
        if candidate is not None and candidate.regular_status in LIVE_SCHEDULE_STATUSES:
            return candidate
    return None


async def _new_recurring_payment(
    session: AsyncSession,
    *,
    subscription_id: int | None,
    user_id: int,
    result,
    now: datetime,
) -> Payment:
    """A row for a charge WayForPay made on its own, so the money has somewhere to land.

    The amount recorded is **what was taken**, not what we expected. On a member-initiated
    payment those are the same by construction and a mismatch means a tampered body, which is why
    `apply_payment_result` refuses one. A scheduled charge is different: the gateway holds its own
    copy of the figure, fixed when the schedule was created, so a discount ending or a promo
    running out makes the two disagree legitimately. The body is signature-verified either way, so
    the money is real; `_report_price_drift` tells an admin the two copies have parted company.
    """
    reference = await _unique_order_reference(session, user_id, now)
    payment = Payment(
        user_id=user_id,
        subscription_id=subscription_id,
        order_reference=reference,
        gateway_order_reference=result.order_reference,
        charge_key=result.charge_key,
        is_recurring=True,
        amount=result.amount if result.amount is not None else Decimal("0"),
        currency=result.currency or "",
        status=PaymentStatus.PENDING,
        created_at=now,
    )
    session.add(payment)
    await session.flush()
    return payment


# --- monthly: forget members who blocked the bot ---------------------------------------------


#: Tables whose rows reference ``users.telegram_id``, in the order they must be deleted: children
#: before parents. ``audit_log`` appears twice because both ``actor_id`` and ``target_user_id``
#: are foreign keys to users, so a row naming the member either way blocks the delete.
#: ``discounts.user_id`` and ``audit_log.target_user_id`` are plain columns rather than declared
#: foreign keys, so nothing would stop the delete — they are here because a purge that left a
#: member's discount and the record of it behind would not be a purge.
#: ``promo_codes.created_by`` is deliberately **not** here: a campaign belongs to the club, not to
#: whoever typed it, and other members' redemptions point at it. Anybody who owns one is kept
#: instead of deleted — see ``_keep_reason``.
#: ``test_the_purge_spares_a_payer_and_an_admin_and_deletes_the_rest`` checks this list against
#: the metadata, so a new table referencing users fails a test rather than a monthly job.
def _purge_statements(telegram_id: int):
    return (
        delete(PromoRedemption).where(PromoRedemption.user_id == telegram_id),
        delete(Payment).where(Payment.user_id == telegram_id),
        delete(Discount).where(Discount.user_id == telegram_id),
        delete(Subscription).where(Subscription.user_id == telegram_id),
        delete(AuditLog).where(AuditLog.actor_id == telegram_id),
        delete(AuditLog).where(AuditLog.target_user_id == telegram_id),
        delete(User).where(User.telegram_id == telegram_id),
    )


async def purge_blocked_users(
    session_factory: async_sessionmaker,
    bot: Bot,
    *,
    admin_ids: frozenset[int],
    probe: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete members who have blocked the bot. Monthly, on the 1st.

    Who counts as blocked is read from ``users.is_blocked_bot``, which
    ``handlers/chat_membership.py`` writes from the ``my_chat_member`` update Telegram sends when
    somebody blocks the bot — and clears from the update it sends when they unblock. So the flag
    is current, and the job makes **no API calls at all** in its scheduled form.

    ``probe=True`` instead asks the gateway about every member directly, for the backlog of people
    who blocked the bot before that handler existed. It is not the scheduled mode: the probe makes
    the bot appear to start typing to everyone who has *not* blocked it (``users.has_blocked_bot``
    explains why), which is acceptable as a deliberate one-off and not as a monthly surprise.

    **Two groups are never deleted:**

    * **admins**, for the same reason removal spares them — no amount of subscription state
      should be able to erase the owner's own row;
    * **anyone with a ``complete`` payment.** Their ``payments`` rows are the only record that
      can be reconciled against WayForPay (standing rule 6), and a deleted payment is a figure
      the gateway reports and this database cannot explain. They are listed for an admin to
      decide about by hand.

    Deleting a member also deletes the ``audit_log`` rows naming them, including records of admin
    actions *taken on* them — a gifted month disappears with its recipient. That is a knowing
    exception to rule 2: the alternative is keeping a ``users`` row for ever so its audit trail
    has somewhere to point.

    ``dry_run=True`` reports what it would delete and changes nothing, which is how the list gets
    checked before a month-end run (the same shape as ``remove_lapsed_members`` under
    ``REMOVAL_ENABLED=false``).

    Monthly rather than daily because nothing depends on it being prompt: a blocked member costs
    nothing but a row.
    """
    now = now or utcnow()
    counts = {"checked": 0, "blocked": 0, "purged": 0, "kept_paid": 0, "failed": 0}
    kept: list[tuple[str, int, str]] = []

    async with session_factory() as session:
        query = select(User.telegram_id, User.username)
        if not probe:
            query = query.where(User.is_blocked_bot.is_(True))
        candidates = list((await session.execute(query)).all())

    for telegram_id, username in candidates:
        if telegram_id in admin_ids:
            continue
        counts["checked"] += 1
        try:
            if probe:
                blocked = await _probe_once(bot, telegram_id)
                # One probe per member, paced like any other bulk send (rule 10).
                await asyncio.sleep(PROBE_INTERVAL)
                if not blocked:
                    continue

            counts["blocked"] += 1
            async with session_factory() as session:
                reason = await _keep_reason(session, telegram_id)
                if reason is not None:
                    counts["kept_paid"] += 1
                    kept.append(
                        (f"@{username}" if username else f"id {telegram_id}", telegram_id, reason)
                    )
                    log.info("Blocked but kept: %s (%s)", telegram_id, reason)
                    continue

                if dry_run:
                    log.info("Would purge %s (@%s): blocked, never paid", telegram_id, username)
                    counts["purged"] += 1
                    continue

                for statement in _purge_statements(telegram_id):
                    await session.execute(statement)

                await record_action(
                    session,
                    # The audit row cannot name the purged member — `target_user_id` is a foreign
                    # key to the row being deleted — so their id is kept in the details instead.
                    actor_id=min(admin_ids) if admin_ids else telegram_id,
                    action=Action.USER_PURGED,
                    details={
                        "user_id": telegram_id,
                        "username": username,
                        "reason": "blocked the bot",
                    },
                    now=now,
                )
                await session.commit()

            counts["purged"] += 1
            log.info("Purged %s: they blocked the bot and never paid", telegram_id)
        except Exception:
            # One member's failure must not end the sweep: the rest of the list is unaffected, and
            # a half-finished purge that reported nothing would look like a clean run.
            counts["failed"] += 1
            log.exception("Could not purge %s", telegram_id)

    if counts["blocked"]:
        report = texts.ADMIN_PURGE_REPORT.format(
            blocked=counts["blocked"],
            purged=counts["purged"],
            kept=counts["kept_paid"],
            extra=texts.ADMIN_PURGE_DRY_RUN if dry_run else "",
        )
        if kept:
            report += "\n\n" + "".join(
                texts.ADMIN_PURGE_KEPT_LINE.format(handle=handle, user_id=user_id, reason=reason)
                for handle, user_id, reason in kept[:REPORT_LIMIT]
            )
            report += texts.ADMIN_PURGE_KEPT_NOTE
        if counts["failed"]:
            report += texts.ADMIN_PURGE_FAILED.format(failed=counts["failed"])
        if not dry_run:
            await notify_admins(bot, admin_ids, report)
        else:
            log.info("purge dry run report:\n%s", report)
    else:
        log.info("purge_blocked_users: nobody is marked as having blocked the bot")

    log.info("purge_blocked_users: %s", counts)
    return counts


async def _keep_reason(session, telegram_id: int) -> str | None:
    """Why this blocked member must not be deleted, or None if nothing stops it.

    A ``complete`` payment is the main one: those rows are the only record reconcilable against
    WayForPay (rule 6). Owning a promo campaign is the other — the code outlives whoever typed it,
    and other members' redemptions point at it.
    """
    paid = await session.scalar(
        select(Payment.id)
        .where(Payment.user_id == telegram_id, Payment.status == PaymentStatus.COMPLETE)
        .limit(1)
    )
    if paid is not None:
        return texts.ADMIN_PURGE_KEPT_PAID
    authored = await session.scalar(
        select(PromoCode.id).where(PromoCode.created_by == telegram_id).limit(1)
    )
    if authored is not None:
        return texts.ADMIN_PURGE_KEPT_PROMO
    return None


async def _probe_once(bot: Bot, telegram_id: int) -> bool:
    """``has_blocked_bot``, retried once past a flood limit.

    A ``RetryAfter`` is the one failure that must not be read as "has not blocked": it would turn
    a rate-limited sweep into a clean-looking result (rule 10).
    """
    from aiogram.exceptions import TelegramRetryAfter

    try:
        return await users.has_blocked_bot(bot, telegram_id)
    except TelegramRetryAfter as exc:
        log.warning("Flood limit while probing %s; waiting %ss", telegram_id, exc.retry_after)
        await asyncio.sleep(exc.retry_after + 1)
        return await users.has_blocked_bot(bot, telegram_id)
