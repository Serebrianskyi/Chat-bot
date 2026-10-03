"""``/start``: registration, pricing, and the first payment prompt. Phase 2A.

Gate items:

* G1.1 / G1.2 — one user row, username kept current
* A.1 — exactly one subscription per user; a second ``/start`` creates nothing
* A.2 / A.3 / A.4 — three-way price resolution
* A.5 — ``getChatMember`` failing, or ``CHANNEL_ID`` unset, means "new joiner", never a crash
* A.6 — the free period is granted once

Routers are built by a factory rather than created at import time: a ``Router`` instance can
only be attached to one dispatcher, so module-level singletons would make it impossible to
build a second dispatcher in the same process — which every test does.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from config import Settings
from db.models import utcnow
from handlers import admin, subscription
from services import billing
from services.billing import BillingConfig
from services.subscriptions import ensure_subscription
from services.users import upsert_user
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)

#: Statuses that mean "currently in the community". ``LEFT`` and ``KICKED`` do not.
IN_COMMUNITY = frozenset(
    {
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
    }
)

# All member-facing wording lives in texts.py; see that module for why.


async def is_in_community(bot: Bot, chat_id: str | None, user_id: int) -> bool:
    """Whether this user is currently a member of the community.

    ``getChatMember`` is the only membership call a bot has — there is no way to list members, so
    this is always asked one user at a time.

    Any failure means "not a member": an unset ``CHANNEL_ID``, a bot that is not in the chat, a
    user Telegram has never heard of. Failing closed matters because the alternative is handing
    out free months on the strength of an API error (A.5).
    """
    if not chat_id:
        return False
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        # Inside the try on purpose: an unexpected response shape must fail the same way a
        # network error does. A guard that does not cover the line that actually breaks is not
        # a guard.
        return member.status in IN_COMMUNITY
    except Exception:  # noqa: BLE001 - every failure mode means the same thing here
        log.warning("Membership check failed for %s in %s; treating as new", user_id, chat_id)
        return False


def member_keyboard(is_admin: bool) -> InlineKeyboardMarkup:
    """What a member can reach from the welcome.

    Without this the only way in is typing a command nobody mentioned. Buttons for features that
    do not exist yet are deliberately absent — a button that answers "later" is worse than none.
    """
    rows = [
        [InlineKeyboardButton(text=texts.MENU_MY_SUBSCRIPTION, callback_data="menu:subscription")]
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text=texts.ADMIN_MENU_TITLE, callback_data="menu:admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_menu(query: CallbackQuery, session: AsyncSession) -> None:
    """The 💳 Моя підписка button, so the feature is reachable without knowing /subscription."""
    await query.answer()
    if query.message is None or query.from_user is None:
        return
    await subscription.render_subscription(
        query.message,
        session,
        telegram_id=query.from_user.id,
        username=query.from_user.username,
    )


async def open_admin_panel(query: CallbackQuery, admin_ids: frozenset[int]) -> None:
    """The admin button on the welcome. Gated here too, not just hidden.

    Hiding a button is not access control: anyone can send the callback data by hand.
    """
    if query.from_user is None or query.from_user.id not in admin_ids:
        await query.answer(texts.ADMIN_ACCESS_DENIED, show_alert=True)
        return
    await query.answer()
    if query.message is not None:
        await query.message.answer(texts.ADMIN_MENU_TITLE, reply_markup=admin.build_menu())


async def handle_start(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    admin_ids: frozenset[int],
    wayforpay: WayForPayClient | None = None,
    billing_config: BillingConfig | None = None,
) -> None:
    """Register the sender, price them, and tell them what they owe.

    ``message.from_user`` is optional on the aiogram type (channel posts have no sender), so it
    is checked rather than assumed — an unchecked access here would be an unhandled exception in
    production (S8).
    """
    if message.from_user is None:
        return

    now = utcnow()
    telegram_id = message.from_user.id

    await upsert_user(
        session,
        telegram_id=telegram_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        is_admin=telegram_id in admin_ids,
    )

    in_community = await is_in_community(bot, settings.channel_id, telegram_id)

    subscription, created = await ensure_subscription(
        session,
        telegram_id=telegram_id,
        username=message.from_user.username,
        in_community=in_community,
        regular_price=settings.subscription_price,
        regular_currency=settings.subscription_currency,
        period_days=settings.subscription_period_days,
        legacy_offer_deadline=settings.legacy_offer_deadline,
        now=now,
    )
    await session.commit()

    log.info(
        "Onboarded %s as tier=%s (in_community=%s)",
        telegram_id,
        subscription.price_tier.value,
        in_community,
    )

    # Read everything needed for the replies *now*, as plain values. A rollback further down
    # expires the ORM objects, and touching an attribute afterwards would trigger a reload on a
    # dead transaction — surfacing to the member as the generic error instead of a greeting.
    amount = texts.money(subscription.price, subscription.currency)
    until = texts.day(subscription.expires_at.date())
    period_days = subscription.period_days
    free_period = subscription.free_period_granted

    is_admin = telegram_id in admin_ids

    if not created:
        await message.answer(
            texts.WELCOME_BACK.format(until=until), reply_markup=member_keyboard(is_admin)
        )
        return

    # The club pitch first, then the tariff as its own message. One combined wall of text would
    # bury the price and the pay button at the bottom of a long read.
    await message.answer(texts.WELCOME_INTRO)

    if free_period:
        await message.answer(
            texts.WELCOME_FREE.format(
                club=texts.CLUB_NAME,
                until=until,
                amount=amount,
                period=period_days,
            ),
            reply_markup=member_keyboard(is_admin),
        )
        log.info("Onboarded %s with a free first period", telegram_id)
        return

    # They owe payment now, so invoice them here rather than leaving it to the daily job. The
    # welcome text says a link follows; a promise the daily job keeps eight hours later is not a
    # promise kept. Which greeting is sent depends on whether the invoice actually worked.
    payment = None
    if wayforpay is not None:
        try:
            payment = await billing.issue_invoice(
                session,
                wayforpay,
                subscription=subscription,
                config=billing_config or BillingConfig(period_days=period_days),
                now=now,
            )
            await session.commit()
        except Exception:
            # A gateway failure must not cost the member their registration, which is already
            # committed. issue_invoice removes its own half-written payment row.
            log.exception("Could not invoice %s at /start", telegram_id)
            await session.rollback()
            payment = None

    if payment is None:
        await message.answer(
            texts.WELCOME_PAY_LATER.format(club=texts.CLUB_NAME, amount=amount, period=period_days)
        )
        log.warning("No invoice for %s at /start; the daily job will retry", telegram_id)
        return

    quoted = texts.money(payment.amount, payment.currency)
    # The menu goes on the tariff message; the pay link follows with its own single button, so
    # the two keyboards do not compete for attention.
    await message.answer(
        texts.WELCOME_PAY.format(club=texts.CLUB_NAME, amount=quoted, period=period_days),
        reply_markup=member_keyboard(is_admin),
    )
    await billing.send_payment_link(bot, subscription=subscription, payment=payment)
    log.info("Invoiced %s at /start: %s %s", telegram_id, payment.amount, payment.currency)


def build_router() -> Router:
    """The ``/start`` router, plus the buttons the welcome offers."""
    router = Router(name="start")
    router.message.register(handle_start, CommandStart())
    router.callback_query.register(show_menu, F.data == "menu:subscription")
    router.callback_query.register(open_admin_panel, F.data == "menu:admin")
    return router


# TODO(phase-2): user menu entry point for the knowledge base.
# TODO(phase-6): "My profile" entry point.
