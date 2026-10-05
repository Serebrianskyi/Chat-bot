"""The whole test suite: the paths where a bug costs money, access or trust.

Kept deliberately small. Each test here guards something that has actually gone wrong, or that
cannot be checked by looking at the code: signature construction, idempotent money handling,
access control, and the arithmetic behind a discount that expires.

What is *not* tested: wording, menu layout, every branch of every message. Those are visible the
moment the bot runs, and covering them was costing more than it caught. Phase gates in
docs/gates/ still describe the manual checks.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

import texts
from config import ConfigError, Settings, get_settings
from db.models import (
    AuditLog,
    Discount,
    DiscountKind,
    Payment,
    PaymentStatus,
    PriceTier,
    Subscription,
    SubscriptionStatus,
    User,
)
from db.session import create_engine
from services import discounts as discount_service
from services.billing import BillingConfig, poll_open_payments, process_due_subscriptions
from services.pricing import decide_price, extend, first_expiry
from services.wayforpay import (
    RESPONSE_SIGNATURE_FIELDS,
    SignatureMismatch,
    WayForPayClient,
    WayForPayError,
    map_status,
)
from tests.conftest import (
    ADMIN_ID,
    CHANNEL_ID,
    CURRENCY,
    PERIOD_DAYS,
    REGULAR_PRICE,
    USER_ID,
    make_callback,
    make_message,
    make_user,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


# ======================================================================================
# Configuration — a wrong value here is a silent outage
# ======================================================================================


def test_missing_or_blank_required_vars_stop_startup(monkeypatch, tmp_path):
    """S5. A blank BOT_TOKEN once passed validation, leaving ADMIN_IDS empty and /admin
    refusing everybody with no explanation."""
    monkeypatch.chdir(tmp_path)  # so no .env on disk is picked up
    for name in ("BOT_TOKEN", "ADMIN_IDS"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    with pytest.raises(ConfigError) as exc:
        get_settings()
    assert "BOT_TOKEN" in str(exc.value) and "ADMIN_IDS" in str(exc.value)
    get_settings.cache_clear()


def test_secrets_never_appear_in_repr():
    """One log.debug("%s", settings) would otherwise publish the key that signs payments.

    The fixture values are deliberately short and unlike real credentials: the pre-commit hook
    blocks anything resembling a live token or payment key, and a test fixture is not a good
    reason to weaken it.
    """
    s = Settings(
        bot_token="123:tok",  # noqa: S106
        admin_ids="1",
        wayforpay_secret_key="shh",  # noqa: S106
    )
    for rendered in (repr(s), str(s), s.model_dump_json()):
        assert "123:tok" not in rendered
        assert "shh" not in rendered


def test_a_host_supplied_database_url_gets_an_async_driver():
    """Railway hands over a libpq URL with no driver named. `postgres://` fails outright and
    `postgresql://` works only by a SQLAlchemy default, so both are pinned."""
    for given in ("postgres://u:p@h/d", "postgresql://u:p@h/d"):
        s = Settings(bot_token="1:x", admin_ids="1", database_url=given)  # noqa: S106
        assert s.database_url.startswith("postgresql+psycopg://")
        assert create_engine(s.database_url).dialect.is_async is True


# ======================================================================================
# Pricing — who pays what, and who gets a free month
# ======================================================================================


def decide(**over):
    kwargs = {
        "regular_price": REGULAR_PRICE,
        "regular_currency": CURRENCY,
        "has_discount": False,
        "in_community": False,
        "now": NOW,
        "free_period_until": None,
    }
    kwargs.update(over)
    return decide_price(**kwargs)


def test_who_pays_what_and_who_gets_a_free_month():
    """The three-way decision, plus the rule that keeps price and the free month separate.

    Conflating them once gave a free month to anyone with a discount, including people who had
    never been in the channel.
    """
    joiner = decide()
    assert (joiner.price, joiner.tier, joiner.first_period_free) == (
        REGULAR_PRICE,
        PriceTier.REGULAR,
        False,
    )

    member = decide(in_community=True)
    assert (member.tier, member.first_period_free) == (PriceTier.COMMUNITY, True)

    # A discount is about price alone.
    discounted = decide(has_discount=True)
    assert (discounted.tier, discounted.first_period_free) == (PriceTier.DISCOUNTED, False)

    # The seeded founding members carry both, and most cannot be detected as channel members
    # because a bot cannot enumerate a channel.
    assert decide(has_discount=True, discount_grants_free_period=True).first_period_free is True


def test_the_free_period_ends_on_a_fixed_date_not_n_days_after_joining():
    """ "Free until the end of the month" has to mean one shared date. Staggered 30-day windows
    would leave the club billing people on 30 different days of the month, forever.

    The same date also closes the offer: after it, nobody gets a free period, which is what stops
    someone joining the open channel later and claiming a free month."""
    first_of_november = datetime(2026, 11, 1, tzinfo=UTC)

    # Joining on the 1st and on the 28th both end on the same day.
    for day in (1, 28):
        joined = datetime(2026, 10, day, 12, 0, tzinfo=UTC)
        decision = decide(in_community=True, now=joined, free_period_until=first_of_november)
        assert decision.first_period_free is True
        assert (
            first_expiry(decision, now=joined, period_days=30, free_period_until=first_of_november)
            == first_of_november
        )

    # On or after the date, no free period at all: billing is monthly from the start.
    after = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    late = decide(in_community=True, now=after, free_period_until=first_of_november)
    assert late.first_period_free is False
    assert (
        first_expiry(late, now=after, period_days=30, free_period_until=first_of_november) == after
    )


def test_extending_never_loses_or_backdates_days():
    """Paying early keeps the days already held; paying late starts today rather than backdating
    into the gap."""
    future = NOW + timedelta(days=10)
    assert extend(future, now=NOW, period_days=30) == future + timedelta(days=30)
    assert extend(NOW - timedelta(days=10), now=NOW, period_days=30) == NOW + timedelta(days=30)


def test_discount_arithmetic():
    base = Decimal("10.00")
    cases = [
        (Discount(kind=DiscountKind.PERCENT, percent_off=20, currency=CURRENCY), "8.00"),
        (Discount(kind=DiscountKind.PERCENT, percent_off=33, currency=CURRENCY), "6.70"),
        (
            Discount(kind=DiscountKind.FIXED_PRICE, fixed_price=Decimal("8.00"), currency=CURRENCY),
            "8.00",
        ),
    ]
    for discount, expected in cases:
        amount, _ = discount_service.apply_to(base, CURRENCY, discount)
        assert amount == Decimal(expected)


def test_an_expired_discount_stops_applying():
    """The reason the charged amount is computed per invoice rather than stored: a snapshot
    would make every time-limited discount permanent."""
    d = Discount(kind=DiscountKind.PERCENT, percent_off=50, valid_until=NOW)
    assert d.is_active(NOW - timedelta(seconds=1)) is True
    assert d.is_active(NOW) is False


# ======================================================================================
# Onboarding
# ======================================================================================


async def load_sub(session_factory, telegram_id=USER_ID):
    async with session_factory() as session:
        return await session.scalar(select(Subscription).where(Subscription.user_id == telegram_id))


async def test_start_registers_once_and_invoices_a_paying_joiner(
    dispatcher, bot, session_factory, fake_wayforpay, recording_session
):
    """Two /start commands must not create two subscriptions, and the welcome promises a payment
    link — so one has to actually be sent."""
    user = make_user(USER_ID, username="newcomer")
    await dispatcher.feed_update(bot, make_message(user, "/start", update_id=1))
    await dispatcher.feed_update(bot, make_message(user, "/start", update_id=2))

    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(User))).scalar_one() == 1
        assert (
            await session.execute(select(func.count()).select_from(Subscription))
        ).scalar_one() == 1
    assert len(fake_wayforpay.invoice_calls) == 1

    # The first /start sends two messages, not three: the club pitch, then the tariff carrying
    # the pay button. The button used to arrive in a message of its own headed "time to renew",
    # which made no sense for somebody who had just joined. The second /start adds one more
    # message, the "already registered" reply.
    sent = recording_session.of_type("SendMessage")
    assert len(sent) == 3
    buttons = [b for row in sent[1].reply_markup.inline_keyboard for b in row]
    assert buttons[0].text == texts.PAY_BUTTON.format(club=texts.CLUB_NAME)
    assert "secure.wayforpay.com" in buttons[0].url
    assert texts.MENU_MY_SUBSCRIPTION in [b.text for b in buttons]


async def test_a_founding_member_is_matched_pinned_and_given_a_free_month(
    dispatcher, bot, session_factory, fake_wayforpay
):
    """Username matching happens once, then the id is pinned — a later rename must not lose the
    price they were promised."""
    async with session_factory() as session:
        session.add(
            Discount(
                username="founder",
                kind=DiscountKind.FIXED_PRICE,
                fixed_price=Decimal("8.00"),
                currency=CURRENCY,
                free_first_period=True,
            )
        )
        await session.commit()

    await dispatcher.feed_update(
        bot, make_message(make_user(USER_ID, username="FoUnDeR"), "/start")
    )

    sub = await load_sub(session_factory)
    assert sub.price_tier is PriceTier.DISCOUNTED
    assert sub.free_period_granted is True
    assert sub.expires_at == sub.started_at + timedelta(days=PERIOD_DAYS)
    assert fake_wayforpay.invoice_calls == []  # nothing owed yet
    async with session_factory() as session:
        claimed = await session.scalar(select(Discount))
    assert claimed.user_id == USER_ID


async def test_a_failed_membership_check_fails_closed(
    dispatcher, bot, session_factory, recording_session
):
    """Treating an API error as "is a member" would hand out free months on a network blip."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetChatMember

    recording_session.failures["GetChatMember"] = TelegramBadRequest(
        method=GetChatMember(chat_id="-100", user_id=USER_ID), message="chat not found"
    )
    await dispatcher.feed_update(bot, make_message(make_user(USER_ID), "/start"))

    sub = await load_sub(session_factory)
    assert sub.price_tier is PriceTier.REGULAR
    assert sub.free_period_granted is False


async def test_a_gateway_outage_still_registers_the_member(
    dispatcher, bot, session_factory, fake_wayforpay
):
    """Registration is committed before the invoice; an outage must not undo it or leave an
    orphan payment row that blocks the next attempt."""
    fake_wayforpay.invoice_error = WayForPayError("gateway down")

    await dispatcher.feed_update(bot, make_message(make_user(USER_ID), "/start"))

    assert await load_sub(session_factory) is not None
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(Payment))).scalar_one() == 0


# ======================================================================================
# Access control
# ======================================================================================


async def test_a_non_admin_cannot_reach_the_admin_panel(dispatcher, bot, recording_session):
    """Hiding a button is not access control: anyone can send the callback data by hand."""
    from handlers.admin import ACCESS_DENIED, AdminMenu

    await dispatcher.feed_update(bot, make_message(make_user(USER_ID), "/admin", update_id=1))
    await dispatcher.feed_update(
        bot, make_callback(make_user(USER_ID), AdminMenu(action="users").pack(), update_id=2)
    )
    await dispatcher.feed_update(bot, make_callback(make_user(USER_ID), "menu:admin", update_id=3))

    refusals = [
        c.text for c in recording_session.calls if getattr(c, "text", None) == ACCESS_DENIED
    ]
    assert len(refusals) == 3
    assert texts.ADMIN_MENU_TITLE not in [c.text for c in recording_session.of_type("SendMessage")]


async def test_an_admin_gets_the_panel(dispatcher, bot, recording_session):
    await dispatcher.feed_update(bot, make_message(make_user(ADMIN_ID), "/admin"))

    sent = recording_session.of_type("SendMessage")[0]
    assert sent.text == texts.ADMIN_MENU_TITLE
    labels = [b.text for row in sent.reply_markup.inline_keyboard for b in row]
    assert texts.ADMIN_MENU_DISCOUNTS in labels
    assert texts.ADMIN_MENU_USERS in labels


async def test_the_admin_gate_reads_the_environment_not_the_database(
    dispatcher, bot, session_factory, recording_session
):
    """If this fails, privilege escalation is one UPDATE away."""
    from db.models import UserRole
    from handlers.admin import ACCESS_DENIED

    await dispatcher.feed_update(bot, make_message(make_user(USER_ID), "/start", update_id=1))
    async with session_factory() as session:
        user = await session.get(User, USER_ID)
        user.role = UserRole.ADMIN
        await session.commit()
    recording_session.calls.clear()

    await dispatcher.feed_update(bot, make_message(make_user(USER_ID), "/admin", update_id=2))

    assert recording_session.of_type("SendMessage")[0].text == ACCESS_DENIED


# ======================================================================================
# WayForPay — signatures and money
# ======================================================================================


def wfp() -> WayForPayClient:
    return WayForPayClient(
        merchant_account="acct",
        merchant_domain="example.com",
        secret_key="secret",  # noqa: S106
    )


def test_the_request_signature_matches_the_documented_field_order():
    """Computed here from WayForPay's documented order, independent of the implementation —
    reusing the code's own ordering would pass even if that ordering were wrong."""
    import hashlib
    import hmac

    fields = wfp().invoice_signature_fields(
        order_reference="o1",
        order_date=100,
        amount=Decimal("300"),
        currency="UAH",
        product_name="Sub",
        product_count=1,
    )
    assert fields == ["acct", "example.com", "o1", "100", "300.00", "UAH", "Sub", "1", "300.00"]
    expected = hmac.new(b"secret", ";".join(fields).encode(), hashlib.md5).hexdigest()
    from services.wayforpay import sign

    assert sign("secret", fields) == expected


def test_a_tampered_response_is_refused():
    """The amount is the one field an attacker would change."""
    import hashlib
    import hmac

    payload = {
        "merchantAccount": "acct",
        "orderReference": "o1",
        "amount": "300.00",
        "currency": "UAH",
        "authCode": "1",
        "cardPan": "44**",
        "transactionStatus": "Approved",
        "reasonCode": "1100",
    }
    payload["merchantSignature"] = hmac.new(
        b"secret",
        ";".join(str(payload.get(f, "")) for f in RESPONSE_SIGNATURE_FIELDS).encode(),
        hashlib.md5,
    ).hexdigest()

    wfp().verify_response(payload)  # the honest one passes
    payload["amount"] = "1.00"
    with pytest.raises(SignatureMismatch):
        wfp().verify_response(payload)

    # An unknown status maps to ERROR, which is non-terminal: the poller retries rather than
    # writing off a payment we merely failed to recognise.
    assert map_status("SomethingNew") is PaymentStatus.ERROR
    assert map_status("Approved") is PaymentStatus.COMPLETE
    assert map_status("InProcessing") is PaymentStatus.PENDING


# ======================================================================================
# Billing jobs
# ======================================================================================


@pytest.fixture
def config() -> BillingConfig:
    return BillingConfig(period_days=PERIOD_DAYS, grace=timedelta(days=3), channel_id=CHANNEL_ID)


class FakeGateway:
    def __init__(self):
        self.invoice_calls = []
        self.status_calls = []
        self.status_result = None

    async def create_invoice(self, **kw):
        from services.wayforpay import Invoice

        self.invoice_calls.append(kw)
        return Invoice(
            order_reference=kw["order_reference"],
            invoice_url=f"https://secure.wayforpay.com/x?{kw['order_reference']}",
            raw={},
        )

    async def check_status(self, *, order_reference):
        self.status_calls.append(order_reference)
        return self.status_result


def approved(order_ref, amount=REGULAR_PRICE, currency=CURRENCY):
    from services.wayforpay import TransactionStatus

    return TransactionStatus(
        order_reference=order_ref,
        status=PaymentStatus.COMPLETE,
        gateway_status="Approved",
        amount=amount,
        currency=currency,
        reason_code="1100",
        rec_token="tok",  # noqa: S106
        raw={"transactionStatus": "Approved"},
    )


async def seed_due(session_factory, *, price=REGULAR_PRICE, overdue_days=0):
    async with session_factory() as session:
        session.add(User(telegram_id=USER_ID, username="member"))
        session.add(User(telegram_id=ADMIN_ID, username="boss"))
        session.add(
            Subscription(
                user_id=USER_ID,
                status=SubscriptionStatus.PAST_DUE,
                price=price,
                currency=CURRENCY,
                price_tier=PriceTier.REGULAR,
                period_days=PERIOD_DAYS,
                started_at=NOW - timedelta(days=PERIOD_DAYS),
                expires_at=NOW - timedelta(days=overdue_days),
            )
        )
        await session.commit()


async def one_payment(session_factory):
    async with session_factory() as session:
        return await session.scalar(select(Payment))


async def test_a_confirmed_payment_extends_access_exactly_once(session_factory, bot, config):
    """A repeated Approved — a retry, or two polls racing — must not grant two periods."""
    await seed_due(session_factory)
    gw = FakeGateway()
    await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )
    gw.status_result = approved((await one_payment(session_factory)).order_reference)

    await poll_open_payments(session_factory, gw, bot, config=config, now=NOW)
    first = (await load_sub(session_factory)).expires_at
    await poll_open_payments(session_factory, gw, bot, config=config, now=NOW)

    sub = await load_sub(session_factory)
    assert first == NOW + timedelta(days=PERIOD_DAYS)
    assert sub.expires_at == first
    assert sub.status is SubscriptionStatus.ACTIVE
    assert sub.wayforpay_rec_token == "tok"


async def test_a_wrong_amount_is_refused(session_factory, bot, config):
    """Otherwise someone pays 1 € for a 10 € month."""
    await seed_due(session_factory)
    gw = FakeGateway()
    await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )
    gw.status_result = approved(
        (await one_payment(session_factory)).order_reference, amount=Decimal("1.00")
    )

    await poll_open_payments(session_factory, gw, bot, config=config, now=NOW)

    assert (await one_payment(session_factory)).status is PaymentStatus.ERROR
    assert (await load_sub(session_factory)).expires_at == NOW


async def test_a_discount_changes_the_invoiced_sum(session_factory, bot, config):
    await seed_due(session_factory, price=Decimal("10.00"))
    async with session_factory() as session:
        await discount_service.grant(
            session,
            actor_id=ADMIN_ID,
            telegram_id=USER_ID,
            kind=DiscountKind.FIXED_PRICE,
            fixed_price=Decimal("8.00"),
            currency=CURRENCY,
            days=None,
            now=NOW,
        )
        await session.commit()
    gw = FakeGateway()

    await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )

    assert gw.invoice_calls[0]["amount"] == Decimal("8.00")
    assert (await load_sub(session_factory)).price == Decimal("10.00")  # base untouched


async def test_the_due_job_is_idempotent(session_factory, bot, config):
    """It runs daily; a second run must not double-invoice or re-alert."""
    await seed_due(session_factory, overdue_days=10)
    gw = FakeGateway()

    first = await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )
    second = await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )

    assert (first["invoiced"], first["escalated"]) == (1, 1)
    assert (second["invoiced"], second["escalated"]) == (0, 0)
    assert len(gw.invoice_calls) == 1


async def test_an_overdue_member_is_reported_not_removed(
    session_factory, bot, recording_session, config
):
    """Removal is deliberately not enabled until the payment path is proven with real money."""
    await seed_due(session_factory, overdue_days=10)

    await process_due_subscriptions(
        session_factory,
        FakeGateway(),
        bot,
        admin_ids=frozenset({ADMIN_ID}),
        config=config,
        now=NOW,
    )

    assert recording_session.of_type("BanChatMember") == []
    to_admin = [c for c in recording_session.of_type("SendMessage") if c.chat_id == ADMIN_ID]
    assert to_admin and "@member" in to_admin[0].text
    async with session_factory() as session:
        row = await session.scalar(select(AuditLog).where(AuditLog.action == "payment.missing"))
    assert row is not None and row.target_user_id == USER_ID


async def test_a_paid_member_receives_a_single_use_invite(
    session_factory, bot, recording_session, config
):
    """A shared link would let one payment admit a crowd."""
    from aiogram.types import ChatInviteLink

    recording_session.responses["CreateChatInviteLink"] = ChatInviteLink.model_validate(
        {
            "invite_link": "https://t.me/+abc",
            "creator": {"id": 1, "is_bot": True, "first_name": "b"},
            "creates_join_request": False,
            "is_primary": False,
            "is_revoked": False,
        }
    )
    await seed_due(session_factory)
    gw = FakeGateway()
    await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )
    gw.status_result = approved((await one_payment(session_factory)).order_reference)

    await poll_open_payments(
        session_factory, gw, bot, config=config, admin_ids=frozenset({ADMIN_ID}), now=NOW
    )

    created = recording_session.of_type("CreateChatInviteLink")
    assert len(created) == 1
    assert created[0].member_limit == 1
    assert created[0].expire_date is not None
    assert any(
        "t.me/+abc" in c.text
        for c in recording_session.of_type("SendMessage")
        if c.chat_id == USER_ID
    )


async def test_a_paid_member_is_told_even_when_the_invite_fails(
    session_factory, bot, recording_session, config
):
    """The money is taken by then, so silence is the worst outcome."""
    await seed_due(session_factory)
    gw = FakeGateway()
    no_channel = BillingConfig(period_days=PERIOD_DAYS, channel_id=None)
    await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=no_channel, now=NOW
    )
    gw.status_result = approved((await one_payment(session_factory)).order_reference)

    await poll_open_payments(
        session_factory, gw, bot, config=no_channel, admin_ids=frozenset({ADMIN_ID}), now=NOW
    )

    to_member = [c.text for c in recording_session.of_type("SendMessage") if c.chat_id == USER_ID]
    to_admin = [c.text for c in recording_session.of_type("SendMessage") if c.chat_id == ADMIN_ID]
    assert any(texts.INVITE_UNAVAILABLE in t for t in to_member)
    assert any("CHANNEL_ID" in t for t in to_admin)


# ======================================================================================
# Admin screens
# ======================================================================================


async def test_granting_a_discount_end_to_end(dispatcher, bot, session_factory):
    """The whole FSM walk, as an admin drives it."""
    from handlers.admin import AdminMenu

    admin = make_user(ADMIN_ID)
    steps = [
        make_callback(admin, AdminMenu(action="grant_discount").pack(), update_id=1),
        make_message(admin, "@student_one", update_id=2),
        make_callback(admin, "grant:kind:fixed", update_id=3),
        make_message(admin, "8", update_id=4),
        make_callback(admin, "grant:days:90", update_id=5),
        make_callback(admin, "grant:note:skip", update_id=6),
        make_callback(admin, "grant:apply", update_id=7),
    ]
    for update in steps:
        await dispatcher.feed_update(bot, update)

    async with session_factory() as session:
        d = await session.scalar(select(Discount))
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "discount.granted"))
    assert d.username == "student_one"
    assert d.fixed_price == Decimal("8")
    assert d.valid_until is not None
    assert d.granted_by == ADMIN_ID
    assert audit is not None


async def test_the_discount_list_shows_live_rows_and_who_has_not_claimed(
    dispatcher, bot, session_factory, recording_session
):
    from handlers.admin import AdminMenu

    async with session_factory() as session:
        session.add(
            Discount(
                username="waiting",
                kind=DiscountKind.FIXED_PRICE,
                fixed_price=Decimal("8.00"),
                currency=CURRENCY,
                note="засновник клубу",
            )
        )
        session.add(
            Discount(
                username="lapsed",
                kind=DiscountKind.PERCENT,
                percent_off=50,
                valid_until=NOW - timedelta(days=1),
            )
        )
        await session.commit()

    await dispatcher.feed_update(
        bot, make_callback(make_user(ADMIN_ID), AdminMenu(action="discounts").pack())
    )

    body = recording_session.of_type("SendMessage")[0].text
    assert "@waiting" in body
    assert "@lapsed" not in body, "an expired discount is not active"
    assert "засновник клубу" in body
    assert "ще не активував бота" in body


async def test_the_participants_list_shows_members_and_says_what_it_cannot_show(
    dispatcher, bot, session_factory, recording_session
):
    """It lists people who started the bot. A bot cannot enumerate a channel's members, so the
    message has to say so or the count reads as if members were missing."""
    from handlers.admin import AdminMenu

    await dispatcher.feed_update(bot, make_message(make_user(USER_ID, username="m1"), "/start"))
    recording_session.calls.clear()

    await dispatcher.feed_update(
        bot, make_callback(make_user(ADMIN_ID), AdminMenu(action="users").pack())
    )

    body = recording_session.of_type("SendMessage")[0].text
    assert "@m1" in body
    assert texts.money(REGULAR_PRICE, CURRENCY) in body
    assert "Перелік учасників каналу бот отримати не може" in body


async def test_cancelling_a_subscription_keeps_the_paid_period(dispatcher, bot, session_factory):
    """SPEC: «Підписка діє до [дата], далі буде скасована»."""
    from db.models import utcnow

    started = utcnow()
    async with session_factory() as session:
        session.add(User(telegram_id=USER_ID, username="member"))
        session.add(
            Subscription(
                user_id=USER_ID,
                status=SubscriptionStatus.ACTIVE,
                price=REGULAR_PRICE,
                currency=CURRENCY,
                price_tier=PriceTier.REGULAR,
                period_days=PERIOD_DAYS,
                started_at=started,
                expires_at=started + timedelta(days=20),
                wayforpay_rec_token="tok",  # noqa: S106
            )
        )
        await session.commit()

    user = make_user(USER_ID)
    await dispatcher.feed_update(bot, make_callback(user, "sub:cancel", update_id=1))
    await dispatcher.feed_update(bot, make_callback(user, "sub:cancel_yes", update_id=2))

    sub = await load_sub(session_factory)
    assert sub.status is SubscriptionStatus.CANCELLED
    assert sub.expires_at == started + timedelta(days=20)
    assert sub.wayforpay_rec_token is None


# ======================================================================================
# Safety nets
# ======================================================================================


async def test_a_handler_crash_still_answers_the_user(
    dispatcher, bot, recording_session, monkeypatch
):
    """S8: no unhandled exception may reach a member as silence."""

    async def boom(*a, **k):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr("handlers.start.upsert_user", boom)

    await dispatcher.feed_update(bot, make_message(make_user(USER_ID), "/start"))

    texts_sent = [c.text for c in recording_session.of_type("SendMessage")]
    assert texts.GENERIC_ERROR in texts_sent
    assert not any("database is on fire" in t for t in texts_sent)


def test_every_member_facing_message_is_ukrainian_and_formats_cleanly():
    """One test over all the copy: unfilled placeholders and unbalanced HTML both make Telegram
    reject a send, and an untranslated string is visible to a member."""
    import re

    sample = {
        "club": texts.CLUB_NAME,
        "until": "31.12.2026",
        "amount": "10 €",
        "period": 30,
        "retry_in": "24 години",
        "since": "01.12.2026",
        "handle": "@m",
        "user_id": 1,
        "tier": "regular",
        "label": "X",
        "status": "активна",
        "link": "https://t.me/+a",
        "name": "Анна",
        "occupation": "X",
        "city": "Київ",
        "blog": "@a",
        "niche": "X",
        "looking_for": "X",
        "offers": "X",
        "note": "X",
        "who": "@m",
        "what": "20%",
        "claimed": "",
        "days": 90,
        "count": 3,
        "currency": "EUR",
        "base": "10 €",
        "new_price": "8 €",
        "title": "T",
        "chat_type": "channel",
        "chat_id": -100,
        "rights": "✅",
        "total": 5,
        "active": 2,
        "unpaid": 1,
        "trial": 2,
        "shown": 30,
    }
    placeholder = re.compile(r"\{(\w+)\}")
    checked = 0
    for attr in dir(texts):
        if attr.startswith("_"):
            continue
        value = getattr(texts, attr)
        if not isinstance(value, str) or attr in {"CLUB_NAME"}:
            continue
        checked += 1
        missing = set(placeholder.findall(value)) - sample.keys()
        assert not missing, f"{attr} needs {missing}"
        filled = value.format(**sample)
        assert "{" not in filled, attr
        for tag in ("b", "i", "code"):
            assert filled.count(f"<{tag}>") == filled.count(f"</{tag}>"), attr
        stripped = re.sub(r"&(amp|lt|gt|quot);", "", value)
        assert "&" not in stripped, f"{attr} has an unescaped & — Telegram rejects the send"
        assert len(value) <= 4096, attr
    assert checked > 40, "the sweep found suspiciously few strings"
