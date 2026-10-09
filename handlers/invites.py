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
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from config import Settings
from db.models import SubscriptionStatus, User, normalise_username, utcnow
from handlers.compose import read_composed
from services import subscriptions as subs

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
        until = f", до {texts.day(subscription.expires_at.date())}"
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


async def receive_message_text(message: Message, state: FSMContext) -> None:
    """Hold what the admin composed and show it back before it is sent.

    A message to a member cannot be unsent, so it is shown for confirmation exactly as it will
    arrive — the one chance to catch a wrong recipient or a half-typed sentence. An attached
    image is shown as an image, not described.
    """
    body, photo = read_composed(message)
    if not body and photo is None:
        await message.answer(texts.ADMIN_MESSAGE_EMPTY)
        return

    data = await state.get_data()
    await state.update_data(body=body, photo=photo)
    await state.set_state(MessageUser.waiting_for_confirmation)
    if photo is not None:
        await message.answer_photo(photo, caption=body or None)
    await message.answer(
        texts.ADMIN_MESSAGE_CONFIRM.format(handle=data.get("handle", ""), preview=body or "—"),
        reply_markup=_send_keyboard(),
    )


async def confirm_message(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    await state.clear()
    await query.answer()
    if query.message is None:
        return

    target_id = data.get("target_id")
    body = data.get("body", "")
    handle = data.get("handle", str(target_id))
    if target_id is None or not (body or data.get("photo")):
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)
        return

    delivered = await subs.send_admin_message(
        session,
        query.bot,
        user_id=int(target_id),
        actor_id=query.from_user.id,
        body=body,
        photo=data.get("photo"),
    )
    await session.commit()

    if delivered:
        await query.message.answer(texts.ADMIN_MESSAGE_SENT.format(handle=handle))
    else:
        await query.message.answer(texts.ADMIN_MESSAGE_UNREACHABLE.format(handle=handle))


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
    router.message.register(receive_message_text, StateFilter(MessageUser.waiting_for_text))
    router.callback_query.register(
        confirm_message, F.data == "msg:send", StateFilter(MessageUser.waiting_for_confirmation)
    )
    router.callback_query.register(cancel_from_button, F.data == "msg:cancel")
