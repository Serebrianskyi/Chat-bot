"""The 👥 Учасники admin screen.

People who have started the **bot**, grouped by what the club needs to know about them: who
renews automatically, who paid and turned renewal off, who is on a free period, and who owes
money. The screen opens on counts with a button per group, so it stays one message as the club
grows — a flat roster was already 85 people across several messages.

Each group then lists its members with the price they will next be charged and their due date.

It is not a channel roster. A bot cannot enumerate a channel's members — the Bot API offers only
``getChatMember`` for one known id — so anybody who never started the bot is invisible here. The
message says that outright, because otherwise the count reads as if members were missing.
"""

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
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


#: Group key -> (label, callback action). Order is the order the buttons appear in: the groups
#: an admin acts on first are the ones at the top.
GROUPS: tuple[tuple[str, str, str], ...] = (
    ("auto", texts.ADMIN_GROUP_AUTO, "users_auto"),
    ("cancelled", texts.ADMIN_GROUP_CANCELLED, "users_cancelled"),
    ("trial", texts.ADMIN_GROUP_TRIAL, "users_trial"),
    ("unpaid", texts.ADMIN_GROUP_UNPAID, "users_unpaid"),
    ("lifetime", texts.ADMIN_GROUP_LIFETIME, "users_lifetime"),
    ("no_sub", texts.ADMIN_GROUP_NO_SUB, "users_no_sub"),
)

#: What marks a subscription as having no end date. Set by the lifetime grant migration.
LIFETIME_SOURCE = "lifetime"


def classify(subscription: Subscription | None, *, now) -> str:
    """Which group this member belongs in. One member is in exactly one group.

    Keyed on the subscription's own state rather than on how it was paid for, because what an
    admin needs to know is what happens next: nothing (renews), nothing until a date (cancelled),
    nothing yet (trial), or money is owed (unpaid).

    ``expires_at`` decides before ``status`` does wherever the two could disagree: a row left
    ACTIVE with a date in the past owes money, whatever the column says.
    """
    if subscription is None:
        return "no_sub"
    if subscription.source == LIFETIME_SOURCE:
        return "lifetime"
    if subscription.expires_at <= now:
        return "unpaid"
    if subscription.status is SubscriptionStatus.CANCELLED:
        return "cancelled"
    if subscription.status is SubscriptionStatus.TRIAL:
        return "trial"
    if subscription.status is SubscriptionStatus.ACTIVE:
        return "auto"
    # PAST_DUE or EXPIRED with a future date: still owes the payment that moved the date.
    return "unpaid"


def _group_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    """One button per non-empty group, carrying its count. Empty groups are left out."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{label} — {counts[key]}",
                    callback_data=f"admin:{action}",
                )
            ]
            for key, label, action in GROUPS
            if counts.get(key)
        ]
    )


async def _load(session: AsyncSession):
    return (
        await session.execute(
            select(User, Subscription)
            .outerjoin(Subscription, Subscription.user_id == User.telegram_id)
            .order_by(User.created_at.desc())
        )
    ).all()


async def show_group(query: CallbackQuery, session: AsyncSession) -> None:
    """List one group. Reached from the summary's buttons."""
    action = (query.data or "").removeprefix("admin:")
    key = next((k for k, _, a in GROUPS if a == action), None)
    label = next((lbl for k, lbl, _ in GROUPS if k == key), "")

    await query.answer()
    if query.message is None or key is None:
        return

    now = utcnow()
    lines: list[str] = []
    for user, subscription in await _load(session):
        if classify(subscription, now=now) != key:
            continue
        if subscription is None:
            status_name = texts.STATUS_NAMES["expired"]
            amount = "—"
            until = "—"
        else:
            status_name = texts.STATUS_NAMES.get(
                subscription.status.value, subscription.status.value
            )
            # The sum they will actually be charged next, discount included.
            price, currency, _ = await discount_service.effective_price(
                session, subscription=subscription, username=user.username, now=now
            )
            amount = texts.money(price, currency)
            until = texts.day(subscription.expires_at)
        lines.append(
            texts.ADMIN_USER_LINE.format(
                handle=_handle(user), status=status_name, amount=amount, until=until
            )
        )

    if not lines:
        await query.message.answer(texts.ADMIN_USERS_GROUP_EMPTY)
        return

    header = texts.ADMIN_USERS_GROUP_HEADER.format(label=label, count=len(lines))
    note = texts.ADMIN_GROUP_NOTES.get(key, "")
    await query.message.answer(f"{header}{note}")
    for chunk in _chunks(lines):
        await query.message.answer(chunk)
    log.info("Listed group %s (%d members) for admin %s", key, len(lines), query.from_user.id)


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

    counts: dict[str, int] = {key: 0 for key, _, _ in GROUPS}
    for _user, subscription in rows:
        counts[classify(subscription, now=now)] += 1

    # One message, whatever the size of the club. The per-group lists are a tap away, and only
    # the group asked for is sent — which is the whole point of the change.
    await query.message.answer(
        texts.ADMIN_USERS_SUMMARY.format(total=len(rows)),
        reply_markup=_group_keyboard(counts),
    )
    await query.message.answer(texts.ADMIN_USERS_FOOTER)
    log.info("Listed %d participants for admin %s: %s", len(rows), query.from_user.id, counts)
    log.info("Listed %d participants for admin %s", len(rows), query.from_user.id)


def register(router: Router) -> None:
    """Attach to the gated admin router, so the existing IsAdmin filter covers this too.

    Registered before the admin menu's placeholder handler (see ``handlers.admin``), so these
    claim their callbacks rather than being answered with "later".
    """
    router.callback_query.register(show_participants, F.data == "admin:users")
    for _key, _label, action in GROUPS:
        router.callback_query.register(show_group, F.data == f"admin:{action}")
