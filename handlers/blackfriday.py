"""🖤 Black Friday 2026: the member-facing flow. Three months for the price of two.

Two audiences, told apart by the spec's own test — `expires_at > now`:

* a **new or lapsed** member is offered the bundle outright;
* an **active** member is told their current date and that ninety days land on top of it.

Both reach the same invoice. What differs is the copy, the button label, and whether a «Детальніше»
screen is offered — and all of it closes by itself when the window does, because every screen
checks `campaigns.is_open` rather than trusting a button that was drawn yesterday.

The price is never written in a handler: it comes from `campaigns.offer_for`, which doubles the
member's **own** standing tariff. A member on the legacy €8 rate therefore pays €16 without that
figure appearing in any copy, which is the owner's explicit instruction.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from config import Settings
from db.models import utcnow
from services import billing, campaigns
from services import subscriptions as subs
from services.billing import BillingConfig
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)

#: Callback data. Short strings: Telegram caps callback data at 64 bytes.
OFFER = "bf:offer"
DETAILS = "bf:details"
BUY = "bf:buy"
BACK = "bf:back"


def menu_button() -> InlineKeyboardButton:
    """The promo entry in the main menu. Only drawn while the campaign is open."""
    return InlineKeyboardButton(text=texts.BF_MENU_BUTTON, callback_data=OFFER)


def offer_keyboard(*, pay_url: str | None, active_member: bool) -> InlineKeyboardMarkup:
    """The buttons under the offer, exactly as specified — labels, order and case.

    An active member is not offered «Детальніше»: they are in the club and know what it is. That
    is the spec's own distinction, not an abbreviation of it.
    """
    rows: list[list[InlineKeyboardButton]] = []
    label = texts.BF_PAY_BUTTON_ACTIVE if active_member else texts.BF_PAY_BUTTON_NEW
    if pay_url:
        rows.append([InlineKeyboardButton(text=label, url=pay_url)])
    else:
        # No live link — the gateway refused or is not configured. A callback that tries again is
        # better than a dead URL button or no button at all.
        rows.append([InlineKeyboardButton(text=label, callback_data=BUY)])
    if not active_member:
        rows.append([InlineKeyboardButton(text=texts.BF_MORE_BUTTON, callback_data=DETAILS)])
    rows.append([InlineKeyboardButton(text=texts.BF_BACK_BUTTON, callback_data=BACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def details_keyboard(*, pay_url: str | None) -> InlineKeyboardMarkup:
    """«Детальніше» — the pay button and the way back to the offer."""
    rows: list[list[InlineKeyboardButton]] = []
    if pay_url:
        rows.append([InlineKeyboardButton(text=texts.BF_PAY_BUTTON_DETAILS, url=pay_url)])
    else:
        rows.append([InlineKeyboardButton(text=texts.BF_PAY_BUTTON_DETAILS, callback_data=BUY)])
    rows.append([InlineKeyboardButton(text=texts.BF_BACK_TO_OFFER_BUTTON, callback_data=OFFER)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def bundle_link(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None,
    wayforpay: WayForPayClient | None,
    billing_config: BillingConfig | None,
    now,
) -> tuple[str | None, bool]:
    """``(payment link, is an active member)`` for this member's bundle.

    Reuses an open campaign invoice rather than minting another: somebody who taps the button
    three times should meet the same page, not three orders for the poller to chase.

    None for the link when the gateway could not be reached — the caller says something true
    rather than drawing a button that fails.
    """
    subscription = await subs.get_subscription(session, telegram_id)
    if subscription is None:
        return None, False
    active = campaigns.is_active_member(subscription, now=now)

    if wayforpay is None:
        return None, active

    config = billing_config or BillingConfig(period_days=subscription.period_days)
    existing = await subs.open_payment(session, subscription.id)
    if (
        existing is not None
        and existing.campaign == campaigns.BLACK_FRIDAY
        and existing.invoice_url
        and now - existing.created_at < config.invoice_timeout
    ):
        return existing.invoice_url, active

    offer = await campaigns.offer_for(
        session, subscription=subscription, username=username, now=now
    )
    try:
        payment = await billing.issue_invoice(
            session,
            wayforpay,
            subscription=subscription,
            config=config,
            now=now,
            offer=offer,
        )
        await session.commit()
    except Exception:
        log.exception("Could not create a Black Friday invoice for %s", telegram_id)
        await session.rollback()
        return None, active
    return payment.invoice_url, active


async def _guard(query: CallbackQuery, session: AsyncSession, settings: Settings, now) -> bool:
    """True when this member may still buy. Answers them when they may not.

    Checked on **every** screen rather than only when the menu is drawn: a button from yesterday
    is still a button, and the campaign closing has to be the end of it.
    """
    if not campaigns.is_open(
        now, starts_at=settings.black_friday_start, ends_at=settings.black_friday_end
    ):
        if query.message:
            await query.message.answer(texts.BF_CLOSED)
        return False
    if await campaigns.already_claimed(session, user_id=query.from_user.id):
        subscription = await subs.get_subscription(session, query.from_user.id)
        if query.message and subscription is not None:
            await query.message.answer(
                texts.BF_ALREADY_CLAIMED.format(until=texts.day(subscription.expires_at))
            )
        return False
    return True


async def show_offer(
    query: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    wayforpay: WayForPayClient | None = None,
    billing_config: BillingConfig | None = None,
) -> None:
    """The offer itself, in the wording that matches which audience they are in."""
    await query.answer()
    now = utcnow()
    if not await _guard(query, session, settings, now) or query.message is None:
        return

    link, active = await bundle_link(
        session,
        telegram_id=query.from_user.id,
        username=query.from_user.username,
        wayforpay=wayforpay,
        billing_config=billing_config,
        now=now,
    )
    subscription = await subs.get_subscription(session, query.from_user.id)
    if active and subscription is not None:
        body = texts.BF_OFFER_ACTIVE.format(until=texts.day(subscription.expires_at))
    else:
        body = texts.BF_OFFER_NEW
    if link is None:
        body += "\n\n" + texts.BF_UNAVAILABLE

    await query.message.answer(
        body, reply_markup=offer_keyboard(pay_url=link, active_member=active)
    )


async def show_details(
    query: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    bot: Bot,
    wayforpay: WayForPayClient | None = None,
    billing_config: BillingConfig | None = None,
) -> None:
    """«Детальніше про Your Story Club» — the long read, with the same live link."""
    await query.answer()
    now = utcnow()
    if not await _guard(query, session, settings, now) or query.message is None:
        return

    link, _active = await bundle_link(
        session,
        telegram_id=query.from_user.id,
        username=query.from_user.username,
        wayforpay=wayforpay,
        billing_config=billing_config,
        now=now,
    )
    subscription = await subs.get_subscription(session, query.from_user.id)
    standard = True
    if subscription is not None:
        offer = await campaigns.offer_for(
            session, subscription=subscription, username=query.from_user.username, now=now
        )
        standard = offer.amount == settings.subscription_price * campaigns.PAY_FOR

    await query.message.answer(
        texts.BF_DETAILS,
        reply_markup=details_keyboard(pay_url=link, standard_price=standard),
    )


async def go_back(query: CallbackQuery, session: AsyncSession) -> None:
    """«Назад» — out of the campaign and into their own subscription screen.

    Somewhere real rather than a dead end: the member came from a promo and the useful next thing
    is what they already have.
    """
    await query.answer()
    if query.message is None:
        return
    from handlers.subscription import render_subscription

    await render_subscription(
        query.message,
        session,
        telegram_id=query.from_user.id,
        username=query.from_user.username,
    )


def build_router() -> Router:
    router = Router(name="black-friday")
    router.callback_query.register(show_offer, F.data == OFFER)
    router.callback_query.register(show_details, F.data == DETAILS)
    # The pay button falls back to a callback when no link could be made; retrying is the whole
    # behaviour, so it re-enters the offer screen.
    router.callback_query.register(show_offer, F.data == BUY)
    router.callback_query.register(go_back, F.data == BACK)
    return router
