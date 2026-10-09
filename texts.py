"""Every string the bot sends to a person. Ukrainian.

One module so the wording can be reviewed as copy rather than hunted through handlers, and so
adding a second language later is a change in one place. The plan defers multi-language UI, so
there is deliberately no i18n machinery here — just constants.

Strings quoted verbatim from the specification are marked ``SPEC``. Their wording is the client's,
including where it reads oddly; changing brand copy is not a developer's call. Anything unmarked
was written to match that voice.

Placeholders use ``str.format``: ``{amount}``, ``{until}``, ``{hours}``.
"""

from datetime import date
from decimal import Decimal

CLUB_NAME = "Your Story Club"

#: ISO currency code -> how it is written to a member. The spec writes sums as "X грн".
CURRENCY_DISPLAY = {"UAH": "грн", "USD": "$", "EUR": "€"}


def money(amount: Decimal, currency: str) -> str:
    """Render a sum the way a member should read it: ``300 грн``, not ``300.00 UAH``.

    Whole amounts lose the decimals — "300 грн" reads like a price, "300.00 грн" like an invoice.
    Signatures and the database keep full precision; this is display only.
    """
    quantised = amount.quantize(Decimal("0.01"))
    shown = (
        quantised.to_integral_value() if quantised == quantised.to_integral_value() else quantised
    )
    return f"{shown} {CURRENCY_DISPLAY.get(currency, currency)}"


def day(value: date) -> str:
    """A date as a member should read it: ``31.12.2026``."""
    return value.strftime("%d.%m.%Y")


def hours_phrase(count: int) -> str:
    """``24`` -> ``"24 години"``. Accusative, as required after «через».

    Ukrainian agrees the noun with the numeral, so a single hard-coded form is wrong for most
    values: «через 24 годин» and «через 5 години» both read as broken software. The rule keys on
    the last digit, with the teens as exceptions (11–14 always take the plural).
    """
    last_two = count % 100
    last = count % 10
    if last == 1 and last_two != 11:
        noun = "годину"
    elif last in (2, 3, 4) and last_two not in (12, 13, 14):
        noun = "години"
    else:
        noun = "годин"
    return f"{count} {noun}"


# --- onboarding -----------------------------------------------------------------------------

#: The club pitch, sent first on /start. The spec's step 2: "інформацію про клуб та тариф" —
#: this is the club half; the tariff follows in its own message so the price is not buried at
#: the bottom of a long read.
#:
#: Owner's copy, verbatim. Note "Q&amp;A": messages go out with parse_mode=HTML, where a bare
#: "&" is invalid and Telegram rejects the whole send.
WELCOME_INTRO = (
    "Вітаю тебе у боті Your Story Club — блогінг ком’юніті для тих, "
    "кому потрібна підтримка у розвитку власного особистого бренду♥️\n"
    "\n"
    "Клуб для тебе, якщо ти\n"
    "• розвиваєш власний блог\n"
    "• лише починаєш у блогінгу\n"
    "• ведеш соцмережі бізнесу\n"
    "\n"
    "Що всередині?\n"
    "🤎 телеграм канал із зручною структурою\n"
    "🤎 інформативні лекції від мене по блогінгу — конкретні інструменти та практики, "
    "які дають результати. Будемо покроково вчитись розвивати власний особистий бренд "
    "та монетизувати блог (власна бібліотека знань клубу)\n"
    "🤎 зустрічі з експертами не лише з блогінгу — цікаві та сильні особистості "
    "з різних сфер\n"
    "🤎 how to — воркшопи та інструменти для роботи з соцмережами\n"
    "🤎 міні-курси та воркшопи по соцмережам і не тільки\n"
    "🤎 спільні зідзвони, мастермайнди, пратики, розбори та Q&amp;A\n"
    "🤎 неформальна онлайн-кава та зустрічі-обговорення (книжки, інфоприводи)\n"
    "🤎 дописи з ідеями для контенту, натхненням та практичними порадами по блогінгу\n"
    "🤎 подарунки для найактивніших учасників\n"
    "\n"
    "Усе це в одному місці — щоб завжди бути на зв’язку🫂 "
    "Чекаю тебе у Your Story Club."
)

WELCOME_FREE = (
    "Вітаємо у {club}! 🎉\n\n"
    "Ви вже з нами, тому перший період — у подарунок. Він діє до <b>{until}</b>.\n\n"
    "Далі підписка становить <b>{amount}</b> на {period} днів і продовжується автоматично. "
    "Ми нагадаємо за день до списання."
)

WELCOME_PAY = (
    "Вітаємо у {club}! 🎉\n\n"
    "Підписка — <b>{amount}</b> на {period} днів.\n\n"
    "Нижче — посилання на оплату. Після першого платежу підписка продовжується "
    "автоматично, тож наступного разу нічого робити не потрібно."
)

#: Used when an invoice could not be created — a gateway outage, or WayForPay not configured yet.
#: Promising a link that never arrives is worse than saying "shortly".
WELCOME_PAY_LATER = (
    "Вітаємо у {club}! 🎉\n\n"
    "Підписка — <b>{amount}</b> на {period} днів.\n\n"
    "Посилання на оплату надішлемо найближчим часом."
)

WELCOME_BACK = "Ви вже зареєстровані. Підписка діє до <b>{until}</b>."

# --- payment --------------------------------------------------------------------------------

PAY_PROMPT = (
    "Час продовжити підписку на {club}: <b>{amount}</b>.\n\nНатисніть кнопку нижче, щоб оплатити."
)
#: Uses {club} rather than the literal name so the button follows CLUB_NAME if it ever changes.
#: The sum is not on the button: it is in the message directly above it, and Telegram truncates
#: long button labels on narrow screens.
PAY_BUTTON = "Стати учасником {club}"

#: SPEC — «Дякуємо! Підписку успішно продовжено»
PAYMENT_RENEWED = "Дякуємо! Підписку успішно продовжено.\n\nДоступ діє до <b>{until}</b>."

#: Sent after the FIRST successful payment. The channel invite follows as its own message,
#: which is what "посилання внизу" refers to. Owner's copy, verbatim — note it deliberately
#: does not quote an expiry date: the point being made is that renewal is automatic. The date
#: is always available from /subscription.
PAYMENT_FIRST_CONFIRMED = (
    "Оплата пройшла успішно, а отже — ти з нами 🤌🏻♥️ Вітаю тебе у {club}! "
    "Не забудь приєднатися до чату — посилання внизу 🫂\n"
    "\n"
    "P.S. клуб у форматі щомісячної підписки, щоб її продовжити, нічого робити не потрібно, "
    "оплата автоматична. Якщо тобі захочеться скасувати підписку, ти в будь-який момент "
    "можеш зробити це через цього бота"
)

#: SPEC — reminder one day before the automatic charge.
RENEWAL_REMINDER = (
    "Завтра відбудеться автоматичне продовження вашої підписки на {club}. "
    "Сума <b>{amount}</b> буде списана з вашої картки. "
    "Переконайтеся, що на картці достатньо коштів!"
)

#: SPEC — the second reminder, after a failed automatic charge.
#: Two corrections to the supplied copy, approved 2026-09-30: «виконати» for the completed
#: attempt, and the hours phrase built by ``hours_phrase`` so the numeral agrees with its noun.
CHARGE_FAILED = (
    "Не вдалося виконати автоматичне списання для продовження підписки. "
    "Ми спробуємо провести платіж ще раз через <b>{retry_in}</b>. "
    "Будь ласка, перевірте баланс або змініть картку в особистому кабінеті."
)

#: How long after a failed charge the next attempt is made.
RETRY_AFTER_HOURS = 24

PAYMENT_DENIED = "Оплату не вдалося провести. Спробуйте, будь ласка, ще раз — кнопка нижче."

# --- losing access ---------------------------------------------------------------------------

#: SPEC — sent when a member is removed from the community for non-payment.
#: Staged: removal is deliberately not enabled yet (see docs/phase-2a-scope.md).
ACCESS_SUSPENDED = (
    "Ваш доступ до ком'юніті призупинено, оскільки не вдалося виконати автоматичне списання. "
    "Ви завжди можете відновити підписку через команду /start"
)

#: What an overdue member is told while removal is still disabled: honest, and not a threat.
PAYMENT_OVERDUE = (
    "Підписку на {club} не продовжено — оплата за <b>{amount}</b> досі не надійшла.\n\n"
    "Натисніть кнопку нижче, щоб оплатити та зберегти доступ."
)

# --- cancelling ------------------------------------------------------------------------------

#: SPEC — button label.
CANCEL_AUTORENEW_BUTTON = "Скасувати автопродовження"

#: SPEC — «Підписка діє до [дата], далі буде скасована»
AUTORENEW_CANCELLED = "Підписка діє до <b>{until}</b>, далі буде скасована."

CANCEL_CONFIRM = (
    "Скасувати автопродовження?\n\n"
    "Доступ залишиться до <b>{until}</b> — ви не втрачаєте те, що вже оплачено. "
    "Далі списань не буде."
)
CANCEL_CONFIRM_YES = "Так, скасувати"
CANCEL_CONFIRM_NO = "Ні, залишити"
CANCEL_NOTHING_TO_CANCEL = "Активної підписки з автопродовженням немає."
CANCEL_ALREADY_CANCELLED = "Автопродовження вже скасовано. Доступ діє до <b>{until}</b>."
RESUME_AUTORENEW_BUTTON = "Відновити автопродовження"
AUTORENEW_RESUMED = "Автопродовження відновлено. Наступне списання — <b>{until}</b>."

# --- the member's own area --------------------------------------------------------------------

# --- the command menu Telegram shows next to the input box ---
#
# Without set_my_commands Telegram lists nothing, so a command is only usable by someone who
# already knows it exists. /admin is registered per-admin, so ordinary members are not shown a
# command they cannot use.

CMD_START = "Почати"
CMD_SUBSCRIPTION = "Моя підписка"
CMD_CANCEL = "Скасувати поточну дію"
CMD_ADMIN = "Адмін-панель"

MENU_KNOWLEDGE_BASE = "📚 База знань"  # SPEC
MENU_NETWORKING = "🤝 Нетворкінг / Каталог"  # SPEC
MENU_MY_SUBSCRIPTION = "💳 Моя підписка"
MENU_MY_PROFILE = "👤 Мій профіль"

#: The member's own subscription card. Two variants, because one line cannot honestly cover
#: both cases: a paid-up member has a date their access runs to, while someone who owes money
#: has no such date — saying «Діє до <today>» to them reads as "valid until today", which is
#: the opposite of the truth.
SUBSCRIPTION_STATUS = (
    "<b>Ваша підписка</b>\n\nСтатус: {status}\nВартість: {amount} на {period} днів\nДіє до: {until}"
)

SUBSCRIPTION_STATUS_UNPAID = (
    "<b>Ваша підписка</b>\n"
    "\n"
    "Статус: {status}\n"
    "До оплати: {amount} за {period} днів\n"
    "Термін вийшов: {until}\n"
    "\n"
    "Після оплати доступ буде діяти до {next_until}."
)

STATUS_NAMES = {
    "trial": "пробний період",
    "active": "активна",
    "past_due": "очікує оплати",
    "expired": "неактивна",
    "cancelled": "скасована",
}

#: Access to everything inside the bot follows the subscription (spec: knowledge base and the
#: catalogue are for active members only).
NEEDS_ACTIVE_SUBSCRIPTION = (
    "Цей розділ доступний лише учасникам з активною підпискою.\n\n"
    "Оплатіть підписку, щоб відкрити доступ."
)

# --- errors -----------------------------------------------------------------------------------

GENERIC_ERROR = "Щось пішло не так з нашого боку. Спробуйте, будь ласка, ще раз за хвилину."

# --- admin ------------------------------------------------------------------------------------

ADMIN_ACCESS_DENIED = "У вас немає доступу."
ADMIN_MENU_TITLE = "Адмін-панель"
ADMIN_NOT_BUILT_YET = "«{label}» буде додано на наступному етапі."

ADMIN_MENU_STATISTICS = "📊 Статистика"
ADMIN_MENU_BROADCAST = "📣 Розсилка"
ADMIN_MENU_USERS = "👥 Учасники"
ADMIN_MENU_KNOWLEDGE_BASE = "📚 База знань"
ADMIN_MENU_ADD_SUBSCRIBER = "➕ Додати учасника"
ADMIN_MENU_DISCOUNTS = "🎟 Знижки"
ADMIN_MENU_GRANT_DISCOUNT = "🎁 Надати знижку"
ADMIN_MENU_SEND_INVITE = "🔗 Надіслати запрошення"
ADMIN_MENU_MESSAGE_USER = "✍️ Написати учаснику"

# --- the participants list ---
#
# These are people who have started the *bot*. A bot cannot enumerate a channel's members, so
# this is not a channel roster and the message says so — otherwise the count looks wrong.

ADMIN_USERS_EMPTY = "Ще ніхто не запускав бота."

#: The 👥 Учасники screen opens on counts alone, with a button per group. A flat roster grew to
#: 85 people and several messages, and it only grows; this stays one message however large the
#: club gets, and an admin opens only the group they care about.
ADMIN_USERS_SUMMARY = "<b>Учасники бота</b> — {total}\n\nОберіть групу:"

#: Group labels. Also the button text, with the count appended.
ADMIN_GROUP_AUTO = "🔄 Автопродовження"
ADMIN_GROUP_CANCELLED = "⏹ Скасували автопродовження"
ADMIN_GROUP_TRIAL = "🎁 Пробний період"
ADMIN_GROUP_UNPAID = "⏳ Очікують оплати"
ADMIN_GROUP_LIFETIME = "♾ Безстрокові"
ADMIN_GROUP_NO_SUB = "❓ Без підписки"

ADMIN_USERS_GROUP_HEADER = "<b>{label}</b> — {count}\n"
ADMIN_USERS_GROUP_EMPTY = "У цій групі нікого немає."

#: What each group means, under its heading — the difference between "cancelled" and "awaiting
#: payment" is not obvious from the name, and acting on the wrong group costs a member.
ADMIN_GROUP_NOTES = {
    "auto": "Оплачено, наступне списання автоматичне.",
    "cancelled": "Оплачено, але автопродовження вимкнено — доступ до вказаної дати.",
    "trial": "Безкоштовний перший період, оплати ще не було.",
    "unpaid": "Доступу немає: не оплатили або підписка завершилася.",
    "lifetime": "Доступ без дати завершення.",
    "no_sub": "Запустили бота, але підписки немає — таке можливе лише після ручної правки.",
}
#: One line per member: handle, status, price, until.
ADMIN_USER_LINE = "• {handle} — {status}, {amount}, до {until}"
ADMIN_USERS_FOOTER = (
    "\nЦе ті, хто запустив бота. Перелік учасників каналу бот отримати не може — "
    "Telegram такого не дозволяє."
)

ADMIN_DISCOUNTS_EMPTY = "Зараз немає активних знижок."
ADMIN_DISCOUNTS_HEADER = "<b>Активні знижки</b> — {count}\n"
#: One line per discount. `who` is @username or an id, `what` is «20%» or «8 €».
ADMIN_DISCOUNT_LINE = "• {who} — {what}{until}{claimed}{note}"
ADMIN_DISCOUNT_FOREVER = ", без обмеження в часі"
ADMIN_DISCOUNT_UNTIL = ", до {until}"
ADMIN_DISCOUNT_NOT_CLAIMED = " (ще не активував бота)"
ADMIN_DISCOUNTS_UNCLAIMED_NOTE = (
    "\n{count} зі списку ще не активували бота — доки вони цього не зроблять, знижка не працює."
)

# --- granting a discount, step by step ---

ADMIN_GRANT_ASK_WHO = (
    "Кому надати знижку?\n\n"
    "Надішліть <b>@username</b> або числовий <b>id</b> учасника.\n"
    "Можна вказати username людини, яка ще не запускала бота — знижка застосується, "
    "коли вона це зробить.\n\n"
    "/cancel — скасувати."
)
ADMIN_GRANT_ASK_KIND = "Який тип знижки для {who}?"
ADMIN_GRANT_KIND_PERCENT = "Відсоток"
ADMIN_GRANT_KIND_FIXED = "Фіксована ціна"
ADMIN_GRANT_ASK_PERCENT = (
    "Надішліть відсоток знижки — число від 1 до 100.\n\nНаприклад: <code>20</code>\n\n/cancel"
)
ADMIN_GRANT_ASK_FIXED = (
    "Надішліть фіксовану ціну в {currency}.\n\nНаприклад: <code>8</code>\n\n/cancel"
)
ADMIN_GRANT_ASK_PERIOD = "На який термін діє знижка для {who}?"
ADMIN_GRANT_PERIOD_FOREVER = "Без обмеження"
ADMIN_GRANT_PERIOD_DAYS = "{days} днів"
ADMIN_GRANT_ASK_NOTE = (
    "Додайте коментар — навіщо ця знижка (наприклад: <code>учень</code>).\n\n"
    "Або натисніть «Пропустити».\n\n/cancel"
)
ADMIN_GRANT_SKIP_NOTE = "Пропустити"
ADMIN_GRANT_CONFIRM = (
    "<b>Підтвердіть знижку</b>\n\n"
    "Кому: {who}\n"
    "Знижка: {what}\n"
    "Термін: {period}\n"
    "Коментар: {note}\n\n"
    "Нова ціна замість {base}: <b>{new_price}</b>"
)
ADMIN_GRANT_CONFIRM_BUTTON = "✅ Надати"
ADMIN_GRANT_CANCEL_BUTTON = "✖️ Скасувати"
ADMIN_GRANT_DONE = "Знижку надано: {who} — {what}{period}."
ADMIN_GRANT_CANCELLED = "Скасовано."
# --- 🔗 Надіслати запрошення (admin sends a channel link by hand) ----------------------------
#
# For a member who paid but never got in: the bot failed to deliver a link, or they never used
# the one they got. The admin names them and the bot creates a fresh single-use link.

ADMIN_INVITE_ASK_WHO = (
    "Кому надіслати запрошення в закритий канал?\n\n"
    "Надішліть @username або числовий id.\n"
    "Щоб скасувати — /cancel"
)

ADMIN_INVITE_BAD_TARGET = "Не схоже на @username або id. Спробуйте ще раз або /cancel"

#: The bot cannot message someone it has never spoken to, and cannot turn a username into an id.
ADMIN_INVITE_UNKNOWN = (
    "Не знайшов такого учасника серед тих, хто запускав бота.\n\n"
    "Бот не може надіслати повідомлення першим тому, хто не натискав /start, "
    "і не може знайти id за @username. Попросіть учасника відкрити бота — "
    "або надішліть числовий id, якщо він у вас є."
)

ADMIN_INVITE_CONFIRM = (
    "Надіслати запрошення?\n\n"
    "Учасник: {handle}\n"
    "Підписка: {status}{until}\n\n"
    "Посилання буде одноразовим і діятиме {days} дні."
)

#: Shown inside the confirmation when the subscription is not active. Not a refusal: an admin
#: sending a link by hand is usually fixing exactly this — a payment the bot failed to record.
ADMIN_INVITE_CONFIRM_WARNING = (
    "\n\n⚠️ Підписка не активна. Запрошення все одно буде надіслано, якщо ви підтвердите."
)

ADMIN_INVITE_CONFIRM_YES = "✅ Надіслати"
ADMIN_INVITE_CONFIRM_NO = "Скасувати"

ADMIN_INVITE_SENT = "✅ Запрошення надіслано: {handle}"

ADMIN_INVITE_UNREACHABLE = (
    "Посилання створено, але надіслати не вдалося: {handle} заблокував бота "
    "або не запускав його. Передайте посилання іншим шляхом:\n\n{link}"
)

ADMIN_INVITE_NO_LINK = (
    "Не вдалося створити посилання. Перевірте, що бот — адміністратор каналу "
    "з правом «Запрошувати користувачів», і що CHANNEL_ID заданий."
)

ADMIN_GRANT_BAD_TARGET = (
    "Не розпізнав. Надішліть <b>@username</b> або числовий <b>id</b>, або /cancel."
)
ADMIN_GRANT_BAD_PERCENT = "Потрібне число від 1 до 100. Спробуйте ще раз або /cancel."
ADMIN_GRANT_BAD_PRICE = "Потрібне число більше за нуль. Спробуйте ще раз або /cancel."

ADMIN_DISCOUNT_REVOKE_BUTTON = "Скасувати знижку"
ADMIN_DISCOUNT_REVOKED = "Знижку для {who} скасовано."

ADMIN_PAYMENT_MISSING = (
    "⚠️ <b>Немає оплати</b>\n\n"
    "Учасник: {handle} (id <code>{user_id}</code>)\n"
    "Сума: {amount}\n"
    "Термін вийшов: {since}\n"
    "Тариф: {tier}\n\n"
    "З каналу не видалено — автоматичне видалення ще не увімкнене."
)

# --- staged for later phases ------------------------------------------------------------------
#
# Written now because the spec supplies the wording; not referenced by any handler yet.

#: SPEC — the single-use invite the bot generates after a successful payment.
INVITE_TO_COMMUNITY = (
    "Ось ваше персональне посилання для входу в закритий канал {club}:\n\n{link}\n\n"
    "Посилання одноразове та діє {days} дні — не передавайте його іншим."
)

#: Payment accepted, but the channel is misconfigured. Never leave a payer with nothing.
INVITE_UNAVAILABLE = (
    "Оплату отримано, дякуємо! Посилання на закритий канал надішлемо вручну — ми вже про це знаємо."
)
#: Sent to every admin when the bot is added to a chat. The chat id is the one thing an
#: operator cannot look up for themselves — an invite link cannot be resolved to an id by the
#: Bot API — so the bot reports it the moment it learns it.
#: Recovering the id of a chat the bot was already added to. Telegram does not re-send the
#: join event, and no API call lists a bot's chats, so the id has to arrive on an update:
#: a forwarded channel message, or a post the bot sees as an administrator.
ADMIN_CHAT_ID_FOUND = (
    "📎 <b>Знайдено чат</b>\n"
    "\n"
    "Назва: {title}\n"
    "Тип: {chat_type}\n"
    "CHANNEL_ID: <code>{chat_id}</code>\n"
    "\n"
    "Додайте цей CHANNEL_ID у .env і перезапустіть бота."
)

ADMIN_ADDED_TO_CHAT = (
    "✅ <b>Бот додано до чату</b>\n"
    "\n"
    "Назва: {title}\n"
    "Тип: {chat_type}\n"
    "CHANNEL_ID: <code>{chat_id}</code>\n"
    "Статус бота: {status}\n"
    "\n"
    "Права:\n"
    "{rights}\n"
    "\n"
    "Додайте цей CHANNEL_ID у .env і перезапустіть бота."
)

ADMIN_RIGHT_OK = "✅ {name}"
ADMIN_RIGHT_MISSING = "❌ {name} — потрібно увімкнути"
ADMIN_RIGHT_INVITE = "Запрошувати користувачів"
ADMIN_RIGHT_BAN = "Видаляти учасників"

#: The bot was added as an ordinary member, which is not enough to do anything useful.
ADMIN_ADDED_NOT_ENOUGH = (
    "⚠️ Бота додано до «{title}», але не адміністратором.\n"
    "CHANNEL_ID: <code>{chat_id}</code>\n"
    "\n"
    "Зробіть бота адміністратором з правами «Запрошувати користувачів» та «Видаляти учасників»."
)

# --- ✍️ Написати учаснику (admin initiates, the bot delivers) --------------------------------
#
# The way to reach a member who has no @username: an admin cannot open that chat by hand, but
# the bot already has one with everybody who pressed /start.

ADMIN_MESSAGE_ASK_WHO = (
    "Кому написати?\n\nНадішліть @username або числовий id.\nЩоб скасувати — /cancel"
)

ADMIN_MESSAGE_ASK_TEXT = (
    "Що надіслати {handle}?\n\n"
    "Надішліть його одним повідомленням. Воно піде від імені клубу.\n"
    "Щоб скасувати — /cancel"
)

ADMIN_MESSAGE_CONFIRM = "Надіслати це {handle}?\n\n— — —\n{preview}\n— — —"

ADMIN_MESSAGE_CONFIRM_YES = "✅ Надіслати"
ADMIN_MESSAGE_SENT = "✅ Надіслано: {handle}"
ADMIN_MESSAGE_UNREACHABLE = (
    "Не вдалося надіслати: {handle} заблокував бота або не запускав його. "
    "Повідомлення не доставлено."
)
ADMIN_MESSAGE_EMPTY = "Повідомлення порожнє. Напишіть текст або /cancel"

#: How an admin's message arrives for the member. Marked as coming from a person, not the bot:
#: a bare forwarded string reads like an automated notice and gets ignored.
MESSAGE_FROM_ADMIN = "Повідомлення від адміністратора {club}:\n\n{text}"

# --- the report an admin gets after the bot boots --------------------------------------------
#
# The startup sweep acts on its own, on money and on access. Everything it did is reported to
# an admin in one message, because a recovery nobody is told about is indistinguishable from a
# recovery that never ran.

ADMIN_STARTUP_HEADER = "🔄 <b>Перевірка після запуску</b>\n"
ADMIN_STARTUP_RECOVERED = "\n💳 Знайдено оплату, доступ поновлено — {count}:\n"
ADMIN_STARTUP_LINKS = "\n🔗 Надіслано посилання на канал — {count}:\n"
ADMIN_STARTUP_NEEDS_YOU = "\n⚠️ Не вдалося — потрібні ви — {count}:\n"
ADMIN_STARTUP_LINE = "• {handle} · id <code>{user_id}</code>{extra}\n"
ADMIN_STARTUP_MORE = "… і ще {count}\n"
ADMIN_STARTUP_UNDELIVERED = " — не доставлено"

#: A whole step failed (WayForPay unreachable, Telegram refusing). The bot keeps running, but
#: an admin has to know that the sweep did not finish.
ADMIN_STARTUP_STEP_FAILED = "\n❗️ Крок «{step}» не виконався. Деталі — у логах.\n"

ADMIN_STARTUP_FOOTER = (
    "\nЩоб написати комусь із них — ✍️ Написати учаснику, "
    "щоб надіслати посилання ще раз — 🔗 Надіслати запрошення."
)

#: One paid member is still outside the channel after a retry. Everything an admin needs to
#: finish it by hand is in the line: the handle to search, the id the 🔗 screen accepts, the
#: name for someone with no username, and why the automatic path did not work.
ADMIN_INVITE_ESCALATION = (
    "⚠️ <b>Учасник оплатив, але не в каналі</b>\n\n"
    "{handle} · id <code>{user_id}</code> · {name}\n"
    "Причина: {reason}\n\n"
    "Запрошення вже надсилалося двічі. Надішліть вручну: 🔗 Надіслати запрошення — "
    "або напишіть людині: ✍️ Написати учаснику."
)

#: Why the automatic path did not finish. Admin-facing, one per branch.
INVITE_REASON_NEVER_SENT = "посилання жодного разу не надсилалося"
INVITE_REASON_NOT_USED = "посилання надіслано, але учасник не приєднався"
INVITE_REASON_NO_LINK = "не вдалося створити посилання (права бота в каналі?)"
INVITE_REASON_UNREACHABLE = "учасник заблокував бота або не запускав його"
INVITE_REASON_UNKNOWN = "не вдалося перевірити участь у каналі: {error}"

ADMIN_INVITE_FAILED = (
    "⚠️ <b>Не вдалося створити запрошення</b>\n\n"
    "Учасник: {handle} (id <code>{user_id}</code>)\n"
    "Оплату отримано, але посилання не створилося.\n\n"
    "Перевірте, що бот — адміністратор каналу з правом «Запрошувати користувачів», "
    "і що CHANNEL_ID заданий. Надішліть запрошення вручну."
)

#: SPEC — default knowledge-base categories.
DEFAULT_CATEGORIES = (
    "📂 Стратегія та Позиціонування",
    "📂 Контент-стратегія",
    "📂 Таргет та Залучення аудиторії",
    "📂 Записи лекцій та воркшопів",
    "📂 Шаблони та Чеклісти",
)

#: SPEC — the member card in the networking catalogue.
PROFILE_CARD = (
    "👤 <b>{name}</b> | {occupation}\n\n"
    "📍 <b>Локація:</b> {city}\n"
    "🔗 <b>Блог:</b> {blog}\n"
    "💡 <b>Ніша:</b> {niche}\n"
    "🎯 <b>Шукаю:</b> {looking_for}\n"
    "🤝 <b>Корисна тим:</b> {offers}"
)
PROFILE_WRITE_BUTTON = "💌 Написати в особисті"  # SPEC
PROFILE_SAVE_BUTTON = "⭐️ Зберегти в обране"  # SPEC
PROFILE_PUBLISH_BUTTON = "Опублікувати мій профіль"  # SPEC
