"""The member's own subscription screen: status, and cancelling autorenew.

``/subscription`` (and the 💳 Моя підписка button) shows what they are paying and until when, with
a cancel option when there is something to cancel.

Cancelling keeps access to ``expires_at`` — the member does not lose what they already paid for.
Only the next charge stops, and the stored card token is cleared so nothing can be charged by
accident afterwards.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from config import Settings
from db.models import SubscriptionStatus, utcnow
from services import billing
from services import discounts as discount_service
from services import subscriptions as subs
from services.billing import BillingConfig
from services.pricing import extend
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)

#: Statuses where a future charge exists and can therefore be called off.
CANCELLABLE = frozenset(
    {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.PAST_DUE}
)


def _status_keyboard(status: SubscriptionStatus) -> InlineKeyboardMarkup | None:
    """What a member can do from their subscription card.

    Everyone whose subscription is not already cancelled gets **«Оплатити наступний період»**.
    Added 2026-10-29 after an admin granted a discount and the member had no way to act on it:
    their period was still running, so nothing was owed, so no pay button appeared anywhere in
    the bot. Paying early costs them nothing — ``extend`` adds a period to the date they already
    hold rather than replacing it.

    The invoice is created when the button is **tapped**, not when this screen is drawn. Issuing
    one per screen view would fill the payments table with orders nobody asked for, each of which
    the poller then chases for two hours.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if status is not SubscriptionStatus.CANCELLED:
        rows.append([InlineKeyboardButton(text=texts.PAY_EARLY_BUTTON, callback_data="sub:pay")])

    if status is SubscriptionStatus.CANCELLED:
        rows.append(
            [InlineKeyboardButton(text=texts.RESUME_AUTORENEW_BUTTON, callback_data="sub:resume")]
        )
    elif status in CANCELLABLE:
        rows.append(
            [InlineKeyboardButton(text=texts.CANCEL_AUTORENEW_BUTTON, callback_data="sub:cancel")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def render_subscription(
    message: Message, session: AsyncSession, *, telegram_id: int, username: str | None
) -> None:
    """Send the subscription card into ``message``'s chat for the given member.

    The member is passed explicitly rather than read from ``message.from_user``, so a button can
    reuse this: on a callback, ``message.from_user`` is the bot, not the person who tapped.
    """
    subscription = await subs.get_subscription(session, telegram_id)
    if subscription is None:
        await message.answer(texts.CANCEL_NOTHING_TO_CANCEL)
        return

    # The sum quoted is what they will actually be charged next, discount included.
    amount, currency, _ = await discount_service.effective_price(
        session,
        subscription=subscription,
        username=username,
        now=utcnow(),
    )
    now = utcnow()
    status_name = texts.STATUS_NAMES.get(subscription.status.value, subscription.status.value)

    if subscription.expires_at <= now:
        # They owe money, so there is no "valid until" date to quote. Show when it lapsed, and
        # what paying now would buy — which is the question somebody looking at this screen has.
        body = texts.SUBSCRIPTION_STATUS_UNPAID.format(
            status=status_name,
            amount=texts.money(amount, currency),
            period=subscription.period_days,
            until=texts.day(subscription.expires_at),
            next_until=texts.day(
                extend(subscription.expires_at, now=now, period_days=subscription.period_days)
            ),
        )
    else:
        body = texts.SUBSCRIPTION_STATUS.format(
            status=status_name,
            amount=texts.money(amount, currency),
            period=subscription.period_days,
            until=texts.day(subscription.expires_at),
        )

    if billing.renews_automatically(subscription):
        body += texts.SUBSCRIPTION_AUTORENEW_ON

    await message.answer(body, reply_markup=_status_keyboard(subscription.status))


async def show_subscription(message: Message, session: AsyncSession) -> None:
    """``/subscription``."""
    if message.from_user is None:
        return
    await render_subscription(
        message,
        session,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )


async def pay_now(
    query: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    wayforpay: WayForPayClient | None = None,
    billing_config: BillingConfig | None = None,
) -> None:
    """Give the member a live payment link for their next period, on demand.

    Reuses an open invoice rather than minting another — a member tapping twice should get the
    link they already have, not a second order for the poller to chase.
    """
    subscription = await subs.get_subscription(session, query.from_user.id)
    await query.answer()
    if query.message is None:
        return
    if subscription is None:
        await query.message.answer(texts.CANCEL_NOTHING_TO_CANCEL)
        return

    now = utcnow()
    payment = None
    if wayforpay is not None:
        payment = await billing.ensure_invoice(
            session,
            wayforpay,
            subscription=subscription,
            config=billing_config or BillingConfig(period_days=subscription.period_days),
            now=now,
        )
        await session.commit()

    if payment is None or not payment.invoice_url:
        await query.message.answer(texts.PAY_EARLY_UNAVAILABLE)
        return

    await query.message.answer(
        texts.PAY_EARLY_PROMPT.format(
            amount=texts.money(payment.amount, payment.currency),
            period=subscription.period_days,
            until=texts.day(
                extend(subscription.expires_at, now=now, period_days=subscription.period_days)
            ),
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.PAY_BUTTON.format(club=texts.CLUB_NAME),
                        url=payment.invoice_url,
                    )
                ]
            ]
        ),
    )


async def ask_to_cancel(query: CallbackQuery, session: AsyncSession) -> None:
    """Confirm before cancelling. One tap should not end a subscription."""
    subscription = await subs.get_subscription(session, query.from_user.id)
    await query.answer()
    if query.message is None:
        return

    if subscription is None or subscription.status not in CANCELLABLE:
        if subscription is not None and subscription.status is SubscriptionStatus.CANCELLED:
            await query.message.answer(
                texts.CANCEL_ALREADY_CANCELLED.format(until=texts.day(subscription.expires_at))
            )
        else:
            await query.message.answer(texts.CANCEL_NOTHING_TO_CANCEL)
        return

    await query.message.answer(
        texts.CANCEL_CONFIRM.format(until=texts.day(subscription.expires_at)),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.CANCEL_CONFIRM_YES, callback_data="sub:cancel_yes"
                    )
                ],
                [InlineKeyboardButton(text=texts.CANCEL_CONFIRM_NO, callback_data="sub:cancel_no")],
            ]
        ),
    )


async def confirm_cancel(
    query: CallbackQuery,
    session: AsyncSession,
    bot: Bot,
    settings: Settings,
    wayforpay: WayForPayClient | None = None,
) -> None:
    """Cancel autorenew — at the gateway first, in our database second.

    The order matters. A member on a gateway-side monthly schedule is charged by WayForPay, not by
    this bot, so flipping a local flag stops nothing: the card is debited next month under a
    subscription the bot calls cancelled. If the schedule cannot be stopped, the member is told
    the request is being processed — never that it is done — and an admin is sent what they need
    to stop it by hand.
    """
    subscription = await subs.get_subscription(session, query.from_user.id)
    await query.answer()
    if query.message is None:
        return
    if subscription is None or subscription.status not in CANCELLABLE:
        await query.message.answer(texts.CANCEL_NOTHING_TO_CANCEL)
        return

    now = utcnow()
    stopped, why = await billing.stop_schedule(
        session, wayforpay, subscription=subscription, now=now
    )
    if not stopped:
        await session.commit()
        await query.message.answer(
            texts.CANCEL_PENDING_MANUAL.format(until=texts.day(subscription.expires_at))
        )
        await billing.notify_admins(
            bot,
            settings.admin_id_set,
            texts.ADMIN_SCHEDULE_CANCEL_FAILED.format(
                user_id=subscription.user_id,
                order=subscription.regular_order_reference or "—",
                reason=why or "—",
            ),
        )
        log.error("%s asked to cancel but the schedule could not be stopped", query.from_user.id)
        return

    await subs.cancel_autorenew(
        session, subscription=subscription, actor_id=query.from_user.id, now=now
    )
    await session.commit()

    # SPEC wording: «Підписка діє до [дата], далі буде скасована»
    await query.message.answer(
        texts.AUTORENEW_CANCELLED.format(until=texts.day(subscription.expires_at))
    )
    log.info("%s cancelled autorenew", query.from_user.id)


async def keep_subscription(query: CallbackQuery) -> None:
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)


async def resume(query: CallbackQuery, session: AsyncSession) -> None:
    """Undo a cancellation while the paid period is still running."""
    subscription = await subs.get_subscription(session, query.from_user.id)
    await query.answer()
    if query.message is None:
        return
    if subscription is None or subscription.status is not SubscriptionStatus.CANCELLED:
        await query.message.answer(texts.CANCEL_NOTHING_TO_CANCEL)
        return

    now = utcnow()
    await subs.resume_autorenew(
        session, subscription=subscription, actor_id=query.from_user.id, now=now
    )
    await session.commit()
    await query.message.answer(
        texts.AUTORENEW_RESUMED.format(until=texts.day(subscription.expires_at))
    )


def build_router() -> Router:
    """The member's subscription router. Not gated: everyone may see their own subscription."""
    router = Router(name="subscription")
    router.message.register(show_subscription, Command("subscription"))
    router.callback_query.register(pay_now, F.data == "sub:pay")
    router.callback_query.register(ask_to_cancel, F.data == "sub:cancel")
    router.callback_query.register(confirm_cancel, F.data == "sub:cancel_yes")
    router.callback_query.register(keep_subscription, F.data == "sub:cancel_no")
    router.callback_query.register(resume, F.data == "sub:resume")
    return router
