"""Admin screens for discounts: listing them, and granting one step by step. Phase 2A.

Two entry points, both behind the ``IsAdmin`` gate on the admin router:

* **🎟 Знижки** — every discount in force, with who has not yet activated the bot.
* **🎁 Надати знижку** — an FSM walk: who → percent or fixed → for how long → note → confirm.

The walk asks one thing per message because the alternative is a command with positional
arguments that an admin has to remember. Every step accepts ``/cancel``, and the confirmation step
shows the resulting price so a mistyped percentage is visible before it is applied.

Granting writes an ``audit_log`` row (S6) — ``services.discounts.grant`` does that.
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
from db.models import Discount, DiscountKind, looks_like_username, normalise_username, utcnow
from services import discounts as discount_service

log = logging.getLogger(__name__)

#: Offered as period buttons. "Without limit" is separate.
PERIOD_CHOICES = (30, 90, 180, 365)


class GrantDiscount(StatesGroup):
    """One state per question. ``/cancel`` clears the whole thing from any of them."""

    waiting_for_target = State()
    waiting_for_kind = State()
    waiting_for_percent = State()
    waiting_for_fixed_price = State()
    waiting_for_period = State()
    waiting_for_note = State()
    waiting_for_confirmation = State()


def describe(discount: Discount) -> str:
    """«20%» or «8 €» — how a discount reads in a list."""
    if discount.kind is DiscountKind.PERCENT:
        return f"{discount.percent_off}%"
    return texts.money(discount.fixed_price or Decimal(0), discount.currency)


def _describe_pending(
    kind: DiscountKind, percent: int | None, price: Decimal | None, currency: str
) -> str:
    if kind is DiscountKind.PERCENT:
        return f"{percent}%"
    return texts.money(price or Decimal(0), currency)


# --- listing ---------------------------------------------------------------------------------


async def show_discounts(query: CallbackQuery, session: AsyncSession) -> None:
    """Every live discount, newest first."""
    now = utcnow()
    active = await discount_service.list_active(session, now=now)

    if not active:
        await query.answer()
        if query.message:
            await query.message.answer(texts.ADMIN_DISCOUNTS_EMPTY)
        return

    lines = [texts.ADMIN_DISCOUNTS_HEADER.format(count=len(active))]
    for discount in active:
        until = (
            texts.ADMIN_DISCOUNT_UNTIL.format(until=texts.day(discount.valid_until))
            if discount.valid_until
            else texts.ADMIN_DISCOUNT_FOREVER
        )
        claimed = "" if discount.user_id is not None else texts.ADMIN_DISCOUNT_NOT_CLAIMED
        note = f" — {discount.note}" if discount.note else ""
        lines.append(
            texts.ADMIN_DISCOUNT_LINE.format(
                who=discount.target,
                what=describe(discount),
                until=until,
                claimed=claimed,
                note=note,
            )
        )

    unclaimed = sum(1 for d in active if d.user_id is None)
    if unclaimed:
        lines.append(texts.ADMIN_DISCOUNTS_UNCLAIMED_NOTE.format(count=unclaimed))

    await query.answer()
    if query.message:
        await query.message.answer("\n".join(lines))


# --- granting --------------------------------------------------------------------------------


async def start_grant(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(GrantDiscount.waiting_for_target)
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_GRANT_ASK_WHO)


async def cancel_grant(message: Message, state: FSMContext) -> None:
    """``/cancel`` from any step. Clears the state so nothing partial is left behind."""
    await state.clear()
    await message.answer(texts.ADMIN_GRANT_CANCELLED)


def _kind_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_GRANT_KIND_PERCENT, callback_data="grant:kind:percent"
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_GRANT_KIND_FIXED, callback_data="grant:kind:fixed"
                )
            ],
        ]
    )


async def receive_target(message: Message, state: FSMContext) -> None:
    """Accept either an @username or a numeric id.

    A username is stored as-is: the person may not have started the bot yet, which is the whole
    point of a list. There is no Bot API call that turns a username into an id, so this cannot be
    resolved now even if we wanted to.
    """
    raw = (message.text or "").strip()
    target_id: int | None = None
    username: str | None = None

    if raw.lstrip("-").isdigit():
        target_id = int(raw)
    else:
        candidate = normalise_username(raw)
        # Validated, not merely tidied: "!!!" would otherwise be stored as a username and sit in
        # the list forever, indistinguishable from someone who has not claimed their discount.
        username = candidate if looks_like_username(candidate) else None

    if target_id is None and not username:
        await message.answer(texts.ADMIN_GRANT_BAD_TARGET)
        return

    who = f"@{username}" if username else f"id {target_id}"
    await state.update_data(target_id=target_id, username=username, who=who)
    await state.set_state(GrantDiscount.waiting_for_kind)
    await message.answer(texts.ADMIN_GRANT_ASK_KIND.format(who=who), reply_markup=_kind_keyboard())


async def choose_kind(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    kind = (
        DiscountKind.PERCENT if (query.data or "").endswith("percent") else DiscountKind.FIXED_PRICE
    )
    await state.update_data(kind=kind.value)
    await query.answer()
    if not query.message:
        return
    if kind is DiscountKind.PERCENT:
        await state.set_state(GrantDiscount.waiting_for_percent)
        await query.message.answer(texts.ADMIN_GRANT_ASK_PERCENT)
    else:
        await state.set_state(GrantDiscount.waiting_for_fixed_price)
        await query.message.answer(
            texts.ADMIN_GRANT_ASK_FIXED.format(currency=settings.subscription_currency)
        )


def _period_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=texts.ADMIN_GRANT_PERIOD_DAYS.format(days=d), callback_data=f"grant:days:{d}"
            )
        ]
        for d in PERIOD_CHOICES
    ]
    rows.append(
        [InlineKeyboardButton(text=texts.ADMIN_GRANT_PERIOD_FOREVER, callback_data="grant:days:0")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def receive_percent(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().rstrip("%")
    if not raw.isdigit() or not 1 <= int(raw) <= 100:
        await message.answer(texts.ADMIN_GRANT_BAD_PERCENT)
        return
    await state.update_data(percent_off=int(raw))
    await _ask_period(message, state)


async def receive_fixed_price(message: Message, state: FSMContext) -> None:
    try:
        amount = Decimal((message.text or "").strip().replace(",", "."))
    except InvalidOperation:
        await message.answer(texts.ADMIN_GRANT_BAD_PRICE)
        return
    if amount <= 0:
        await message.answer(texts.ADMIN_GRANT_BAD_PRICE)
        return
    await state.update_data(fixed_price=str(amount))
    await _ask_period(message, state)


async def _ask_period(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(GrantDiscount.waiting_for_period)
    await message.answer(
        texts.ADMIN_GRANT_ASK_PERIOD.format(who=data["who"]), reply_markup=_period_keyboard()
    )


async def choose_period(query: CallbackQuery, state: FSMContext) -> None:
    days = int((query.data or "grant:days:0").rsplit(":", 1)[1])
    await state.update_data(days=days or None)
    await state.set_state(GrantDiscount.waiting_for_note)
    await query.answer()
    if query.message:
        await query.message.answer(
            texts.ADMIN_GRANT_ASK_NOTE,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=texts.ADMIN_GRANT_SKIP_NOTE, callback_data="grant:note:skip"
                        )
                    ]
                ]
            ),
        )


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_GRANT_CONFIRM_BUTTON, callback_data="grant:apply"
                )
            ],
            [
                InlineKeyboardButton(
                    text=texts.ADMIN_GRANT_CANCEL_BUTTON, callback_data="grant:abort"
                )
            ],
        ]
    )


def _summary(data: dict, settings: Settings) -> tuple[str, str, str]:
    """``(what, period, new_price)`` as they appear on the confirmation screen."""
    kind = DiscountKind(data["kind"])
    percent = data.get("percent_off")
    fixed = Decimal(data["fixed_price"]) if data.get("fixed_price") else None
    what = _describe_pending(kind, percent, fixed, settings.subscription_currency)

    days = data.get("days")
    period = (
        texts.ADMIN_GRANT_PERIOD_DAYS.format(days=days)
        if days
        else texts.ADMIN_GRANT_PERIOD_FOREVER
    )

    base = settings.subscription_price
    if kind is DiscountKind.PERCENT:
        new = (base * Decimal(100 - int(percent or 0)) / Decimal(100)).quantize(Decimal("0.01"))
    else:
        new = fixed or Decimal(0)
    return what, period, texts.money(new, settings.subscription_currency)


async def _show_confirmation(message: Message, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    what, period, new_price = _summary(data, settings)
    await state.set_state(GrantDiscount.waiting_for_confirmation)
    await message.answer(
        texts.ADMIN_GRANT_CONFIRM.format(
            who=data["who"],
            what=what,
            period=period,
            note=data.get("note") or "—",
            base=texts.money(settings.subscription_price, settings.subscription_currency),
            new_price=new_price,
        ),
        reply_markup=_confirm_keyboard(),
    )


async def receive_note(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.update_data(note=(message.text or "").strip() or None)
    await _show_confirmation(message, state, settings)


async def skip_note(query: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    await state.update_data(note=None)
    await query.answer()
    if query.message:
        await _show_confirmation(query.message, state, settings)


async def abort_grant(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    if query.message:
        await query.message.answer(texts.ADMIN_GRANT_CANCELLED)


async def apply_grant(
    query: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings
) -> None:
    """Write the discount. The confirmation step is what makes this safe to do on one tap."""
    data = await state.get_data()
    await state.clear()
    now = utcnow()
    kind = DiscountKind(data["kind"])

    discount = await discount_service.grant(
        session,
        actor_id=query.from_user.id,
        telegram_id=data.get("target_id"),
        username=data.get("username"),
        kind=kind,
        percent_off=data.get("percent_off"),
        fixed_price=Decimal(data["fixed_price"]) if data.get("fixed_price") else None,
        currency=settings.subscription_currency,
        days=data.get("days"),
        note=data.get("note"),
        now=now,
    )
    await session.commit()

    what, period, _ = _summary(data, settings)
    await query.answer()
    if query.message:
        await query.message.answer(
            texts.ADMIN_GRANT_DONE.format(
                who=discount.target,
                what=what,
                period="" if not data.get("days") else f", {period.lower()}",
            )
        )


def register(router: Router) -> None:
    """Attach the discount screens to the gated admin router.

    Registered on the caller's router rather than a router of its own, so the ``IsAdmin`` filter
    already applied there covers all of this too — one gate, no chance of a screen escaping it.
    """
    router.callback_query.register(show_discounts, F.data == "admin:discounts")
    router.callback_query.register(start_grant, F.data == "admin:grant_discount")

    router.message.register(
        cancel_grant, Command("cancel"), StateFilter(*GrantDiscount.__all_states__)
    )
    router.message.register(receive_target, StateFilter(GrantDiscount.waiting_for_target))
    router.callback_query.register(
        choose_kind, StateFilter(GrantDiscount.waiting_for_kind), F.data.startswith("grant:kind:")
    )
    router.message.register(receive_percent, StateFilter(GrantDiscount.waiting_for_percent))
    router.message.register(receive_fixed_price, StateFilter(GrantDiscount.waiting_for_fixed_price))
    router.callback_query.register(
        choose_period,
        StateFilter(GrantDiscount.waiting_for_period),
        F.data.startswith("grant:days:"),
    )
    router.callback_query.register(
        skip_note, StateFilter(GrantDiscount.waiting_for_note), F.data == "grant:note:skip"
    )
    router.message.register(receive_note, StateFilter(GrantDiscount.waiting_for_note))
    router.callback_query.register(
        apply_grant, StateFilter(GrantDiscount.waiting_for_confirmation), F.data == "grant:apply"
    )
    router.callback_query.register(
        abort_grant, StateFilter(GrantDiscount.waiting_for_confirmation), F.data == "grant:abort"
    )
