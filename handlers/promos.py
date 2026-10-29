"""Promo codes: a member redeems one, an admin creates and stops them.

**Two routers, deliberately.** Redeeming is a member action and must not sit behind the admin
gate; creating is an admin action and must. They live in one module because they are one feature
and the copy is shared, but ``build_member_router`` goes on the open dispatcher and
``register_admin`` attaches to the gated admin router.

The member reaches it three ways, because a code nobody can find is worthless:

* **«🎟 У мене є промокод»** on the ``/start`` tariff message and on ``/subscription`` — on
  screen exactly when price is being discussed;
* **``/promo``** for somebody told the code elsewhere;
* typing the code **only while the FSM is waiting for one**. Never a global text handler: that
  would swallow every message a member sends and break every other flow in the bot.
"""

import logging
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from config import Settings
from db.models import DiscountKind, utcnow
from services import billing, promos
from services import discounts as discount_service
from services.billing import BillingConfig
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)


class RedeemPromo(StatesGroup):
    waiting_for_code = State()


class CreatePromo(StatesGroup):
    waiting_for_code = State()
    waiting_for_price = State()
    waiting_for_window = State()
    waiting_for_months = State()
    waiting_for_audience = State()
    waiting_for_confirmation = State()


#: Maps a PromoError's reason to what the member reads. The service raises short English reasons
#: so it stays testable without copy; the translation lives here, with the rest of the wording.
REFUSALS = {
    "promo revoked": texts.PROMO_EXPIRED,
    "promo expired": texts.PROMO_EXPIRED,
    "promo exhausted": texts.PROMO_EXHAUSTED,
    "already redeemed": texts.PROMO_USED,
    "another promo is still running": texts.PROMO_ANOTHER_RUNNING,
    "not for members who already have a discount": texts.PROMO_NEW_ONLY,
    "not for members who have already paid": texts.PROMO_NEW_ONLY,
}


# --- the member side -------------------------------------------------------------------------


async def ask_for_code(event: Message | CallbackQuery, state: FSMContext) -> None:
    """Start the walk from either the button or ``/promo``."""
    await state.clear()
    await state.set_state(RedeemPromo.waiting_for_code)
    if isinstance(event, CallbackQuery):
        await event.answer()
        if event.message:
            await event.message.answer(texts.PROMO_ASK_CODE)
        return
    await event.answer(texts.PROMO_ASK_CODE)


async def cancel_redeem(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(texts.ADMIN_GRANT_CANCELLED)


async def receive_code(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    wayforpay: WayForPayClient | None = None,
    billing_config: BillingConfig | None = None,
) -> None:
    """Redeem what the member typed, and tell them what it bought.

    The reply differs by whether they owe money **now**: somebody already paid up is told when
    the price will apply, while somebody due immediately gets a pay button at the new price —
    quoting a future date to them would be wrong, and leaving them without a link would waste
    the moment they are most likely to pay.
    """
    if message.from_user is None:
        return
    user_id = message.from_user.id
    now = utcnow()

    promo = await promos.by_code(session, message.text or "")
    if promo is None:
        await message.answer(texts.PROMO_UNKNOWN)
        return

    from services import subscriptions as subs

    subscription = await subs.get_subscription(session, user_id)
    if subscription is None:
        # Nothing to apply a price to. `/start` creates the row, and its own pricing decision
        # depends on whether they were already in the channel — not something to guess here.
        await state.clear()
        await message.answer(texts.PROMO_NO_SUBSCRIPTION)
        return

    # What they pay today, before the promo, so the reply can show both figures.
    was, was_currency, _ = await discount_service.effective_price(
        session, subscription=subscription, username=message.from_user.username, now=now
    )

    try:
        await promos.redeem(session, promo=promo, user_id=user_id, now=now)
    except promos.PromoError as exc:
        await state.clear()
        await message.answer(REFUSALS.get(str(exc), texts.PROMO_UNKNOWN))
        return

    await session.commit()
    await state.clear()

    now_price, currency, _ = await discount_service.effective_price(
        session, subscription=subscription, username=message.from_user.username, now=now
    )
    months = texts.months_phrase(promo.discount_months)

    if subscription.expires_at > now:
        await message.answer(
            texts.PROMO_REDEEMED.format(
                amount=texts.money(now_price, currency),
                was=texts.money(was, was_currency),
                months=months,
                until=texts.day(subscription.expires_at),
            )
        )
        return

    # They owe money now, so the promo applies to the payment in front of them. A fresh invoice
    # is issued because any open one quotes the old price.
    payment = None
    if wayforpay is not None:
        payment = await billing.ensure_invoice(
            session,
            wayforpay,
            subscription=subscription,
            config=billing_config or BillingConfig(period_days=subscription.period_days),
            now=now,
            reuse_open=False,
        )
        await session.commit()

    await message.answer(
        texts.PROMO_REDEEMED_NOW.format(
            amount=texts.money(now_price, currency),
            was=texts.money(was, was_currency),
            months=months,
        ),
        reply_markup=(
            InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.PAY_BUTTON.format(club=texts.CLUB_NAME),
                            url=payment.invoice_url,
                        )
                    ]
                ]
            )
            if payment and payment.invoice_url
            else None
        ),
    )


def build_member_router() -> Router:
    """Open to everyone: redeeming is a member action, not an admin one."""
    router = Router(name="promos-member")
    router.message.register(ask_for_code, Command("promo"))
    router.callback_query.register(ask_for_code, F.data == "menu:promo")
    router.message.register(
        cancel_redeem, Command("cancel"), StateFilter(RedeemPromo.waiting_for_code)
    )
    router.message.register(receive_code, StateFilter(RedeemPromo.waiting_for_code))
    return router


# --- the admin side -------------------------------------------------------------------------


def _percent_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"−{percent}%", callback_data=f"promo:pc:{percent}")
                for percent in promos.PERCENT_CHOICES
            ]
        ]
    )


def _months_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.months_phrase(months), callback_data=f"{prefix}:{months}"
                )
            ]
            for months in promos.MONTH_CHOICES
        ]
    )


def _audience_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_PROMO_AUDIENCE_NEW, callback_data="promo:aud:new"
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_PROMO_AUDIENCE_ALL, callback_data="promo:aud:all"
                )
            ],
        ]
    )


async def show_promos(query: CallbackQuery, session: AsyncSession) -> None:
    """Live campaigns, with how many people have used each."""
    now = utcnow()
    live = await promos.list_live(session, now=now)

    await query.answer()
    if query.message is None:
        return
    if not live:
        await query.message.answer(texts.ADMIN_PROMOS_EMPTY)
        return

    lines = [texts.ADMIN_PROMOS_HEADER.format(count=len(live))]
    rows: list[list[InlineKeyboardButton]] = []
    for promo in live:
        price = (
            f"−{promo.percent_off}%"
            if promo.kind is DiscountKind.PERCENT
            else texts.money(promo.fixed_price or Decimal(0), promo.currency)
        )
        lines.append(
            texts.ADMIN_PROMO_LINE.format(
                code=promo.code,
                price=price,
                months=texts.months_phrase(promo.discount_months),
                until=texts.day(promo.redeemable_until),
                used=promo.redemptions,
                cap=(
                    texts.ADMIN_PROMO_CAP.format(max=promo.max_redemptions)
                    if promo.max_redemptions
                    else ""
                ),
            )
            + (texts.ADMIN_PROMO_NEW_ONLY_MARK if promo.new_members_only else "")
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_PROMO_REVOKE_BUTTON.format(code=promo.code),
                    callback_data=f"promo:revoke:{promo.id}",
                )
            ]
        )

    await query.message.answer(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


async def revoke_promo(query: CallbackQuery, session: AsyncSession) -> None:
    promo_id = int((query.data or "promo:revoke:0").rsplit(":", 1)[1])
    from db.models import PromoCode

    promo = await session.get(PromoCode, promo_id)
    await query.answer()
    if query.message is None or promo is None:
        return

    await promos.revoke(session, promo=promo, actor_id=query.from_user.id, now=utcnow())
    await session.commit()
    await query.message.answer(texts.ADMIN_PROMO_REVOKED.format(code=promo.code))


async def start_create(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CreatePromo.waiting_for_code)
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_PROMO_ASK_CODE)


async def receive_new_code(message: Message, state: FSMContext, session: AsyncSession) -> None:
    code = promos.normalise_code(message.text or "")
    if not promos.looks_like_code(code):
        await message.answer(texts.ADMIN_PROMO_BAD_CODE)
        return
    if await promos.by_code(session, code) is not None:
        await message.answer(texts.ADMIN_PROMO_EXISTS)
        return

    await state.update_data(code=code)
    await state.set_state(CreatePromo.waiting_for_price)
    await message.answer(
        texts.ADMIN_PROMO_ASK_PRICE.format(code=code), reply_markup=_percent_keyboard()
    )


async def _ask_window(message: Message, state: FSMContext) -> None:
    await state.set_state(CreatePromo.waiting_for_window)
    await message.answer(texts.ADMIN_PROMO_ASK_WINDOW, reply_markup=_months_keyboard("promo:win"))


async def choose_percent(query: CallbackQuery, state: FSMContext) -> None:
    percent = int((query.data or "promo:pc:10").rsplit(":", 1)[1])
    await state.update_data(percent_off=percent, fixed_price=None)
    await query.answer()
    if query.message:
        await _ask_window(query.message, state)


async def receive_price(message: Message, state: FSMContext, settings: Settings) -> None:
    """A typed sum (``8``) or percentage (``15%``), for anything not on a button."""
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
        await message.answer(texts.ADMIN_PROMO_BAD_PRICE)
        return

    await state.update_data(
        percent_off=percent,
        fixed_price=None if fixed is None else str(fixed),
        currency=settings.subscription_currency,
    )
    await _ask_window(message, state)


async def choose_window(query: CallbackQuery, state: FSMContext) -> None:
    window = int((query.data or "promo:win:1").rsplit(":", 1)[1])
    await state.update_data(window_months=window)
    await state.set_state(CreatePromo.waiting_for_months)
    await query.answer()
    if query.message:
        await query.message.answer(
            texts.ADMIN_PROMO_ASK_MONTHS.format(window=texts.months_phrase(window)),
            reply_markup=_months_keyboard("promo:mon"),
        )


async def choose_months(query: CallbackQuery, state: FSMContext) -> None:
    months = int((query.data or "promo:mon:1").rsplit(":", 1)[1])
    await state.update_data(discount_months=months)
    await state.set_state(CreatePromo.waiting_for_audience)
    await query.answer()
    if query.message:
        await query.message.answer(
            texts.ADMIN_PROMO_ASK_AUDIENCE, reply_markup=_audience_keyboard()
        )


async def choose_audience(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    new_only = (query.data or "").endswith("new")
    await state.update_data(new_members_only=new_only)
    data = await state.get_data()
    await state.set_state(CreatePromo.waiting_for_confirmation)
    await query.answer()
    if query.message is None:
        return

    price = (
        f"−{data['percent_off']}%"
        if data.get("percent_off")
        else texts.money(
            Decimal(data["fixed_price"]), data.get("currency", settings.subscription_currency)
        )
    )
    await query.message.answer(
        texts.ADMIN_PROMO_CONFIRM.format(
            code=data["code"],
            price=price,
            window=texts.months_phrase(data["window_months"]),
            months=texts.months_phrase(data["discount_months"]),
            audience=(
                texts.ADMIN_PROMO_AUDIENCE_NEW if new_only else texts.ADMIN_PROMO_AUDIENCE_ALL
            ),
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=texts.ADMIN_PROMO_CONFIRM_YES, callback_data="promo:create"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=texts.ADMIN_INVITE_CONFIRM_NO, callback_data="promo:cancel"
                    )
                ],
            ]
        ),
    )


async def confirm_create(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    data = await state.get_data()
    await state.clear()
    await query.answer()
    if query.message is None:
        return

    fixed = data.get("fixed_price")
    try:
        promo = await promos.create(
            session,
            actor_id=query.from_user.id,
            code=data["code"],
            kind=DiscountKind.PERCENT if data.get("percent_off") else DiscountKind.FIXED_PRICE,
            percent_off=data.get("percent_off"),
            fixed_price=None if fixed is None else Decimal(fixed),
            currency=data.get("currency", settings.subscription_currency),
            discount_months=data["discount_months"],
            window_months=data["window_months"],
            new_members_only=data.get("new_members_only", False),
            now=utcnow(),
        )
    except promos.PromoError as exc:
        log.warning("Could not create promo: %s", exc)
        await query.message.answer(texts.ADMIN_PROMO_EXISTS)
        return

    await session.commit()
    await query.message.answer(
        texts.ADMIN_PROMO_CREATED.format(code=promo.code, until=texts.day(promo.redeemable_until))
    )


async def cancel_create(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)


def register_admin(router: Router) -> None:
    """Attach to the gated admin router, so ``IsAdmin`` covers creating and stopping codes."""
    router.callback_query.register(show_promos, F.data == "admin:promos")
    router.callback_query.register(start_create, F.data == "admin:create_promo")
    router.callback_query.register(revoke_promo, F.data.startswith("promo:revoke:"))

    for state in (
        CreatePromo.waiting_for_code,
        CreatePromo.waiting_for_price,
        CreatePromo.waiting_for_window,
        CreatePromo.waiting_for_months,
        CreatePromo.waiting_for_audience,
        CreatePromo.waiting_for_confirmation,
    ):
        router.message.register(cancel_redeem, Command("cancel"), StateFilter(state))

    router.message.register(receive_new_code, StateFilter(CreatePromo.waiting_for_code))
    router.callback_query.register(
        choose_percent, F.data.startswith("promo:pc:"), StateFilter(CreatePromo.waiting_for_price)
    )
    router.message.register(receive_price, StateFilter(CreatePromo.waiting_for_price))
    router.callback_query.register(
        choose_window, F.data.startswith("promo:win:"), StateFilter(CreatePromo.waiting_for_window)
    )
    router.callback_query.register(
        choose_months, F.data.startswith("promo:mon:"), StateFilter(CreatePromo.waiting_for_months)
    )
    router.callback_query.register(
        choose_audience,
        F.data.startswith("promo:aud:"),
        StateFilter(CreatePromo.waiting_for_audience),
    )
    router.callback_query.register(
        confirm_create, F.data == "promo:create", StateFilter(CreatePromo.waiting_for_confirmation)
    )
    router.callback_query.register(cancel_create, F.data == "promo:cancel")
