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

#: Telegram rejects a message over 4096 characters. The list is therefore split across several
#: messages rather than truncated: an admin looking for one member among 85 cannot find them in
#: a list that stops at 30, which is what this screen is for.
CHUNK_CHARS = 3500


def _handle(user: User) -> str:
    """How a member is named in the list.

    A username when they have one. Otherwise their first name — Telegram does not give a bot
    anybody's phone number (only a contact the user chooses to share, which this bot never asks
    for), so a name is the best searchable handle available, and far better than a bare id.
    """
    if user.username:
        return f"@{user.username}"
    if user.first_name:
        return f"{user.first_name} (id {user.telegram_id})"
    return f"id {user.telegram_id}"


def _chunks(lines: list[str]) -> list[str]:
    """Group lines into messages that each stay under Telegram's limit."""
    messages: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) > CHUNK_CHARS:
            messages.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        messages.append("\n".join(current))
    return messages


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
        handle = _handle(user)

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

        lines.append(
            texts.ADMIN_USER_LINE.format(
                handle=handle, status=status_name, amount=amount, until=until
            )
        )

    await query.message.answer(texts.ADMIN_USERS_HEADER.format(total=len(rows), **counts))
    for chunk in _chunks(lines):
        await query.message.answer(chunk)
    await query.message.answer(texts.ADMIN_USERS_FOOTER)
    log.info("Listed %d participants for admin %s", len(rows), query.from_user.id)


def register(router: Router) -> None:
    """Attach to the gated admin router, so the existing IsAdmin filter covers this too."""
    router.callback_query.register(show_participants, F.data == "admin:users")
