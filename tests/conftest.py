"""Shared fixtures: an in-memory database, a Bot that records instead of calling Telegram,
and a dispatcher wired exactly as production wires it.

``aiogram.test_utils`` is not shipped in the installed aiogram, so ``RecordingSession`` below
is the test double: it satisfies ``BaseSession`` and captures every outgoing API call. Tests
then assert on what the bot *would have sent*, which is the only thing the user sees.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatInviteLink,
    ChatMemberLeft,
    ChatMemberMember,
    Message,
    Update,
    User,
)
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings
from db.models import Base
from db.session import create_engine, create_session_factory

# aiogram's validate_token only requires "<digits>:<non-empty>", so this is deliberately
# nothing like a real token. A realistic-looking one matched the pre-commit hook's
# real-token pattern and blocked every commit — the fixture was the thing that should give way,
# not the hook.
FAKE_TOKEN = "123456789:FAKE"  # noqa: S105

ADMIN_ID = 111_111_111
USER_ID = 222_222_222


class RecordingSession(BaseSession):
    """Captures outgoing API calls instead of performing them."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        #: Canned results, keyed by aiogram method class name, e.g. {"GetChatMember": <obj>}.
        #: GetChatMember defaults to "left" rather than None, because real Telegram always
        #: returns a ChatMember — a None default would let tests pass against a shape the API
        #: never produces.
        #: Realistic defaults, because real Telegram never returns None. A None default lets a
        #: test pass against a response shape the API cannot produce.
        self.responses: dict[str, Any] = {
            "GetChatMember": ChatMemberLeft.model_validate(
                {"status": "left", "user": {"id": 0, "is_bot": False, "first_name": "Nobody"}}
            ),
            "CreateChatInviteLink": ChatInviteLink.model_validate(
                {
                    "invite_link": "https://t.me/+default",
                    "creator": {"id": 0, "is_bot": True, "first_name": "bot"},
                    "creates_join_request": False,
                    "is_primary": False,
                    "is_revoked": False,
                }
            ),
        }
        #: Exceptions to raise instead of returning, keyed the same way.
        self.failures: dict[str, Exception] = {}

    async def close(self) -> None:
        pass

    # ASYNC109: `timeout` is part of the BaseSession signature being overridden, not a
    # choice we can make differently here.
    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod,
        timeout: int | None = None,  # noqa: ASYNC109
    ):
        name = type(method).__name__
        self.calls.append(method)
        if name in self.failures:
            raise self.failures[name]
        return self.responses.get(name)

    async def stream_content(self, *args: Any, **kwargs: Any):
        yield b""

    # --- assertions helpers ---

    def method_names(self) -> list[str]:
        return [type(call).__name__ for call in self.calls]

    def of_type(self, name: str) -> list[TelegramMethod]:
        return [call for call in self.calls if type(call).__name__ == name]

    def sent_texts(self) -> list[str]:
        """Text of every sendMessage, plus the text of every answerCallbackQuery."""
        texts: list[str] = []
        for call in self.calls:
            text = getattr(call, "text", None)
            if text is not None:
                texts.append(text)
        return texts


@pytest.fixture
async def engine(tmp_path):
    """A fresh SQLite database per test, with the schema created from the models.

    A **file**, not ``:memory:``. An in-memory SQLite database belongs to its connection, so any
    reconnect — which ``pool_pre_ping`` will do after a failed statement — silently hands back an
    empty database and every later query fails with "no such table". A rolled-back transaction is
    ordinary production behaviour, so the harness must survive it.

    The schema comes from ``Base.metadata`` rather than from Alembic so a test failure points at
    the models. That migrations reproduce this schema is proven separately by
    ``alembic upgrade head`` in CI (S7 / G1.6).
    """
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture
async def session(session_factory) -> AsyncSession:
    async with session_factory() as session:
        yield session


REGULAR_PRICE = Decimal("300.00")
CURRENCY = "UAH"
PERIOD_DAYS = 30
CHANNEL_ID = "-1001234567890"


@pytest.fixture
def settings() -> Settings:
    """Settings built explicitly, never read from the developer's environment."""
    return Settings(
        bot_token=FAKE_TOKEN,
        admin_ids=str(ADMIN_ID),
        channel_id=CHANNEL_ID,
        database_url="sqlite+aiosqlite:///:memory:",
        subscription_price=REGULAR_PRICE,
        subscription_currency=CURRENCY,
        subscription_period_days=PERIOD_DAYS,
        wayforpay_merchant_account="test_merchant",
        wayforpay_merchant_domain="example.com",
        wayforpay_secret_key="test_secret",
    )


def chat_member(status: str, user_id: int = USER_ID) -> dict[str, Any]:
    """A getChatMember result aiogram will parse into a ChatMember subclass."""
    return {
        "status": status,
        "user": {"id": user_id, "is_bot": False, "first_name": "Test"},
    }


@pytest.fixture
def in_community(recording_session):
    """Make the next membership check say "yes, a member"."""

    def _set(status: str = "member", user_id: int = USER_ID) -> None:
        recording_session.responses["GetChatMember"] = ChatMemberMember.model_validate(
            chat_member(status, user_id)
        )

    return _set


@pytest.fixture
def recording_session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
async def bot(recording_session) -> Bot:
    bot = Bot(FAKE_TOKEN, session=recording_session)
    yield bot
    await bot.session.close()


class FakeWayForPayClient:
    """Stands in for the gateway. Records what was asked; never touches the network.

    The suite must not call a real payment API: it would be slow, flaky, and would send live
    requests with whatever credentials happen to be configured.
    """

    def __init__(self) -> None:
        self.invoice_calls: list[dict[str, Any]] = []
        self.invoice_error: Exception | None = None

    async def create_invoice(self, **kwargs: Any):
        from services.wayforpay import Invoice

        self.invoice_calls.append(kwargs)
        if self.invoice_error is not None:
            raise self.invoice_error
        order = kwargs["order_reference"]
        return Invoice(
            order_reference=order,
            invoice_url=f"https://secure.wayforpay.com/page?o={order}",
            raw={"reasonCode": "Ok"},
        )

    async def check_status(self, *, order_reference: str):  # pragma: no cover - unused here
        raise AssertionError("check_status is exercised in tests/test_billing.py")


@pytest.fixture
def fake_wayforpay() -> FakeWayForPayClient:
    return FakeWayForPayClient()


@pytest.fixture
def dispatcher(settings, session_factory, fake_wayforpay) -> Dispatcher:
    """The production dispatcher: same middleware, same routers, same order.

    The gateway client is a fake. ``build_dispatcher`` takes it as a parameter precisely so a test
    cannot accidentally get a real one built from its fake credentials.
    """
    from main import build_dispatcher

    return build_dispatcher(settings, session_factory, wayforpay=fake_wayforpay)


# --- update builders -------------------------------------------------------------------


def make_user(user_id: int, username: str | None = "someone", first_name: str = "Test") -> User:
    return User(id=user_id, is_bot=False, first_name=first_name, username=username)


def make_message(user: User, text: str, update_id: int = 1) -> Update:
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=user.id, type="private"),
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)


def make_callback(user: User, data: str, update_id: int = 1) -> Update:
    """A callback query carrying an arbitrary ``data`` string.

    Nothing stops a real client from sending any string here — which is exactly the attack
    G1.5 describes.
    """
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=user.id, type="private"),
        from_user=user,
    )
    query = CallbackQuery(
        id=str(update_id),
        from_user=user,
        chat_instance="test-chat-instance",
        data=data,
        message=message,
    )
    return Update(update_id=update_id, callback_query=query)
