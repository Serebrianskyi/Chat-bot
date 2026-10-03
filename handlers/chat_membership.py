"""Reports the chat id when the bot is added to a channel or group.

An operator cannot look this up: the Bot API has no way to turn an invite link into a chat id, and
a channel usually has no public @username. The bot is the only thing that learns the id — from the
``my_chat_member`` update it receives on being added — so it tells the admins immediately, along
with which of the two required rights are actually granted.

That turns "find your channel id" from a research task into reading one message.

``my_chat_member`` arrives without any ``allowed_updates`` configuration; only ``chat_member``
(membership changes of *other* users) has to be requested explicitly.
"""

import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import ChatMemberUpdated, Message

import texts
from services.billing import notify_admins

log = logging.getLogger(__name__)

#: Chats worth reporting. A private chat with a user is not a community.
COMMUNITY_TYPES = frozenset({ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP})


def _rights_report(member) -> str:
    """Which of the two rights Phase 3 needs are present.

    Reported as a checklist rather than a yes/no, because "the bot is an admin" is not the same as
    "the bot can do its job" — Telegram grants admin rights individually.
    """
    lines = []
    for granted, name in (
        (getattr(member, "can_invite_users", False), texts.ADMIN_RIGHT_INVITE),
        (getattr(member, "can_restrict_members", False), texts.ADMIN_RIGHT_BAN),
    ):
        template = texts.ADMIN_RIGHT_OK if granted else texts.ADMIN_RIGHT_MISSING
        lines.append(template.format(name=name))
    return "\n".join(lines)


async def handle_my_chat_member(
    event: ChatMemberUpdated, bot: Bot, admin_ids: frozenset[int]
) -> None:
    """Tell the admins the chat id, and whether the bot can actually work there."""
    chat = event.chat
    if chat.type not in COMMUNITY_TYPES:
        return

    # Compared with == rather than `is`, and never via .value: aiogram's enums subclass str, and
    # depending on how the update was parsed this field can arrive as a plain string. An `is`
    # comparison then silently falls through every branch.
    status = event.new_chat_member.status
    title = chat.title or "(без назви)"

    if status == ChatMemberStatus.ADMINISTRATOR:
        log.info("Bot added to %s (%s) as administrator. CHANNEL_ID=%s", title, chat.type, chat.id)
        await notify_admins(
            bot,
            admin_ids,
            texts.ADMIN_ADDED_TO_CHAT.format(
                title=title,
                chat_type=chat.type,
                chat_id=chat.id,
                status=str(status),
                rights=_rights_report(event.new_chat_member),
            ),
        )
        return

    if status == ChatMemberStatus.MEMBER:
        log.warning("Bot added to %s as a plain member; CHANNEL_ID=%s", title, chat.id)
        await notify_admins(
            bot,
            admin_ids,
            texts.ADMIN_ADDED_NOT_ENOUGH.format(title=title, chat_id=chat.id),
        )
        return

    # LEFT or KICKED: worth a log line, but nothing an admin must act on immediately.
    log.warning("Bot is now %s in %s (id %s)", status, title, chat.id)


async def report_chat_id(
    bot: Bot, admin_ids: frozenset[int], *, chat_id: int, title: str | None, chat_type: str
) -> None:
    log.info("Chat id discovered: %s (%s, %s)", chat_id, title, chat_type)
    await notify_admins(
        bot,
        admin_ids,
        texts.ADMIN_CHAT_ID_FOUND.format(
            title=title or "(без назви)", chat_type=chat_type, chat_id=chat_id
        ),
    )


async def handle_forwarded_from_chat(message: Message, bot: Bot, admin_ids: frozenset[int]) -> None:
    """Recover a chat id from a message forwarded to the bot by an admin.

    Telegram only announces the join once. For a chat the bot was already in, this is the quickest
    way to learn the id: forward any post from it into the bot's DM.
    """
    if message.from_user is None or message.from_user.id not in admin_ids:
        return
    origin = message.forward_origin
    chat = getattr(origin, "chat", None)
    if chat is None:
        return
    await report_chat_id(
        bot, admin_ids, chat_id=chat.id, title=chat.title, chat_type=str(chat.type)
    )


async def handle_channel_post(message: Message, bot: Bot, admin_ids: frozenset[int]) -> None:
    """Report the id of a channel the bot can see posts in.

    Only announced once per channel per run: a chatty channel should not spam the admins.
    """
    if message.chat.id in _reported:
        return
    _reported.add(message.chat.id)
    await report_chat_id(
        bot,
        admin_ids,
        chat_id=message.chat.id,
        title=message.chat.title,
        chat_type=str(message.chat.type),
    )


#: Channels already reported in this process, so one post per channel is enough.
_reported: set[int] = set()


def build_router() -> Router:
    router = Router(name="chat-membership")
    router.my_chat_member.register(handle_my_chat_member)
    # A forward carries forward_origin; F.forward_origin keeps ordinary DMs out of this handler.
    router.message.register(handle_forwarded_from_chat, F.forward_origin)
    router.channel_post.register(handle_channel_post)
    return router
