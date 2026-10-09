"""Entry point.

Two run modes, selected by the ``MODE`` environment variable:

* ``polling``  — development, Phases 0–3. Asks Telegram for updates in a loop.
* ``webhook``  — production, Phase 4 onward. Telegram POSTs to the FastAPI app in ``web/``.

Only one process may use a bot token at a time. Never point local polling at the production
token (CLAUDE.md rule 4).
"""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from config import ConfigError, Settings, get_settings
from db.session import create_engine, create_session_factory
from handlers import admin, chat_membership, errors, start, subscription
from middlewares.session import DbSessionMiddleware
from services.billing import BillingConfig, recover_access_at_startup
from services.commands import register_commands
from services.scheduler import (
    build_billing_config,
    build_client,
    start_scheduler,
)
from services.wayforpay import WayForPayClient

log = logging.getLogger(__name__)


def build_dispatcher(
    settings: Settings,
    session_factory: async_sessionmaker,
    *,
    wayforpay: WayForPayClient | None = None,
    billing_config: BillingConfig | None = None,
) -> Dispatcher:
    """Create the dispatcher, register middleware and routers.

    ``admin_ids`` goes in as dispatcher context so that handlers and the ``IsAdmin`` filter
    read one source of truth, and tests can substitute it without touching the environment.

    **Router order is part of the access control.** The admin router carries the ``IsAdmin``
    gate; an update it rejects falls through to the denied router, which answers with the
    refusal. Registering them the other way round would deny everyone, including admins.
    The error router is last because it only handles the error event.
    """
    # settings goes in as context so handlers read one validated object rather than the
    # environment, and tests can substitute it wholesale. The WayForPay client goes in for the
    # same reason: /start invoices a paying joiner on the spot, and a test needs to swap it.
    # Both are None when WayForPay is not configured, and handlers degrade rather than fail.
    # The client is a parameter, not built here unconditionally: a test that did not supply one
    # would otherwise get a real client from its fake credentials and make live HTTP calls to a
    # payment gateway. Production passes one in from `_run`.
    dispatcher = Dispatcher(
        admin_ids=settings.admin_id_set,
        settings=settings,
        wayforpay=wayforpay,
        billing_config=billing_config or build_billing_config(settings),
        # A broadcast outlives the one-session-per-update contract: it opens a session per
        # recipient so one failed invoice cannot roll back the others. It therefore needs the
        # factory itself, not the session the middleware injects.
        session_factory=session_factory,
    )

    # Outer, so every update type gets a session before any filter runs.
    dispatcher.update.outer_middleware(DbSessionMiddleware(session_factory))

    dispatcher.include_router(chat_membership.build_router())
    dispatcher.include_router(start.build_router())
    dispatcher.include_router(subscription.build_router())
    dispatcher.include_router(admin.build_router())
    dispatcher.include_router(admin.build_denied_router())
    dispatcher.include_router(errors.build_router())

    return dispatcher


def build_bot(settings: Settings) -> Bot:
    """The Bot, with HTML parse mode as the default for all outgoing messages."""
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def run_polling(settings: Settings, engine: AsyncEngine) -> None:
    """Development mode: long-poll Telegram with the test bot.

    The engine is passed in rather than looked up: reaching for a module-level cached engine
    would ignore the ``settings`` argument and make this function untestable.

    Everything after the Bot is constructed runs inside the try, including the first two API
    calls. They are the most likely thing to fail — a rejected token fails there — and leaving
    them outside would skip ``session.close()``, so the real error would be buried under
    "Unclosed client session" warnings from aiohttp.
    """
    bot = build_bot(settings)
    try:
        dispatcher = build_dispatcher(
            settings,
            create_session_factory(engine),
            wayforpay=build_client(settings),
            billing_config=build_billing_config(settings),
        )

        # A webhook left over from a previous run would stop polling receiving anything.
        await bot.delete_webhook(drop_pending_updates=False)

        # Publish the command menu before polling starts, so the very first /start already has
        # a usable menu beside the input box.
        await register_commands(bot, settings.admin_id_set)

        me = await bot.get_me()
        log.info("Polling as @%s (admins: %s)", me.username, sorted(settings.admin_id_set))

        scheduler = start_scheduler(
            settings=settings, session_factory=create_session_factory(engine), bot=bot
        )

        # Catch up on anyone who paid and never got into the channel. As a background task, not
        # awaited: it makes dozens of API calls and the bot should answer /start while it runs.
        # The reference is held because asyncio only keeps a weak one to a bare task.
        recovery = asyncio.create_task(
            recover_access_at_startup(
                create_session_factory(engine),
                build_client(settings),
                bot,
                admin_ids=settings.admin_id_set,
                config=build_billing_config(settings),
            )
        )
        try:
            await dispatcher.start_polling(bot)
        finally:
            recovery.cancel()
            if scheduler is not None:
                scheduler.shutdown(wait=False)
    except TelegramUnauthorizedError:
        # The commonest first-run mistake, and Telegram's own message is just "Unauthorized".
        raise ConfigError(
            "Telegram rejected BOT_TOKEN. Check it is copied whole from @BotFather "
            "(format 123456789:AA...), and that it is the TEST bot's token."
        ) from None
    finally:
        await bot.session.close()


async def _run(settings: Settings) -> None:
    """Own the engine for the lifetime of the process, and dispose it on the way out."""
    engine = create_engine(settings.database_url)
    try:
        await run_polling(settings, engine)
    finally:
        await engine.dispose()


def main() -> int:
    """Entry point. Returns a process exit code.

    A ConfigError is an operator mistake, not a crash: print the message and exit non-zero
    rather than dumping a traceback that buries it.
    """
    try:
        settings = get_settings()
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    if settings.mode == "webhook":
        # TODO(phase-4): serve web.app with uvicorn; register the webhook with
        # secret_token=settings.webhook_secret on startup.
        raise NotImplementedError("Phase 4: run the FastAPI app in webhook mode.")

    try:
        asyncio.run(_run(settings))
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
