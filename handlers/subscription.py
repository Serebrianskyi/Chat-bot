"""The member's own subscription screen: status, and cancelling autorenew.

``/subscription`` (and the 💳 Моя підписка button) shows what they are paying and until when, with
a cancel option when there is something to cancel.

Cancelling keeps access to ``expires_at`` — the member does not lose what they already paid for.
Only the next charge stops, and the stored card token is cleared so nothing can be charged by
accident afterwards.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from db.models import SubscriptionStatus, utcnow
from services import discounts as discount_service
from services import subscriptions as subs

log = logging.getLogger(__name__)

#: Statuses where a future charge exists and can therefore be called off.
CANCELLABLE = frozenset(
    {SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.PAST_DUE}
)


def _status_keyboard(status: SubscriptionStatus) -> InlineKeyboardMarkup | None:
    if status is SubscriptionStatus.CANCELLED:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.RESUME_AUTORENEW_BUTTON, callback_data="sub:resume"
                    )
                ]
            ]
        )
    if status in CANCELLABLE:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.CANCEL_AUTORENEW_BUTTON, callback_data="sub:cancel"
                    )
                ]
            ]
        )
    return None


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
    await message.answer(
        texts.SUBSCRIPTION_STATUS.format(
            status=texts.STATUS_NAMES.get(subscription.status.value, subscription.status.value),
            amount=texts.money(amount, currency),
            period=subscription.period_days,
            until=texts.day(subscription.expires_at.date()),
        ),
        reply_markup=_status_keyboard(subscription.status),
    )


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


async def ask_to_cancel(query: CallbackQuery, session: AsyncSession) -> None:
    """Confirm before cancelling. One tap should not end a subscription."""
    subscription = await subs.get_subscription(session, query.from_user.id)
    await query.answer()
    if query.message is None:
        return

    if subscription is None or subscription.status not in CANCELLABLE:
        if subscription is not None and subscription.status is SubscriptionStatus.CANCELLED:
            await query.message.answer(
                texts.CANCEL_ALREADY_CANCELLED.format(
                    until=texts.day(subscription.expires_at.date())
                )
            )
        else:
            await query.message.answer(texts.CANCEL_NOTHING_TO_CANCEL)
        return

    await query.message.answer(
        texts.CANCEL_CONFIRM.format(until=texts.day(subscription.expires_at.date())),
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


async def confirm_cancel(query: CallbackQuery, session: AsyncSession) -> None:
    subscription = await subs.get_subscription(session, query.from_user.id)
    await query.answer()
    if query.message is None:
        return
    if subscription is None or subscription.status not in CANCELLABLE:
        await query.message.answer(texts.CANCEL_NOTHING_TO_CANCEL)
        return

    now = utcnow()
    await subs.cancel_autorenew(
        session, subscription=subscription, actor_id=query.from_user.id, now=now
    )
    await session.commit()

    # SPEC wording: «Підписка діє до [дата], далі буде скасована»
    await query.message.answer(
        texts.AUTORENEW_CANCELLED.format(until=texts.day(subscription.expires_at.date()))
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
        texts.AUTORENEW_RESUMED.format(until=texts.day(subscription.expires_at.date()))
    )


def build_router() -> Router:
    """The member's subscription router. Not gated: everyone may see their own subscription."""
    router = Router(name="subscription")
    router.message.register(show_subscription, Command("subscription"))
    router.callback_query.register(ask_to_cancel, F.data == "sub:cancel")
    router.callback_query.register(confirm_cancel, F.data == "sub:cancel_yes")
    router.callback_query.register(keep_subscription, F.data == "sub:cancel_no")
    router.callback_query.register(resume, F.data == "sub:resume")
    return router
