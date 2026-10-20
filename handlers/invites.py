"""Two admin screens for reaching one member directly: 🔗 a channel link, ✍️ a message.

Exists because a payment and a delivered invite are two separate events, and the second one can
fail on its own — the bot was not an administrator yet, the member had not started the bot, the
poller died before it got that far, or the single-use link expired unused. When that happens the
money is in and the member is outside, and until now there was no way to fix it from the bot.

An FSM walk of two steps: who → confirm. Short on purpose. The confirmation step exists because
the link is **single-use and expires** (``INVITE_VALID_DAYS``): issuing one to the wrong person
burns it, and a typo in a numeric id is not otherwise visible.

**An inactive subscription does not block the send.** It is shown as a warning on the
confirmation instead. The usual reason an admin reaches for this screen is a payment the bot
failed to record, so refusing exactly that case would make the screen useless for its purpose.

``services.subscriptions.send_manual_invite`` writes the ``audit_log`` row, with the admin as
actor (S6).

✍️ Написати учаснику exists for the member with no ``@username``: an admin cannot open that chat
by hand — Telegram offers no way to find them — but the bot has had a chat with everybody who
pressed /start, and can address it by numeric id. The admin dictates, the bot delivers, and the
message is recorded. It is also the fallback the escalation alert points at when a link alone has
not worked.
"""

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from config import Settings
from db.models import SubscriptionStatus, User, normalise_username, utcnow
from handlers import compose
from handlers.broadcast import MAX_MONTHS, parse_months
from services import subscriptions as subs
from services.pricing import extend

log = logging.getLogger(__name__)


class SendInvite(StatesGroup):
    waiting_for_target = State()
    waiting_for_confirmation = State()


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_INVITE_CONFIRM_YES, callback_data="invite:send"
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_INVITE_CONFIRM_NO, callback_data="invite:cancel"
                )
            ],
        ]
    )


async def _find_user(session: AsyncSession, raw: str) -> User | None:
    """Resolve what the admin typed to a user who has actually started the bot.

    Unlike a discount, an invite cannot be left waiting for someone to appear: the bot has to
    send a message now, and it can only message a user it has a chat with. So this resolves
    against ``users`` and returns None when there is no row — there is nothing useful to do with
    an unknown username, and the Bot API offers no way to turn one into an id.

    Usernames are compared lowercased, because Telegram treats them case-insensitively and the
    stored copy keeps whatever casing the member used.
    """
    if raw.lstrip("-").isdigit():
        return await session.get(User, int(raw))
    username = normalise_username(raw)
    if not username:
        return None
    return await session.scalar(select(User).where(func.lower(User.username) == username))


async def start_invite(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # never inherit an abandoned walk
    await state.set_state(SendInvite.waiting_for_target)
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_INVITE_ASK_WHO)


async def cancel_invite(message: Message, state: FSMContext) -> None:
    """``/cancel`` from any step."""
    await state.clear()
    await message.answer(texts.ADMIN_GRANT_CANCELLED)


async def receive_target(message: Message, state: FSMContext, session: AsyncSession) -> None:
    raw = (message.text or "").strip()
    if not raw or (not raw.lstrip("-").isdigit() and not normalise_username(raw)):
        await message.answer(texts.ADMIN_INVITE_BAD_TARGET)
        return

    user = await _find_user(session, raw)
    if user is None:
        await message.answer(texts.ADMIN_INVITE_UNKNOWN)
        return

    handle = f"@{user.username}" if user.username else f"id {user.telegram_id}"
    subscription = await subs.get_subscription(session, user.telegram_id)

    if subscription is None:
        status_name = texts.STATUS_NAMES["expired"]
        until = ""
        active = False
    else:
        status_name = texts.STATUS_NAMES.get(subscription.status.value, subscription.status.value)
        until = f", до {texts.day(subscription.expires_at)}"
        active = (
            subscription.status
            in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIAL, SubscriptionStatus.CANCELLED)
            and subscription.expires_at > utcnow()
        )

    body = texts.ADMIN_INVITE_CONFIRM.format(
        handle=handle, status=status_name, until=until, days=subs.INVITE_VALID_DAYS
    )
    if not active:
        body += texts.ADMIN_INVITE_CONFIRM_WARNING

    await state.update_data(target_id=user.telegram_id, handle=handle)
    await state.set_state(SendInvite.waiting_for_confirmation)
    await message.answer(body, reply_markup=_confirm_keyboard())


async def confirm_send(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    data = await state.get_data()
    await state.clear()
    await query.answer()
    if query.message is None:
        return

    target_id = data.get("target_id")
    handle = data.get("handle", str(target_id))
    if target_id is None:
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)
        return

    if not settings.channel_id:
        await query.message.answer(texts.ADMIN_INVITE_NO_LINK)
        return

    delivered, link = await subs.send_manual_invite(
        session,
        query.bot,
        user_id=int(target_id),
        actor_id=query.from_user.id,
        channel_id=settings.channel_id,
        now=utcnow(),
    )
    # Committed before answering the admin: the audit row is the record that a live link exists,
    # and it must survive even if the reply fails.
    await session.commit()

    if link is None:
        await query.message.answer(texts.ADMIN_INVITE_NO_LINK)
    elif delivered:
        await query.message.answer(texts.ADMIN_INVITE_SENT.format(handle=handle))
    else:
        # The link is live but undeliverable. Hand it to the admin rather than discard it — a
        # created invite link cannot be looked up again through the Bot API.
        await query.message.answer(texts.ADMIN_INVITE_UNREACHABLE.format(handle=handle, link=link))


async def cancel_from_button(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)


class MessageUser(StatesGroup):
    waiting_for_target = State()
    waiting_for_text = State()
    waiting_for_confirmation = State()


def _send_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.ADMIN_MESSAGE_CONFIRM_YES, callback_data="msg:send")],
            [InlineKeyboardButton(text=texts.ADMIN_INVITE_CONFIRM_NO, callback_data="msg:cancel")],
        ]
    )


async def start_message(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # never inherit the parts of an abandoned message
    await state.set_state(MessageUser.waiting_for_target)
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_MESSAGE_ASK_WHO)


async def receive_message_target(
    message: Message, state: FSMContext, session: AsyncSession
) -> None:
    raw = (message.text or "").strip()
    if not raw or (not raw.lstrip("-").isdigit() and not normalise_username(raw)):
        await message.answer(texts.ADMIN_INVITE_BAD_TARGET)
        return

    user = await _find_user(session, raw)
    if user is None:
        await message.answer(texts.ADMIN_INVITE_UNKNOWN)
        return

    handle = f"@{user.username}" if user.username else f"id {user.telegram_id}"
    await state.update_data(target_id=user.telegram_id, handle=handle)
    await state.set_state(MessageUser.waiting_for_text)
    await message.answer(texts.ADMIN_MESSAGE_ASK_TEXT.format(handle=handle))


#: The «Повідомлення від адміністратора …» prefix the send puts on the first part, reserved so a
#: part that passes the limit check here cannot overflow once wrapped.
WRAPPER_LENGTH = len(texts.MESSAGE_FROM_ADMIN.format(club=texts.CLUB_NAME, text=""))


async def receive_message_part(message: Message, state: FSMContext) -> None:
    """Take one part of the message. As many as the admin sends, then Готово."""
    await compose.collect(message, state, cancel_data="msg:cancel", reserved=WRAPPER_LENGTH)


async def finish_message(query: CallbackQuery, state: FSMContext) -> None:
    """Готово: show it back before it is sent.

    A message to a member cannot be unsent, so it is shown for confirmation exactly as it will
    arrive — the one chance to catch a wrong recipient or a half-typed sentence. An attached
    image is shown as an image, not described.
    """
    data = await state.get_data()
    await query.answer()
    if query.message is None:
        return
    parts = data.get("parts", [])
    if not parts:
        await query.message.answer(texts.ADMIN_COMPOSE_NOTHING)
        return

    await state.set_state(MessageUser.waiting_for_confirmation)
    await compose.replay(query.message, parts)
    await query.message.answer(
        texts.ADMIN_MESSAGE_CONFIRM_ASK.format(handle=data.get("handle", "")),
        reply_markup=_send_keyboard(),
    )


async def confirm_message(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    await state.clear()
    await query.answer()
    if query.message is None:
        return

    target_id = data.get("target_id")
    parts: list[dict] = data.get("parts", [])
    handle = data.get("handle", str(target_id))
    if target_id is None or not parts:
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)
        return

    delivered = await subs.send_admin_message(
        session,
        query.bot,
        user_id=int(target_id),
        actor_id=query.from_user.id,
        parts=parts,
    )
    await session.commit()

    if delivered:
        await query.message.answer(texts.ADMIN_MESSAGE_SENT.format(handle=handle))
    else:
        await query.message.answer(texts.ADMIN_MESSAGE_UNREACHABLE.format(handle=handle))


class GiftMonths(StatesGroup):
    waiting_for_target = State()
    waiting_for_months = State()
    waiting_for_confirmation = State()


#: Offered as gift lengths. Same choices as a special price, and a number may be typed.
GIFT_MONTH_CHOICES = (1, 2, 3, 6, 12)


def _gift_months_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.months_phrase(months), callback_data=f"gift:months:{months}"
                )
            ]
            for months in GIFT_MONTH_CHOICES
        ]
    )


def _gift_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.ADMIN_GIFT_CONFIRM_YES, callback_data="gift:send")],
            [InlineKeyboardButton(text=texts.ADMIN_INVITE_CONFIRM_NO, callback_data="gift:cancel")],
        ]
    )


async def start_gift(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()  # never inherit an abandoned walk
    await state.set_state(GiftMonths.waiting_for_target)
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_GIFT_ASK_WHO)


async def receive_gift_target(
    message: Message, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    """Resolve the member, and refuse early if there is nothing to extend."""
    raw = (message.text or "").strip()
    if not raw or (not raw.lstrip("-").isdigit() and not normalise_username(raw)):
        await message.answer(texts.ADMIN_INVITE_BAD_TARGET)
        return

    user = await _find_user(session, raw)
    if user is None:
        await message.answer(texts.ADMIN_INVITE_UNKNOWN)
        return

    # A gift extends an existing subscription. Somebody who has never run /start has no row to
    # extend, and inventing one here would duplicate what onboarding does — including the price
    # decision, which depends on whether they were already in the channel.
    subscription = await subs.get_subscription(session, user.telegram_id)
    if subscription is None:
        await message.answer(texts.ADMIN_GIFT_NO_SUBSCRIPTION)
        return

    handle = f"@{user.username}" if user.username else f"id {user.telegram_id}"
    await state.update_data(target_id=user.telegram_id, handle=handle)
    await state.set_state(GiftMonths.waiting_for_months)
    await message.answer(
        texts.ADMIN_GIFT_ASK_MONTHS.format(handle=handle, period=settings.subscription_period_days),
        reply_markup=_gift_months_keyboard(),
    )


async def _show_gift_confirmation(
    message: Message, state: FSMContext, session: AsyncSession, *, months: int
) -> None:
    """Quote the dates before and after, so a wrong number is visible rather than inferred."""
    data = await state.get_data()
    target_id = data.get("target_id")
    subscription = await subs.get_subscription(session, int(target_id))
    if subscription is None:
        await message.answer(texts.ADMIN_GIFT_NO_SUBSCRIPTION)
        await state.clear()
        return

    now = utcnow()
    new_until = subscription.expires_at
    for _ in range(months):
        new_until = extend(new_until, now=now, period_days=subscription.period_days)

    body = texts.ADMIN_GIFT_CONFIRM.format(
        handle=data.get("handle", ""),
        months=texts.months_phrase(months),
        status=texts.STATUS_NAMES.get(subscription.status.value, subscription.status.value),
        until=texts.day(subscription.expires_at),
        new_until=texts.day(new_until),
    )
    if subscription.expires_at <= now:
        body += texts.ADMIN_GIFT_CONFIRM_LAPSED

    await state.update_data(months=months)
    await state.set_state(GiftMonths.waiting_for_confirmation)
    await message.answer(body, reply_markup=_gift_confirm_keyboard())


async def choose_gift_months(
    query: CallbackQuery, state: FSMContext, session: AsyncSession
) -> None:
    months = int((query.data or "gift:months:1").rsplit(":", 1)[1])
    await query.answer()
    if query.message:
        await _show_gift_confirmation(query.message, state, session, months=months)


async def receive_gift_months(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """A typed number of months, for a length that is not on a button."""
    months = parse_months(message.text or "")
    if months is None:
        await message.answer(texts.ADMIN_BROADCAST_BAD_PERIOD.format(max_months=MAX_MONTHS))
        return
    await _show_gift_confirmation(message, state, session, months=months)


async def confirm_gift(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    data = await state.get_data()
    await state.clear()
    await query.answer()
    if query.message is None:
        return

    target_id = data.get("target_id")
    months = data.get("months")
    handle = data.get("handle", str(target_id))
    if target_id is None or not months:
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)
        return

    subscription = await subs.get_subscription(session, int(target_id))
    if subscription is None:
        await query.message.answer(texts.ADMIN_GIFT_NO_SUBSCRIPTION)
        return

    new_until = await subs.gift_months(
        session,
        subscription=subscription,
        months=months,
        actor_id=query.from_user.id,
        now=utcnow(),
    )
    # Committed before anybody is told: the extension is the thing that matters, and a failed
    # notification must not roll it back.
    await session.commit()

    try:
        await query.bot.send_message(
            int(target_id),
            texts.GIFT_RECEIVED.format(club=texts.CLUB_NAME, months=texts.months_phrase(months)),
        )
    except TelegramForbiddenError:
        log.warning("Gifted %s but they are unreachable", target_id)

    # The gift copy says «долучайся за посиланням», so the link has to follow it or the message
    # promises something that never arrives. Sent after the gift message, so it reads in order.
    if settings.channel_id:
        delivered, link = await subs.send_manual_invite(
            session,
            query.bot,
            user_id=int(target_id),
            actor_id=query.from_user.id,
            channel_id=settings.channel_id,
            now=utcnow(),
            manual=True,
        )
        await session.commit()
        if link is None:
            await query.message.answer(texts.ADMIN_INVITE_NO_LINK)
        elif not delivered:
            await query.message.answer(
                texts.ADMIN_INVITE_UNREACHABLE.format(handle=handle, link=link)
            )

    await query.message.answer(
        texts.ADMIN_GIFT_DONE.format(
            handle=handle, months=texts.months_phrase(months), new_until=texts.day(new_until)
        )
    )


def register(router: Router) -> None:
    """Attach to the gated admin router, so ``IsAdmin`` covers all of this too."""
    router.callback_query.register(start_invite, F.data == "admin:send_invite")
    router.message.register(
        cancel_invite, Command("cancel"), StateFilter(SendInvite.waiting_for_target)
    )
    router.message.register(
        cancel_invite, Command("cancel"), StateFilter(SendInvite.waiting_for_confirmation)
    )
    router.message.register(receive_target, StateFilter(SendInvite.waiting_for_target))
    router.callback_query.register(
        confirm_send, F.data == "invite:send", StateFilter(SendInvite.waiting_for_confirmation)
    )
    router.callback_query.register(cancel_from_button, F.data == "invite:cancel")

    router.callback_query.register(start_message, F.data == "admin:message_user")
    for state in (
        MessageUser.waiting_for_target,
        MessageUser.waiting_for_text,
        MessageUser.waiting_for_confirmation,
    ):
        router.message.register(cancel_invite, Command("cancel"), StateFilter(state))
    router.message.register(receive_message_target, StateFilter(MessageUser.waiting_for_target))
    router.message.register(receive_message_part, StateFilter(MessageUser.waiting_for_text))
    router.callback_query.register(
        finish_message, F.data == "compose:done", StateFilter(MessageUser.waiting_for_text)
    )
    router.callback_query.register(
        confirm_message, F.data == "msg:send", StateFilter(MessageUser.waiting_for_confirmation)
    )
    router.callback_query.register(cancel_from_button, F.data == "msg:cancel")

    router.callback_query.register(start_gift, F.data == "admin:gift_months")
    for state in (
        GiftMonths.waiting_for_target,
        GiftMonths.waiting_for_months,
        GiftMonths.waiting_for_confirmation,
    ):
        router.message.register(cancel_invite, Command("cancel"), StateFilter(state))
    router.message.register(receive_gift_target, StateFilter(GiftMonths.waiting_for_target))
    router.callback_query.register(
        choose_gift_months,
        F.data.startswith("gift:months:"),
        StateFilter(GiftMonths.waiting_for_months),
    )
    router.message.register(receive_gift_months, StateFilter(GiftMonths.waiting_for_months))
    router.callback_query.register(
        confirm_gift, F.data == "gift:send", StateFilter(GiftMonths.waiting_for_confirmation)
    )
    router.callback_query.register(cancel_from_button, F.data == "gift:cancel")
