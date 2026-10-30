"""User registration and lookup. Phase 1.

Pure database logic, unit-tested without Telegram (CLAUDE.md rule 10). Handlers pass in
values already extracted from the update; nothing here knows what an aiogram type is.

Gate items: G1.1 ``/start`` twice creates exactly one row · G1.2 a changed username is
stored on the next ``/start``.
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User, UserRole

log = logging.getLogger(__name__)


async def upsert_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    *,
    is_admin: bool = False,
) -> User:
    """Register the user, or refresh the profile fields of an existing row.

    Called on every ``/start``, so it must be idempotent: the second call updates rather
    than inserts (G1.1). ``username`` and ``first_name`` are overwritten because Telegram
    lets users change both at will and the stored copy would otherwise go stale (G1.2).

    ``created_at`` is never touched after insert — it records first contact.

    ``is_admin`` comes from ``ADMIN_IDS``, so ``role`` stays a mirror of the environment.
    It is written here only for display and reporting; the access check reads the
    environment directly (see ``handlers.admin``).
    """
    user = await session.get(User, telegram_id)
    role = UserRole.ADMIN if is_admin else UserRole.USER

    if user is None:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            role=role,
        )
        session.add(user)
    else:
        user.username = username
        user.first_name = first_name
        user.role = role

    await session.commit()
    return user


# TODO(phase-3): find_user(session, username_or_id) -- admin lookup by @username or id.
# TODO(phase-7): count_users / segment counts for the statistics screen (G7.9).


async def mark_blocked_bot(session, telegram_id: int, *, blocked: bool) -> bool:
    """Record that this member blocked the bot, or unblocked it. Returns True if it changed.

    Telegram announces both: a ``my_chat_member`` update arrives for the private chat with status
    ``kicked`` on a block and ``member`` on an unblock. That makes the flag **current** rather than
    a stale observation — the objection to storing it at all was that somebody who blocks in
    January and unblocks in March would be purged on two-month-old evidence, and an update that
    clears the flag removes exactly that.

    Writes nothing for a member with no row: blocking the bot without ever having started it is
    not possible, and inventing a user here would create rows from an update alone.
    """
    user = await session.get(User, telegram_id)
    if user is None or user.is_blocked_bot == blocked:
        return False
    user.is_blocked_bot = blocked
    await session.commit()
    log.info("%s %s the bot", telegram_id, "blocked" if blocked else "unblocked")
    return True


async def has_blocked_bot(bot, telegram_id: int) -> bool:
    """Whether this member has blocked the bot, asked at this moment.

    There is no API that answers "did they block me", so this sends a **typing action**: the
    cheapest call that fails with ``Forbidden`` for a blocked chat. Nothing is delivered and
    nothing stays in their history — but it is **not** silent: anyone who has not blocked the bot
    sees it start typing for a few seconds. So this is the deliberate, by-hand way of finding
    members who blocked the bot *before* ``mark_blocked_bot`` existed to notice; the monthly job
    reads the flag and probes nobody.

    ``TelegramRetryAfter`` is re-raised rather than swallowed. Caught here it would return False —
    "they have not blocked the bot" — and a flood limit partway through a sweep would read as a
    clean result, which is worse than failing.

    Returns False on any other failure. A network blip must not be read as a block, because what
    follows a block here is deletion.
    """
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

    try:
        await bot.send_chat_action(chat_id=telegram_id, action="typing")
    except TelegramForbiddenError:
        return True
    except TelegramRetryAfter:
        raise
    except Exception:
        log.debug("Could not probe %s; assuming they have not blocked the bot", telegram_id)
        return False
    return False
