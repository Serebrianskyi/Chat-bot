"""Two admin screens for talking to people: 📣 Розсилка to members, 📢 a post in the channel.

Both follow the same shape, and the shape is the point: **nothing is sent until the admin has
been shown exactly what will arrive.** A broadcast cannot be recalled, so the preview is the
last place a typo, a wrong audience or a half-written sentence can be caught.

📣 Розсилка — pick a group → write the text → see it rendered → send. The groups are the same
ones 👥 Учасники uses, so "53 people owe money" and "send to those 53" mean the same set.

For the group that owes money the member gets a **second** message straight after the admin's
text, carrying «Стати частиною клубу!» and their own payment link — the same thing a new joiner
is sent at ``/start``. The text on its own would be an advert with no way to act on it, which is
the whole reason that group is worth writing to.

📢 Написати в канал — the other direction: the bot publishes an admin's text in the private
channel, so the club can speak there as well as read.

Both record an ``audit_log`` row carrying the audience and the text (S6).
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import texts
from config import Settings
from db.models import Subscription, User, utcnow
from handlers.participants import GROUPS, classify
from services import broadcast as broadcast_service
from services.billing import BillingConfig
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)

#: Audiences that have not paid, and so are sent the payment link after the admin's text. TRIAL
#: is not here: a free period is running, nothing is owed yet, and a payment button would be
#: asking for money the club has not asked for.
AUDIENCES_OWED_PAYMENT = frozenset({"unpaid"})


class Broadcast(StatesGroup):
    waiting_for_audience = State()
    waiting_for_text = State()
    waiting_for_confirmation = State()


class ChannelPost(StatesGroup):
    waiting_for_text = State()
    waiting_for_confirmation = State()


def _audience_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{label} — {counts[key]}", callback_data=f"bcast:{key}")]
            for key, label, _action in GROUPS
            if counts.get(key)
        ]
    )


def _confirm_keyboard(yes_text: str, yes_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=yes_text, callback_data=yes_data)],
            [InlineKeyboardButton(text=texts.ADMIN_INVITE_CONFIRM_NO, callback_data="bcast:no")],
        ]
    )


async def _members_of(session: AsyncSession, key: str) -> list[int]:
    """Telegram ids of everyone in one group, resolved at the moment the admin is shown a count."""
    rows = (
        await session.execute(
            select(User, Subscription).outerjoin(
                Subscription, Subscription.user_id == User.telegram_id
            )
        )
    ).all()
    now = utcnow()
    return [
        user.telegram_id for user, subscription in rows if classify(subscription, now=now) == key
    ]


# --- 📣 Розсилка -----------------------------------------------------------------------------


async def start_broadcast(query: CallbackQuery, session: AsyncSession) -> None:
    rows = (
        await session.execute(
            select(User, Subscription).outerjoin(
                Subscription, Subscription.user_id == User.telegram_id
            )
        )
    ).all()
    now = utcnow()
    counts: dict[str, int] = {key: 0 for key, _, _ in GROUPS}
    for _user, subscription in rows:
        counts[classify(subscription, now=now)] += 1

    await query.answer()
    if query.message is None:
        return
    await query.message.answer(
        texts.ADMIN_BROADCAST_ASK_AUDIENCE, reply_markup=_audience_keyboard(counts)
    )


async def choose_audience(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    key = (query.data or "").removeprefix("bcast:")
    label = next((lbl for k, lbl, _ in GROUPS if k == key), key)

    await query.answer()
    if query.message is None:
        return

    recipients = await _members_of(session, key)
    if not recipients:
        await query.message.answer(texts.ADMIN_BROADCAST_NO_AUDIENCE)
        return

    await state.update_data(audience=key, label=label, recipients=recipients)
    await state.set_state(Broadcast.waiting_for_text)
    await query.message.answer(
        texts.ADMIN_BROADCAST_ASK_TEXT.format(label=label, count=len(recipients))
    )


async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.ADMIN_GRANT_CANCELLED)


async def receive_broadcast_text(message: Message, state: FSMContext) -> None:
    """Show the admin the message as the member will receive it, then ask."""
    body = (message.text or "").strip()
    if not body:
        await message.answer(texts.ADMIN_BROADCAST_EMPTY)
        return

    data = await state.get_data()
    recipients = data.get("recipients", [])
    await state.update_data(body=body)
    await state.set_state(Broadcast.waiting_for_confirmation)

    # Three messages, in the order the member will see them: the frame, the text itself exactly
    # as it will arrive, then what follows it. Quoting the text inside a bigger message would
    # change how it looks, which defeats the purpose of a preview.
    await message.answer(texts.ADMIN_BROADCAST_PREVIEW.format(count=len(recipients)))
    await message.answer(body)

    if data.get("audience") in AUDIENCES_OWED_PAYMENT:
        tail = texts.ADMIN_BROADCAST_PREVIEW_WITH_PAY.format(button=texts.JOIN_CLUB_BUTTON)
    else:
        tail = texts.ADMIN_BROADCAST_PREVIEW_PLAIN
    await message.answer(
        tail,
        reply_markup=_confirm_keyboard(texts.ADMIN_BROADCAST_CONFIRM_YES, "bcast:send"),
    )


async def confirm_broadcast(
    query: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    wayforpay: WayForPayClient | None,
    billing_config: BillingConfig,
    session_factory: async_sessionmaker,
) -> None:
    data = await state.get_data()
    await state.clear()
    await query.answer()
    if query.message is None:
        return

    recipients: list[int] = data.get("recipients", [])
    body = data.get("body")
    audience = data.get("audience", "")
    if not recipients or not body:
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)
        return

    with_invoice = audience in AUDIENCES_OWED_PAYMENT
    await query.message.answer(texts.ADMIN_BROADCAST_STARTED.format(count=len(recipients)))

    # The session from the middleware is not used for the send: a broadcast outlives one unit of
    # work, and the service opens a session per member so one failure cannot undo the rest.
    result = await broadcast_service.send_broadcast(
        session_factory,
        query.bot,
        user_ids=recipients,
        text=body,
        actor_id=query.from_user.id,
        audience=audience,
        with_invoice=with_invoice,
        client=wayforpay if with_invoice else None,
        config=billing_config if with_invoice else None,
    )

    await query.message.answer(texts.ADMIN_BROADCAST_DONE.format(**result.as_counts()))
    log.info(
        "Admin %s broadcast to %s (%d recipients): %s",
        query.from_user.id,
        audience,
        len(recipients),
        result.as_counts(),
    )


# --- 📢 Написати в канал ---------------------------------------------------------------------


async def start_channel_post(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ChannelPost.waiting_for_text)
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_CHANNEL_ASK_TEXT.format(club=texts.CLUB_NAME))


async def receive_channel_text(message: Message, state: FSMContext) -> None:
    body = (message.text or "").strip()
    if not body:
        await message.answer(texts.ADMIN_BROADCAST_EMPTY)
        return

    await state.update_data(body=body)
    await state.set_state(ChannelPost.waiting_for_confirmation)
    await message.answer(texts.ADMIN_CHANNEL_PREVIEW)
    await message.answer(body)
    await message.answer(
        texts.ADMIN_CHANNEL_PREVIEW_FOOTER,
        reply_markup=_confirm_keyboard(texts.ADMIN_CHANNEL_CONFIRM_YES, "post:send"),
    )


async def confirm_channel_post(
    query: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    session_factory: async_sessionmaker,
) -> None:
    data = await state.get_data()
    await state.clear()
    await query.answer()
    if query.message is None:
        return

    body = data.get("body")
    if not body:
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)
        return
    if not settings.channel_id:
        await query.message.answer(texts.ADMIN_CHANNEL_NO_ID)
        return

    posted = await broadcast_service.post_to_channel(
        session_factory,
        query.bot,
        channel_id=settings.channel_id,
        text=body,
        actor_id=query.from_user.id,
    )
    await query.message.answer(texts.ADMIN_CHANNEL_SENT if posted else texts.ADMIN_CHANNEL_FAILED)


async def cancel_from_button(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)


def register(router: Router) -> None:
    """Attach to the gated admin router, so ``IsAdmin`` covers all of this too."""
    router.callback_query.register(start_broadcast, F.data == "admin:broadcast")
    router.callback_query.register(start_channel_post, F.data == "admin:channel_post")

    for group in Broadcast.__all_states__ + ChannelPost.__all_states__:
        router.message.register(cancel, Command("cancel"), StateFilter(group))

    for key, _label, _action in GROUPS:
        router.callback_query.register(choose_audience, F.data == f"bcast:{key}")
    router.message.register(receive_broadcast_text, StateFilter(Broadcast.waiting_for_text))
    router.callback_query.register(
        confirm_broadcast, F.data == "bcast:send", StateFilter(Broadcast.waiting_for_confirmation)
    )

    router.message.register(receive_channel_text, StateFilter(ChannelPost.waiting_for_text))
    router.callback_query.register(
        confirm_channel_post,
        F.data == "post:send",
        StateFilter(ChannelPost.waiting_for_confirmation),
    )
    router.callback_query.register(cancel_from_button, F.data == "bcast:no")
