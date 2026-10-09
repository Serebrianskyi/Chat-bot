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
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import texts
from config import Settings
from db.models import DiscountKind, Subscription, User, utcnow
from handlers.compose import read_composed
from handlers.participants import GROUPS, classify
from services import broadcast as broadcast_service
from services import discounts as discount_service
from services.billing import BillingConfig
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)

#: How many months a special price can be made to last. Months, not days: a subscription is
#: billed monthly, so "three months" is what the club decides and `months × period_days` is the
#: arithmetic that follows from it rather than a number the admin has to work out.
MONTH_CHOICES = (1, 2, 3, 6, 12)

#: Upper bound on a typed number of months. The buttons cover the usual offers; typing exists
#: for the one that is not on them, not for a price that outlives the club.
MAX_MONTHS = 60


def parse_months(raw: str) -> int | None:
    """``"4"`` -> ``4``. None for anything that is not a usable number of months.

    Bounded at both ends: ``0`` is not "no limit" — that is its own button — and an unbounded
    number would set a price running for centuries off one typo.
    """
    digits = raw.strip()
    if not digits.isdigit():
        return None
    months = int(digits)
    return months if 1 <= months <= MAX_MONTHS else None


#: Audiences that have not paid, and so are sent the payment link after the admin's text. TRIAL
#: is not here: a free period is running, nothing is owed yet, and a payment button would be
#: asking for money the club has not asked for.
AUDIENCES_OWED_PAYMENT = frozenset({"unpaid"})


class Broadcast(StatesGroup):
    waiting_for_audience = State()
    waiting_for_text = State()
    waiting_for_price = State()
    waiting_for_amount = State()
    waiting_for_period = State()
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

    now = utcnow()
    recipients = await _members_of(session, key)
    if not recipients:
        await query.message.answer(texts.ADMIN_BROADCAST_NO_AUDIENCE)
        return

    # How many of them already hold a discount, so the preview can warn that a special price
    # would replace it. Counted here, against the same recipient list the admin is shown.
    live = await discount_service.list_active(session, now=now)
    chosen = set(recipients)
    already_discounted = len({d.user_id for d in live if d.user_id in chosen})

    await state.update_data(
        audience=key,
        label=label,
        recipients=recipients,
        already_discounted=already_discounted,
    )
    await state.set_state(Broadcast.waiting_for_text)
    await query.message.answer(
        texts.ADMIN_BROADCAST_ASK_TEXT.format(label=label, count=len(recipients))
    )


async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.ADMIN_GRANT_CANCELLED)


def _price_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_BROADCAST_PRICE_REGULAR, callback_data="bprice:regular"
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_BROADCAST_PRICE_SPECIAL, callback_data="bprice:special"
                )
            ],
        ]
    )


def _period_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=texts.ADMIN_BROADCAST_PERIOD_MONTHS.format(months=texts.months_phrase(months)),
                callback_data=f"bperiod:{months}",
            )
        ]
        for months in MONTH_CHOICES
    ]
    rows.append(
        [InlineKeyboardButton(text=texts.ADMIN_BROADCAST_PERIOD_FOREVER, callback_data="bperiod:0")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def receive_broadcast_text(message: Message, state: FSMContext) -> None:
    """Hold the text, then ask what the payment link should charge.

    The audiences that are not sent a payment link skip straight to the preview: there is no
    price to configure when no invoice is being issued.
    """
    body, photo = read_composed(message)
    # An image on its own is a valid post; a message with neither words nor picture is not.
    if not body and photo is None:
        await message.answer(texts.ADMIN_BROADCAST_EMPTY)
        return

    await state.update_data(body=body, photo=photo)
    data = await state.get_data()

    if data.get("audience") in AUDIENCES_OWED_PAYMENT:
        await state.set_state(Broadcast.waiting_for_price)
        await message.answer(texts.ADMIN_BROADCAST_ASK_PRICE, reply_markup=_price_keyboard())
        return

    await _show_preview(message, state)


async def choose_price(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await query.answer()
    if query.message is None:
        return
    if (query.data or "").endswith("regular"):
        await _show_preview(query.message, state)
        return
    await state.set_state(Broadcast.waiting_for_amount)
    await query.message.answer(texts.ADMIN_BROADCAST_ASK_AMOUNT)


async def receive_amount(message: Message, state: FSMContext, settings: Settings) -> None:
    """Accept either a sum (``8``) or a percentage (``20%``).

    One step rather than a kind button followed by a number: the ``%`` makes the two forms
    unambiguous, and an admin writing an offer already thinks in one or the other.
    """
    raw = (message.text or "").strip().replace(",", ".")
    percent: int | None = None
    fixed: Decimal | None = None

    if raw.endswith("%"):
        digits = raw[:-1].strip()
        if digits.isdigit() and 1 <= int(digits) <= 100:
            percent = int(digits)
    else:
        try:
            value = Decimal(raw)
        except InvalidOperation:
            value = None
        if value is not None and value > 0:
            fixed = value

    if percent is None and fixed is None:
        await message.answer(texts.ADMIN_BROADCAST_BAD_AMOUNT)
        return

    await state.update_data(
        percent_off=percent,
        fixed_price=None if fixed is None else str(fixed),
        currency=settings.subscription_currency,
    )
    await state.set_state(Broadcast.waiting_for_period)
    await message.answer(
        texts.ADMIN_BROADCAST_ASK_PERIOD.format(period=settings.subscription_period_days),
        reply_markup=_period_keyboard(),
    )


async def _apply_months(
    message: Message, state: FSMContext, *, months: int | None, settings: Settings
) -> None:
    """Store the duration and move to the preview. ``months=None`` means no end date.

    ``Discount.valid_until`` is computed from days, so the conversion happens once, here, using
    the club's own billing period rather than a calendar month — the price has to cover whole
    billing periods or a member's last month would be charged at the old amount.
    """
    await state.update_data(
        months=months,
        days=months * settings.subscription_period_days if months else None,
    )
    await _show_preview(message, state)


async def choose_period(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    """A tapped button. ``bperiod:0`` is the "no limit" choice."""
    months = int((query.data or "bperiod:0").removeprefix("bperiod:"))
    await query.answer()
    if query.message:
        await _apply_months(query.message, state, months=months or None, settings=settings)


async def receive_period_months(message: Message, state: FSMContext, settings: Settings) -> None:
    """A typed number of months, for the duration that is not on a button."""
    months = parse_months(message.text or "")
    if months is None:
        await message.answer(texts.ADMIN_BROADCAST_BAD_PERIOD.format(max_months=MAX_MONTHS))
        return
    await _apply_months(message, state, months=months, settings=settings)


def _offer_from(data: dict) -> broadcast_service.PriceOffer | None:
    """The configured price, or None when the regular one is being used."""
    if data.get("percent_off") is None and data.get("fixed_price") is None:
        return None
    fixed = data.get("fixed_price")
    return broadcast_service.PriceOffer(
        kind=DiscountKind.PERCENT if data.get("percent_off") else DiscountKind.FIXED_PRICE,
        percent_off=data.get("percent_off"),
        fixed_price=None if fixed is None else Decimal(fixed),
        currency=data.get("currency", "EUR"),
        days=data.get("days"),
        note=texts.ADMIN_BROADCAST_NOTE,
    )


async def _show_preview(message: Message, state: FSMContext) -> None:
    """Everything the admin needs to decide, in the order the member will see it."""
    data = await state.get_data()
    body = data.get("body", "")
    recipients = data.get("recipients", [])
    await state.set_state(Broadcast.waiting_for_confirmation)

    # Three messages, in the order the member will see them: the frame, the text itself exactly
    # as it will arrive, then what follows it. Quoting the text inside a bigger message would
    # change how it looks, which defeats the purpose of a preview.
    await message.answer(texts.ADMIN_BROADCAST_PREVIEW.format(count=len(recipients)))
    photo = data.get("photo")
    if photo is not None:
        await message.answer_photo(photo, caption=body or None)
    else:
        await message.answer(body)

    if data.get("audience") not in AUDIENCES_OWED_PAYMENT:
        await message.answer(
            texts.ADMIN_BROADCAST_PREVIEW_PLAIN,
            reply_markup=_confirm_keyboard(texts.ADMIN_BROADCAST_CONFIRM_YES, "bcast:send"),
        )
        return

    offer = _offer_from(data)
    tail = texts.ADMIN_BROADCAST_PREVIEW_WITH_PAY.format(button=texts.JOIN_CLUB_BUTTON)
    if offer is None:
        tail += "\n" + texts.ADMIN_BROADCAST_PRICE_LINE_REGULAR
    else:
        price = (
            f"−{offer.percent_off}%"
            if offer.percent_off
            else texts.money(offer.fixed_price, offer.currency)
        )
        months = data.get("months")
        validity = (
            texts.ADMIN_BROADCAST_VALIDITY_FOREVER
            if months is None
            else texts.ADMIN_BROADCAST_VALIDITY_MONTHS.format(months=texts.months_phrase(months))
        )
        tail += "\n" + texts.ADMIN_BROADCAST_PRICE_LINE_SPECIAL.format(
            price=price, validity=validity
        )
        # One active discount per person, so this price replaces whatever they had. Said before
        # sending, because silently overwriting a promised price is the costly mistake here.
        replaced = data.get("already_discounted", 0)
        if replaced:
            tail += texts.ADMIN_BROADCAST_REPLACES_WARNING.format(count=replaced)

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
    body = data.get("body", "")
    audience = data.get("audience", "")
    if not recipients or not (body or data.get("photo")):
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
        photo=data.get("photo"),
        actor_id=query.from_user.id,
        audience=audience,
        with_invoice=with_invoice,
        offer=_offer_from(data) if with_invoice else None,
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
    body, photo = read_composed(message)
    if not body and photo is None:
        await message.answer(texts.ADMIN_BROADCAST_EMPTY)
        return

    await state.update_data(body=body, photo=photo)
    await state.set_state(ChannelPost.waiting_for_confirmation)
    await message.answer(texts.ADMIN_CHANNEL_PREVIEW)
    # Shown the way it will appear in the channel, image and all — a quoted description of a
    # picture is not a preview of it.
    if photo is not None:
        await message.answer_photo(photo, caption=body or None)
    else:
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

    body = data.get("body", "")
    if not (body or data.get("photo")):
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
        photo=data.get("photo"),
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
        choose_price, F.data.startswith("bprice:"), StateFilter(Broadcast.waiting_for_price)
    )
    router.message.register(receive_amount, StateFilter(Broadcast.waiting_for_amount))
    router.callback_query.register(
        choose_period, F.data.startswith("bperiod:"), StateFilter(Broadcast.waiting_for_period)
    )
    router.message.register(receive_period_months, StateFilter(Broadcast.waiting_for_period))
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
