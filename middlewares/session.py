"""Opens one database session per update and injects it into handlers.

Registered as an **outer** middleware on ``dp.update``, so every message, callback query and
future update type gets a session without each router asking for one. Handlers receive it as
a ``session`` argument.

One session per update is the unit of work: a handler that raises leaves nothing half-written
because the context manager rolls back on the way out.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from sqlalchemy.ext.asyncio import async_sessionmaker


class DbSessionMiddleware(BaseMiddleware):
    """Injects ``session`` into handler data for the lifetime of one update."""

    def __init__(self, session_factory: async_sessionmaker) -> None:
        self.session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.session_factory() as session:
            data["session"] = session
            return await handler(event, data)
