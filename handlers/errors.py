"""Global error handler. Standing gate S8.

"No unhandled exception reaches the user as silence." An exception escaping a handler is
logged in full and the user gets a short, friendly message — never a stack trace, and never
nothing at all.

From Phase 4 the log line is what Sentry picks up (G4.7).

aiogram catches the exception in ``ErrorsMiddleware``, an outer middleware on ``dp.update``
registered before ours. By the time this handler runs, ``DbSessionMiddleware`` has already
exited and rolled its session back — so the ``session`` in handler data is closed. An error
handler that needs the database must open its own session.
"""

import logging

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent

import texts

log = logging.getLogger(__name__)

#: Wording lives in texts.py. Kept as a module-level alias so tests and callers have a
#: single name to assert on.
USER_FACING_ERROR = texts.GENERIC_ERROR


def _reply_target(event: ErrorEvent):
    """The message or callback query to answer, if the update carries one."""
    update = event.update
    if update.message is not None:
        return update.message
    if update.callback_query is not None:
        return update.callback_query
    return None


async def handle_error(event: ErrorEvent) -> bool:
    """Log the exception, then tell the user something went wrong.

    Returns ``True`` so the dispatcher treats the update as handled and does not re-raise;
    for polling that keeps the loop alive, and for the Phase 4 webhook it keeps Telegram from
    retrying an update that will fail again.

    Telling the user can itself fail (they blocked the bot, the callback expired). That
    secondary failure is logged and swallowed — it must not mask the original exception.
    """
    log.exception(
        "Unhandled exception while processing update %s",
        event.update.update_id,
        exc_info=event.exception,
    )

    target = _reply_target(event)
    if target is None:
        return True

    try:
        await target.answer(USER_FACING_ERROR)
    except TelegramAPIError:
        log.warning(
            "Could not deliver the error notice for update %s",
            event.update.update_id,
            exc_info=True,
        )

    return True


def build_router() -> Router:
    """The error router. Registered last; it only handles the error event."""
    router = Router(name="errors")
    router.errors.register(handle_error)
    return router
