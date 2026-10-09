"""``/admin`` panel and the admin gate. Phase 1.

**The environment is the only authority on who is an admin.** ``IsAdmin`` reads ``ADMIN_IDS``
(via dispatcher context), never ``users.role``. The column is a mirror kept for display, so a
row rewritten by a bug, a bad migration or a manual SQL fix cannot grant anyone access. Do not
reverse this in a later phase.

The gate is applied at **router level** to both ``message`` and ``callback_query``, so it
cannot be forgotten on a new handler. Hiding a button is not access control: a non-admin can
send any ``callback_data`` string by hand, which is what G1.5 checks.

A rejected update falls through to ``denied_router``, registered after this one, which gives
the consistent refusal. Without it the non-admin would get silence — the behaviour this
project explicitly chose against.

Gate items: G1.3 non-admin gets no menu · G1.4 admin gets the menu with all buttons ·
G1.5 a forged ``callback_data`` from a non-admin is rejected. Every data-changing action added
here must write an ``audit_log`` row (S6).

Routers come from factories, not module-level singletons — see ``handlers.start`` for why.
"""

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import texts
from handlers import discounts, invites, participants

ACCESS_DENIED = texts.ADMIN_ACCESS_DENIED
MENU_TITLE = texts.ADMIN_MENU_TITLE
NOT_BUILT_YET = texts.ADMIN_NOT_BUILT_YET


class AdminMenu(CallbackData, prefix="admin"):
    """Callback payload for the admin menu. Serialises as ``admin:<action>``."""

    action: str


# label -> action. The order is the order the buttons appear in.
MENU_ITEMS: tuple[tuple[str, str], ...] = (
    (texts.ADMIN_MENU_DISCOUNTS, "discounts"),  # live
    (texts.ADMIN_MENU_GRANT_DISCOUNT, "grant_discount"),  # live
    (texts.ADMIN_MENU_STATISTICS, "stats"),  # TODO(phase-7)
    (texts.ADMIN_MENU_BROADCAST, "broadcast"),  # TODO(phase-7)
    (texts.ADMIN_MENU_USERS, "users"),  # live
    (texts.ADMIN_MENU_SEND_INVITE, "send_invite"),  # live
    (texts.ADMIN_MENU_MESSAGE_USER, "message_user"),  # live
    (texts.ADMIN_MENU_KNOWLEDGE_BASE, "materials"),  # TODO(phase-2)
    (texts.ADMIN_MENU_ADD_SUBSCRIBER, "add_subscriber"),  # TODO(phase-3)
)

#: Actions that have a real screen. The placeholder handler must not swallow these, and the
#: denial router must still refuse them for a non-admin.
IMPLEMENTED_ACTIONS = frozenset({"discounts", "grant_discount", "users", "send_invite"})


class IsAdmin(BaseFilter):
    """Passes when the sender's id is in ``ADMIN_IDS``.

    ``admin_ids`` is resolved from dispatcher context, so there is one source of truth and
    tests can vary it without touching the environment.
    """

    async def __call__(self, event: Message | CallbackQuery, admin_ids: frozenset[int]) -> bool:
        return event.from_user is not None and event.from_user.id in admin_ids


def build_menu() -> InlineKeyboardMarkup:
    """One button per menu item, one per row."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=AdminMenu(action=action).pack())]
            for label, action in MENU_ITEMS
        ]
    )


async def handle_admin(message: Message) -> None:
    await message.answer(MENU_TITLE, reply_markup=build_menu())


async def handle_admin_menu(query: CallbackQuery, callback_data: AdminMenu) -> None:
    """Acknowledge a menu tap.

    Every item is a placeholder until its phase. The callback is always answered so the
    client stops showing a spinner.
    """
    labels = {action: label for label, action in MENU_ITEMS}
    label = labels.get(callback_data.action, callback_data.action)
    await query.answer(NOT_BUILT_YET.format(label=label), show_alert=True)


async def deny_admin_command(message: Message) -> None:
    await message.answer(ACCESS_DENIED)


async def deny_admin_callback(query: CallbackQuery) -> None:
    """Refuse a forged admin callback (G1.5).

    Reached only when ``IsAdmin`` rejected the update on ``router`` above.
    """
    await query.answer(ACCESS_DENIED, show_alert=True)


async def deny_unknown_admin_callback(query: CallbackQuery) -> None:
    """Refuse any admin-prefixed callback no handler above claimed."""
    await query.answer(ACCESS_DENIED, show_alert=True)


ADMIN_CALLBACK_PREFIX = f"{AdminMenu.__prefix__}{AdminMenu.__separator__}"


def build_router() -> Router:
    """The gated admin router. Nothing registered here runs for a non-admin."""
    router = Router(name="admin")

    # Router level, so the gate cannot be forgotten on a handler added later.
    router.message.filter(IsAdmin())
    router.callback_query.filter(IsAdmin())

    router.message.register(handle_admin, Command("admin"))

    # Real screens first, so they claim their callbacks before the placeholder does.
    discounts.register(router)
    participants.register(router)
    invites.register(router)

    router.callback_query.register(
        handle_admin_menu,
        AdminMenu.filter(~F.action.in_(IMPLEMENTED_ACTIONS)),
    )
    return router


def build_denied_router() -> Router:
    """The refusal router. Must be registered *after* ``build_router()``.

    An update that ``IsAdmin`` rejected falls through to here, so a non-admin gets a clear
    answer rather than silence.
    """
    router = Router(name="admin-denied")
    router.message.register(deny_admin_command, Command("admin"))
    router.callback_query.register(deny_admin_callback, AdminMenu.filter())
    # Belt and braces: any unclaimed callback under the admin prefix is still refused, so a
    # menu item added in a later phase is gated by default.
    router.callback_query.register(
        deny_unknown_admin_callback, F.data.startswith(ADMIN_CALLBACK_PREFIX)
    )
    return router


# TODO(phase-2): wire the Knowledge Base button.
# TODO(phase-3): wire Users / Add subscriber (grant, extend, revoke) -- each writes audit_log.
# TODO(phase-7): wire Statistics and Broadcast.
