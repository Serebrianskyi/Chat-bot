"""Reading what an admin composed, whether or not they attached a picture.

Its own module because three screens need it — 📣 Розсилка, 📢 Написати в канал and
✍️ Написати учаснику — and none of them should have to import from the others to get it.

One message per step for now: text, or a photo whose caption is the text. Collecting several
messages into one broadcast was started and deliberately parked — see the note in CLAUDE.md.
"""

from aiogram.types import Message


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
