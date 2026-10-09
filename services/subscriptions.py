"""Subscription lifecycle. Phase 2A.

What this slice does: create a subscription at ``/start`` with the right price and due date,
issue invoices, apply confirmed payments, and **notify an admin** when a payment is missing.

What it deliberately does not do: remove anyone from the community. See the ``TODO(removal)``
markers — that is a separate change gated by the plan's G3.6 and G3.8.

Date arithmetic lives in ``services.pricing``; this module owns the database transitions.
Every function takes ``now`` rather than reading the clock, so the jobs are testable.
"""

import asyncio
import logging
from datetime import datetime
from decimal import Decimal

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from db.models import Payment, PaymentStatus, Subscription, SubscriptionStatus, utcnow
from services import discounts as discount_service
from services.audit import Action, record_action
from services.pricing import decide_price, extend, first_expiry

log = logging.getLogger(__name__)

#: How long a single-use invite stays usable. Long enough to notice the message, short enough
#: that a forwarded link is useless by the time it travels.
INVITE_VALID_DAYS = 3


async def get_subscription(session: AsyncSession, telegram_id: int) -> Subscription | None:
    return await session.scalar(select(Subscription).where(Subscription.user_id == telegram_id))


async def ensure_subscription(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    in_community: bool,
    regular_price: Decimal,
    regular_currency: str,
    period_days: int,
    free_period_until: datetime | None,
    now: datetime,
) -> tuple[Subscription, bool]:
    """Return this user's subscription, creating it on first ``/start``.

    Returns ``(subscription, created)``. Idempotent: a second ``/start`` returns the existing row
    untouched, so the free period cannot be granted twice and ``expires_at`` cannot drift (A.1,
    A.6). Re-pricing an existing subscriber is an admin action, not a side effect of saying hello.
    """
    existing = await get_subscription(session, telegram_id)
    if existing is not None:
        return existing, False

    discount = await discount_service.find_active(
        session, telegram_id=telegram_id, username=username, now=now
    )
    decision = decide_price(
        regular_price=regular_price,
        regular_currency=regular_currency,
        has_discount=discount is not None,
        discount_grants_free_period=discount is not None and discount.free_first_period,
        in_community=in_community,
        now=now,
        free_period_until=free_period_until,
    )

    subscription = Subscription(
        user_id=telegram_id,
        # PAST_DUE, not EXPIRED: someone who joined a moment ago and owes their first payment
        # has not expired. EXPIRED reads as "lapsed" to an admin and shows the member
        # «неактивна», both of which are wrong on day one. Their expires_at is now, so past-due
        # is literally true, and it is what the due-date job would set on its next run anyway.
        status=SubscriptionStatus.TRIAL
        if decision.first_period_free
        else SubscriptionStatus.PAST_DUE,
        price=decision.price,
        currency=decision.currency,
        price_tier=decision.tier,
        period_days=period_days,
        started_at=now,
        expires_at=first_expiry(
            decision,
            now=now,
            period_days=period_days,
            free_period_until=free_period_until,
        ),
        free_period_granted=decision.first_period_free,
        source="wayforpay",
    )
    session.add(subscription)

    if discount is not None:
        # Pin the list entry to this id, so a later rename cannot lose the discount.
        await discount_service.claim(session, discount=discount, telegram_id=telegram_id, now=now)

    await session.flush()
    log.info(
        "Created subscription for %s: tier=%s base=%s %s discount=%s free=%s expires=%s",
        telegram_id,
        decision.tier,
        decision.price,
        decision.currency,
        discount.id if discount else None,
        decision.first_period_free,
        subscription.expires_at,
    )
    return subscription, True


def is_due(subscription: Subscription, *, now: datetime) -> bool:
    """Whether this subscription needs paying. The only definition of "due" in the project."""
    return subscription.expires_at <= now


async def open_payment(session: AsyncSession, subscription_id: int) -> Payment | None:
    """The newest payment for this subscription that has not reached a terminal state."""
    return await session.scalar(
        select(Payment)
        .where(
            Payment.subscription_id == subscription_id,
            Payment.status.in_(
                [PaymentStatus.PENDING_PAYMENT, PaymentStatus.PENDING, PaymentStatus.ERROR]
            ),
        )
        .order_by(Payment.created_at.desc())
    )


def build_order_reference(telegram_id: int, now: datetime) -> str:
    """A unique, human-readable order id.

    Readable because it is the key you will use when reconciling against the WayForPay dashboard
    (plan G5.13); the timestamp keeps it unique across periods for the same user.
    """
    return f"sub-{telegram_id}-{int(now.timestamp())}"


async def apply_payment_result(
    session: AsyncSession,
    *,
    payment: Payment,
    status: PaymentStatus,
    gateway_status: str | None,
    amount: Decimal | None,
    currency: str | None,
    reason_code: str | None,
    rec_token: str | None,
    raw: dict,
    now: datetime,
) -> bool:
    """Record a gateway result and extend access if it is a confirmed payment.

    Returns True when access was extended. The caller commits.

    Idempotency (A.11): a payment already ``complete`` is left alone, so polling the same order
    twice — or a retry after a crash — cannot grant two periods.

    Amount is checked against the **payment's** recorded amount, which was taken from the
    subscription's own price when the invoice was created (A.14). A mismatch is refused rather
    than trusted, because the amount is the one field a tampered-with response would change.
    """
    payment.last_checked_at = now
    payment.wayforpay_status = gateway_status
    payment.reason_code = reason_code
    payment.raw_response = raw

    if payment.status is PaymentStatus.COMPLETE:
        log.info("Order %s already complete; ignoring repeat result", payment.order_reference)
        return False

    if status is not PaymentStatus.COMPLETE:
        # PENDING / ERROR are non-terminal: keep the row pollable rather than writing it off.
        payment.status = status
        return False

    if amount is None or currency is None:
        payment.status = PaymentStatus.ERROR
        log.error("Order %s reported Approved without an amount", payment.order_reference)
        return False

    if amount != payment.amount or currency != payment.currency:
        payment.status = PaymentStatus.ERROR
        log.error(
            "Order %s amount mismatch: expected %s %s, gateway said %s %s",
            payment.order_reference,
            payment.amount,
            payment.currency,
            amount,
            currency,
        )
        return False

    payment.status = PaymentStatus.COMPLETE
    payment.settled_at = now

    if payment.subscription_id is None:
        log.error("Order %s has no subscription to extend", payment.order_reference)
        return False
    subscription = await session.get(Subscription, payment.subscription_id)
    if subscription is None:
        log.error("Order %s references a subscription that is gone", payment.order_reference)
        return False

    subscription.expires_at = extend(
        subscription.expires_at, now=now, period_days=subscription.period_days
    )
    subscription.status = SubscriptionStatus.ACTIVE
    subscription.grace_until = None
    subscription.last_reminder_at = None
    subscription.admin_notified_at = None
    if rec_token:
        # Captured now so renewals can be built later without a migration.
        subscription.wayforpay_rec_token = rec_token

    log.info(
        "Order %s complete; %s now expires %s",
        payment.order_reference,
        subscription.user_id,
        subscription.expires_at,
    )
    return True


async def revoke_for_refund(
    session: AsyncSession, *, payment: Payment, actor_id: int, now: datetime
) -> None:
    """A refunded or reversed payment takes the period back.

    The plan's gate does not cover this path; it is why G5.14 asks for a real payment to be
    refunded and the resulting state checked.
    """
    if payment.subscription_id is None:
        # Explicit rather than relying on session.get(None): SQLAlchemy warns that a NULL
        # primary-key lookup may become an error.
        log.warning("Refund on %s has no subscription to revoke", payment.order_reference)
        return
    subscription = await session.get(Subscription, payment.subscription_id)
    if subscription is None:
        return
    subscription.expires_at = now
    subscription.status = SubscriptionStatus.EXPIRED
    await record_action(
        session,
        actor_id=actor_id,
        action=Action.SUBSCRIPTION_REVOKED,
        target_user_id=subscription.user_id,
        details={
            "reason": payment.status.value,
            "order_reference": payment.order_reference,
            "amount": str(payment.amount),
        },
    )
    # TODO(removal): once removal is enabled, this is where the member leaves the community.


async def mark_admin_notified(
    session: AsyncSession, *, subscription: Subscription, actor_id: int, now: datetime
) -> None:
    """Record that an admin was told about a missing payment (A.17, S6).

    ``admin_notified_at`` is what makes the job idempotent — without it every run would alert
    again for the same overdue member.
    """
    subscription.admin_notified_at = now
    await record_action(
        session,
        actor_id=actor_id,
        action=Action.PAYMENT_MISSING,
        target_user_id=subscription.user_id,
        details={
            "expires_at": subscription.expires_at.isoformat(),
            "price": str(subscription.price),
            "currency": subscription.currency,
            "tier": subscription.price_tier.value,
        },
    )


async def invite_to_community(bot, *, chat_id: str, user_id: int) -> str | None:
    """Create a single-use invite link for one member. Returns the URL, or None on failure.

    ``member_limit=1`` and a short ``expire_date`` together mean the link admits exactly the
    person it was sent to, once. A shared link would let one payment buy access for a crowd.

    Returns None rather than raising: a channel misconfiguration must not undo a payment that has
    already been accepted. The caller tells the member something useful instead.
    """
    from datetime import timedelta

    try:
        link = await bot.create_chat_invite_link(
            chat_id=chat_id,
            member_limit=1,
            expire_date=utcnow() + timedelta(days=INVITE_VALID_DAYS),
            name=f"sub-{user_id}"[:32],
        )
        # Inside the try on purpose: an unexpected response shape has to fail the same way a
        # refusal does. A guard that does not cover the line that actually breaks is not a guard.
        return link.invite_link
    except Exception:
        log.exception(
            "Could not create an invite for %s into %s. Check the bot is an administrator "
            "there with 'Invite users via link'.",
            user_id,
            chat_id,
        )
        return None


async def send_manual_invite(
    session: AsyncSession,
    bot,
    *,
    user_id: int,
    actor_id: int,
    channel_id: str,
    now: datetime,
    manual: bool = True,
) -> tuple[bool, str | None]:
    """Send one member a channel link outside the payment flow. Returns (delivered, link).

    ``manual`` records whether a person pressed the button or a job did it. It is written into
    the audit row and read back by ``scripts.diagnose invites``, which is where somebody decides
    "did the bot handle this or do I still owe them a link" — so labelling an automatic retry as
    manual would make that report lie.

    Separate from ``billing.deliver_invite`` for one reason that matters: the audit row must name
    the **admin** as actor, not the member (standing rule 2). ``deliver_invite`` runs off a
    confirmed payment and records the member as their own actor, which would be a lie here.

    It also takes a bare ``user_id`` rather than a ``Subscription``, so an admin can invite
    somebody whose payment the bot never managed to record — which is the situation this exists
    for. Whether that is appropriate is the admin's judgement, made at the confirmation step.

    ``(False, link)`` means the link exists but the DM did not arrive: the member blocked the bot
    or never started it. The link is handed back so the admin can pass it on another way rather
    than it being silently thrown away — a created invite link cannot be recovered afterwards.
    """
    link = await invite_to_community(bot, chat_id=channel_id, user_id=user_id)
    if link is None:
        return False, None

    message = texts.INVITE_TO_COMMUNITY.format(
        club=texts.CLUB_NAME, link=link, days=INVITE_VALID_DAYS
    )
    delivered = True
    for attempt in (1, 2):
        try:
            await bot.send_message(user_id, message)
            break
        except TelegramForbiddenError:
            log.warning("Manual invite created for %s but they are unreachable", user_id)
            delivered = False
            break
        except TelegramRetryAfter as exc:
            # Standing rule 10: wait the time Telegram asks for rather than dropping the member.
            # This runs in a loop over every paid member, so a rate limit is expected, not odd.
            if attempt == 2:
                log.error("Rate-limited twice sending %s their invite", user_id)
                delivered = False
                break
            await asyncio.sleep(exc.retry_after)

    # Recorded either way: the link was created and is now live whether or not it was delivered,
    # and "who issued a link to this channel" is the question an audit of access has to answer.
    await record_action(
        session,
        actor_id=actor_id,
        action=Action.INVITE_SENT,
        target_user_id=user_id,
        details={"channel_id": channel_id, "manual": manual, "delivered": delivered},
    )
    log.info("Admin %s sent %s a manual invite (delivered=%s)", actor_id, user_id, delivered)
    return delivered, link


async def cancel_autorenew(
    session: AsyncSession, *, subscription: Subscription, actor_id: int, now: datetime
) -> None:
    """Stop future renewals, keeping access until ``expires_at``.

    The member keeps what they paid for; only the next charge is cancelled. Status becomes
    CANCELLED, which the due-date job treats as "do not invoice", while ``expires_at`` still
    governs access.

    ``wayforpay_rec_token`` is cleared so no stored card can be charged again even by accident.
    """
    subscription.status = SubscriptionStatus.CANCELLED
    subscription.wayforpay_rec_token = None
    await record_action(
        session,
        actor_id=actor_id,
        action=Action.SUBSCRIPTION_CANCELLED,
        target_user_id=subscription.user_id,
        details={"access_until": subscription.expires_at.isoformat()},
    )
    log.info(
        "Autorenew cancelled for %s; access until %s", subscription.user_id, subscription.expires_at
    )


async def resume_autorenew(
    session: AsyncSession, *, subscription: Subscription, actor_id: int, now: datetime
) -> None:
    """Undo a cancellation while the paid period is still running."""
    subscription.status = (
        SubscriptionStatus.ACTIVE if subscription.expires_at > now else SubscriptionStatus.PAST_DUE
    )
    await record_action(
        session,
        actor_id=actor_id,
        action=Action.SUBSCRIPTION_RESUMED,
        target_user_id=subscription.user_id,
        details={"expires_at": subscription.expires_at.isoformat()},
    )


# TODO(removal): remove_from_community(bot, chat_id, user_id) -- ban_chat_member then
#                unban_chat_member, so a returning payer can rejoin. Deferred until the payment
#                path has been exercised against real money; gate it with G3.6 and G3.8.
# TODO(phase-3): grant / extend / revoke by hand from the admin panel, each writing audit_log.
# TODO(phase-3): single-use invite links (create_chat_invite_link, member_limit=1).
# TODO(renewals): CHARGE with wayforpay_rec_token, so renewal needs no user action.
