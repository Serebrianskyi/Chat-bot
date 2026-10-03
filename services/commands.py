"""Registering the command menu Telegram shows beside the input box.

Without this, ``getMyCommands`` is empty and a command is only usable by someone who already knows
it exists — which is how ``/admin`` ended up invisible.

Two scopes:

* **default** — what every member sees.
* **per-admin chat** — the same plus ``/admin``, set individually for each id in ``ADMIN_IDS``.
  Telegram has no "only admins" scope for a private chat, so the list is attached to each admin's
  own chat. An ordinary member is never shown a command they cannot use.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

import texts

log = logging.getLogger(__name__)

MEMBER_COMMANDS = [
    BotCommand(command="start", description=texts.CMD_START),
    BotCommand(command="subscription", description=texts.CMD_SUBSCRIPTION),
]

ADMIN_COMMANDS = [
    *MEMBER_COMMANDS,
    BotCommand(command="admin", description=texts.CMD_ADMIN),
    BotCommand(command="cancel", description=texts.CMD_CANCEL),
]


async def register_commands(bot: Bot, admin_ids: frozenset[int]) -> None:
    """Publish the command menus. Called once at startup.

    An admin who has never started the bot cannot have a scoped list set; that is logged and
    skipped rather than raised, because one unreachable admin must not stop the bot booting.
    """
    await bot.set_my_commands(MEMBER_COMMANDS, scope=BotCommandScopeDefault())
    log.info("Registered %d member commands", len(MEMBER_COMMANDS))

    for admin_id in sorted(admin_ids):
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except TelegramForbiddenError:
            log.warning(
                "Admin %s has not started the bot, so their command menu cannot be set.",
                admin_id,
            )
        except Exception:
            log.exception("Could not set the command menu for admin %s", admin_id)
        else:
            log.info("Registered %d admin commands for %s", len(ADMIN_COMMANDS), admin_id)
