"""The 👥 Учасники admin screen.

Lists people who have started the **bot**, with their status, the price they will next be charged
and their due date.

It is not a channel roster. A bot cannot enumerate a channel's members — the Bot API offers only
``getChatMember`` for one known id — so anybody who never started the bot is invisible here. The
message says that outright, because otherwise the count reads as if members were missing.
"""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import texts
from db.models import Subscription, SubscriptionStatus, User, utcnow
from services import discounts as discount_service

log = logging.getLogger(__name__)

#: Telegram rejects a message over 4096 characters, so the list is capped and the rest is counted.
MAX_LISTED = 30


async def show_participants(query: CallbackQuery, session: AsyncSession) -> None:
    now = utcnow()

    rows = (
        await session.execute(
            select(User, Subscription)
            .outerjoin(Subscription, Subscription.user_id == User.telegram_id)
            .order_by(User.created_at.desc())
        )
    ).all()

    await query.answer()
    if query.message is None:
        return

    if not rows:
        await query.message.answer(texts.ADMIN_USERS_EMPTY)
        return

    counts = {"active": 0, "unpaid": 0, "trial": 0}
    lines: list[str] = []

    for user, subscription in rows:
        handle = f"@{user.username}" if user.username else f"id {user.telegram_id}"

        if subscription is None:
            # Registered but with no subscription: only possible if a row was removed by hand.
            status_name = texts.STATUS_NAMES["expired"]
            amount = "—"
            until = "—"
        else:
            if subscription.status is SubscriptionStatus.ACTIVE:
                counts["active"] += 1
            elif subscription.status is SubscriptionStatus.TRIAL:
                counts["trial"] += 1
            elif subscription.status in (
                SubscriptionStatus.PAST_DUE,
                SubscriptionStatus.EXPIRED,
            ):
                counts["unpaid"] += 1

            status_name = texts.STATUS_NAMES.get(
                subscription.status.value, subscription.status.value
            )
            # The sum they will actually be charged next, discount included.
            price, currency, _ = await discount_service.effective_price(
                session, subscription=subscription, username=user.username, now=now
            )
            amount = texts.money(price, currency)
            until = texts.day(subscription.expires_at.date())

        if len(lines) < MAX_LISTED:
            lines.append(
                texts.ADMIN_USER_LINE.format(
                    handle=handle, status=status_name, amount=amount, until=until
                )
            )

    body = [
        texts.ADMIN_USERS_HEADER.format(total=len(rows), **counts),
        *lines,
    ]
    if len(rows) > MAX_LISTED:
        body.append(texts.ADMIN_USERS_TRUNCATED.format(shown=MAX_LISTED, total=len(rows)))
    body.append(texts.ADMIN_USERS_FOOTER)

    await query.message.answer("\n".join(body))
    log.info("Listed %d participants for admin %s", len(rows), query.from_user.id)


def register(router: Router) -> None:
    """Attach to the gated admin router, so the existing IsAdmin filter covers this too."""
    router.callback_query.register(show_participants, F.data == "admin:users")
