"""User registration and lookup. Phase 1.

Pure database logic, unit-tested without Telegram (CLAUDE.md rule 10). Handlers pass in
values already extracted from the update; nothing here knows what an aiogram type is.

Gate items: G1.1 ``/start`` twice creates exactly one row · G1.2 a changed username is
stored on the next ``/start``.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User, UserRole


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
# TODO(phase-3): mark_blocked_bot(session, telegram_id, blocked) -- set is_blocked_bot when
#                Telegram raises TelegramForbiddenError, so expiry and broadcast loops can
#                skip the user and continue (G3.5, G7.3).
# TODO(phase-7): count_users / segment counts for the statistics screen (G7.9).
