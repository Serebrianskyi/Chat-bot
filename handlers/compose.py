"""Reading what an admin composed, whether or not they attached a picture.

Its own module because three screens need it — 📣 Розсилка, 📢 Написати в канал and
✍️ Написати учаснику — and none of them should have to import from the others to get it.

A post is **collected from as many messages as the admin sends**, because a long post with a
picture cannot be one Telegram message: attaching a photo drops the limit from 4096 characters
to 1024. ``collect`` takes one message at a time and ``replay`` shows the result back in order.

``too_long`` is the single place that knows Telegram's two ceilings, so every screen refuses an
oversized part the same way and says the same thing about it.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

import texts
from services.subscriptions import CAPTION_LIMIT, MESSAGE_LIMIT


def read_composed(message: Message) -> tuple[str, str | None]:
    """What the admin composed: ``(text, photo_file_id)``.

    An admin writes a message the way they write any message — typed, or typed with an image
    attached. Telegram puts the words in ``caption`` rather than ``text`` when a photo is
    attached, which is the only reason this needs saying in code at all.

    The largest offered size is taken: Telegram sends several, ascending. The ``file_id`` it
    returns is reusable, so one upload serves every recipient of a broadcast rather than the
    bot re-uploading the picture per member.
    """
    if message.photo:
        return (message.caption or "").strip(), message.photo[-1].file_id
    return (message.text or "").strip(), None


async def preview(message: Message, *, text: str, photo: str | None, reply_markup=None) -> None:
    """Show the admin their message exactly as a member will receive it.

    Splits when the words will not fit in a caption. Telegram allows 1024 characters on a photo
    caption against 4096 in a message, and a Premium account can *type* a longer caption than a
    bot is allowed to *send* — which is how a real preview died on
    ``Bad Request: message caption is too long`` and showed the admin nothing (2026-10-10).

    When it splits, the keyboard goes on the text rather than the picture: it is the last thing
    the member reads, and a button above the words it refers to reads backwards.
    """
    extra = {"reply_markup": reply_markup} if reply_markup is not None else {}

    if photo is not None and len(text) > CAPTION_LIMIT:
        await message.answer_photo(photo)
        await message.answer(text, **extra)
    elif photo is not None:
        await message.answer_photo(photo, caption=text or None, **extra)
    else:
        await message.answer(text, **extra)


def too_long(text: str, photo: str | None, *, reserved: int = 0) -> str | None:
    """The warning to send the admin, or None when what they wrote will fit.

    Checked at the compose step so they are told the numbers, rather than discovering it from a
    preview that silently came back empty. Both of Telegram's ceilings are covered: 1024 with a
    picture attached, 4096 without — the second was missed at first, and a long text-only
    broadcast would have failed exactly the same way.

    ``reserved`` is room the caller will add afterwards, so a message that fits here cannot
    overflow once it is wrapped — ✍️ Написати учаснику prefixes the admin's words with
    «Повідомлення від адміністратора …» before sending.
    """
    limit = (CAPTION_LIMIT if photo is not None else MESSAGE_LIMIT) - reserved
    over = len(text) - limit
    if over <= 0:
        return None
    template = texts.ADMIN_CAPTION_TOO_LONG if photo is not None else texts.ADMIN_TEXT_TOO_LONG
    return template.format(length=len(text), limit=limit, over=over)


def done_keyboard(cancel_data: str) -> InlineKeyboardMarkup:
    """The "I have finished" button, offered after each part is taken."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.ADMIN_COMPOSE_DONE, callback_data="compose:done")],
            [InlineKeyboardButton(text=texts.ADMIN_INVITE_CONFIRM_NO, callback_data=cancel_data)],
        ]
    )


async def collect(message: Message, state, *, cancel_data: str, reserved: int = 0) -> bool:
    """Append what the admin just sent to the post being built. False if it was refused.

    Several messages rather than one, because **a long post with a picture cannot be a single
    Telegram message**: attaching a photo drops the ceiling from 4096 characters to 1024, so the
    words and the image have to travel separately. Owner's observation, 2026-10-10, after a
    caption that was well inside the message limit was refused for exceeding the caption one.

    Order is kept and each part is delivered as its own message, so what the member reads is what
    the admin wrote.
    """
    text, photo = read_composed(message)
    if not text and photo is None:
        await message.answer(texts.ADMIN_COMPOSE_NOTHING)
        return False

    # Each part is one send, so each is measured on its own: a picture part against the caption
    # limit, a words part against the message limit.
    warning = too_long(text, photo, reserved=reserved)
    if warning is not None:
        await message.answer(warning)
        return False

    data = await state.get_data()
    # Plain dicts rather than a dataclass: FSM state has to survive a storage backend that
    # serialises, and Redis is in the plan for a later phase.
    parts = [*data.get("parts", []), {"text": text, "photo": photo}]
    await state.update_data(parts=parts)
    await message.answer(
        texts.ADMIN_COMPOSE_ADDED.format(count=len(parts)),
        reply_markup=done_keyboard(cancel_data),
    )
    return True


async def replay(message: Message, parts: list[dict], *, reply_markup=None) -> None:
    """Send the composed post back part by part, exactly as a recipient will receive it.

    ``reply_markup`` goes on the last part, which is where the real one goes: a pay button above
    the words explaining the offer reads backwards.
    """
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        extra = {"reply_markup": reply_markup} if (last and reply_markup is not None) else {}
        if part.get("photo"):
            await message.answer_photo(part["photo"], caption=part.get("text") or None, **extra)
        else:
            await message.answer(part.get("text") or texts.ADMIN_COMPOSE_PHOTO_ONLY, **extra)
