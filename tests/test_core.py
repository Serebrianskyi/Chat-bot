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
    parse_amount,
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


async def test_an_expired_invoice_with_a_blank_amount_does_not_crash_the_poller(session_factory):
    """Production, 2026-10-06: WayForPay answered CHECK_STATUS with ``"amount": ""``.

    The body is signed with the blank in place, so it is genuine and must be read, not rejected.
    ``Decimal("")`` raised out of the client, out of the job's loop and past ``session.commit()``,
    so every two minutes the whole poll run died and no open payment was checked at all. A blank
    amount must come back as None, which ``apply_payment_result`` already refuses to grant on.
    """
    import hashlib
    import hmac

    import httpx

    body = {
        "merchantAccount": "acct",
        "orderReference": "o1",
        "amount": "",
        "currency": "",
        "transactionStatus": "Expired",
        "reasonCode": 1108,
    }
    body["merchantSignature"] = hmac.new(
        b"secret",
        ";".join(str(body.get(f, "") or "") for f in RESPONSE_SIGNATURE_FIELDS).encode(),
        hashlib.md5,
    ).hexdigest()

    client = WayForPayClient(
        merchant_account="acct",
        merchant_domain="example.com",
        secret_key="secret",  # noqa: S106
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)),
    )

    result = await client.check_status(order_reference="o1")

    assert result.amount is None  # not an exception, and not Decimal("0")
    assert result.status is PaymentStatus.CANCELED  # Expired is terminal: stop polling it
    # An unparseable amount is equally absent, rather than fatal.
    assert parse_amount("10 EUR", order_reference="o1", gateway_status="Approved") is None
    assert parse_amount("10.00", order_reference="o1", gateway_status="Approved") == Decimal("10")


async def test_one_broken_row_does_not_cost_everyone_else_their_payment(
    session_factory, bot, config
):
    """The real damage of the crash above: one unreadable row aborted the run for every other.

    A payment that raises must be stepped over — and a payment already granted must stay
    ``complete`` even if telling the member fails, because a downgraded row is polled again and
    ``apply_payment_result``'s idempotency guard only holds while it reads COMPLETE (A.11).
    """
    other_id = USER_ID + 1
    await seed_due(session_factory)
    async with session_factory() as session:
        session.add(User(telegram_id=other_id, username="second"))
        session.add(
            Subscription(
                user_id=other_id,
                status=SubscriptionStatus.PAST_DUE,
                price=REGULAR_PRICE,
                currency=CURRENCY,
                price_tier=PriceTier.REGULAR,
                period_days=PERIOD_DAYS,
                started_at=NOW - timedelta(days=PERIOD_DAYS),
                expires_at=NOW,
            )
        )
        await session.commit()

    gw = FakeGateway()
    await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )
    async with session_factory() as session:
        refs = {
            p.user_id: p.order_reference
            for p in (await session.execute(select(Payment))).scalars().all()
        }
    assert len(refs) == 2

    async def check_status(*, order_reference):
        if order_reference == refs[USER_ID]:
            raise ValueError("unparseable field")  # stands in for the Decimal("") crash
        return approved(order_reference)

    gw.check_status = check_status

    # Announcing the good payment fails too, which must not undo it.
    async def unreachable(*a, **k):
        raise RuntimeError("Telegram is down")

    bot.send_message = unreachable

    counts = await poll_open_payments(session_factory, gw, bot, config=config, now=NOW)

    async with session_factory() as session:
        paid = await session.scalar(
            select(Payment).where(Payment.order_reference == refs[other_id])
        )
        broken = await session.scalar(
            select(Payment).where(Payment.order_reference == refs[USER_ID])
        )
        good_sub = await session.scalar(
            select(Subscription).where(Subscription.user_id == other_id)
        )

    assert counts["completed"] == 1  # the run finished instead of dying on the first row
    assert paid.status is PaymentStatus.COMPLETE  # committed, not rolled back
    assert good_sub.expires_at == NOW + timedelta(days=PERIOD_DAYS)
    assert good_sub.status is SubscriptionStatus.ACTIVE
    assert broken.status is PaymentStatus.ERROR  # non-terminal, so it is retried
    assert broken.last_checked_at == NOW


async def test_a_declined_with_no_card_detail_is_not_written_off(session_factory, bot, config):
    """Production, 2026-10-09: 77 invoices one minute old all came back `Declined` with a blank
    amount. DENIED is terminal, so each left the poll query about a minute after being issued —
    and the member who then paid within the link's two-hour window was never asked about again.
    Money in, no invite. A refusal with no amount and no cardPan must stay pollable."""
    from services.wayforpay import TransactionStatus, refine_status

    assert (
        refine_status(PaymentStatus.DENIED, gateway_status="Declined", amount=None, card_pan="")
        is PaymentStatus.PENDING_PAYMENT
    )
    # A card that really was refused comes back with what it was refused for. Still denied.
    assert (
        refine_status(
            PaymentStatus.DENIED,
            gateway_status="Declined",
            amount=REGULAR_PRICE,
            card_pan="44**",
        )
        is PaymentStatus.DENIED
    )

    # End to end: the order stays open, so a payment made later is still confirmed.
    await seed_due(session_factory)
    gw = FakeGateway()
    await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )
    ref = (await one_payment(session_factory)).order_reference
    gw.status_result = TransactionStatus(
        order_reference=ref,
        status=refine_status(
            PaymentStatus.DENIED, gateway_status="Declined", amount=None, card_pan=""
        ),
        gateway_status="Declined",
        amount=None,
        currency=None,
        reason_code="1105",
        rec_token=None,
        raw={"transactionStatus": "Declined", "amount": ""},
    )

    await poll_open_payments(session_factory, gw, bot, config=config, now=NOW)
    assert (await one_payment(session_factory)).status is PaymentStatus.PENDING_PAYMENT

    # They pay an hour later, inside the invoice window. It must still be picked up.
    gw.status_result = approved(ref)
    await poll_open_payments(session_factory, gw, bot, config=config, now=NOW + timedelta(hours=1))
    sub = await load_sub(session_factory)
    assert (await one_payment(session_factory)).status is PaymentStatus.COMPLETE
    assert sub.status is SubscriptionStatus.ACTIVE


async def test_reconcile_credits_a_written_off_order_but_never_twice(session_factory, bot, config):
    """The recovery path for the 77 orders written off on 2026-10-09.

    Two things have to hold: an order that WayForPay says was paid gets credited and invited, and
    a member who already has access is left alone — re-crediting them would be a second period for
    one payment (A.11), which is exactly what the hand-written credits must be safe from.
    """
    from services.billing import reconcile_written_off

    await seed_due(session_factory)
    gw = FakeGateway()
    await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )
    # The invoice is written off the way the bug wrote them off: terminal, unpollable.
    async with session_factory() as session:
        payment = await session.scalar(select(Payment))
        payment.status = PaymentStatus.DENIED
        await session.commit()
        ref = payment.order_reference

    gw.status_result = approved(ref)

    # A dry run must not change anything, so it can be read before it is trusted.
    counts = await reconcile_written_off(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )
    assert counts["paid"] == 1
    assert (await one_payment(session_factory)).status is PaymentStatus.DENIED
    assert (await load_sub(session_factory)).expires_at == NOW

    await reconcile_written_off(
        session_factory,
        gw,
        bot,
        admin_ids=frozenset({ADMIN_ID}),
        config=config,
        now=NOW,
        apply=True,
    )
    first = (await load_sub(session_factory)).expires_at
    assert first == NOW + timedelta(days=PERIOD_DAYS)
    assert (await one_payment(session_factory)).status is PaymentStatus.COMPLETE

    # Now the hazard that the scope guard exists for: a member credited by hand, who therefore
    # has access but whose own invoice row is still sitting there written off. This is
    # @darriashine's exact shape. Crediting that row would buy a second period with one payment.
    credited_id = USER_ID + 11
    async with session_factory() as session:
        session.add(User(telegram_id=credited_id, username="handcredited"))
        sub = Subscription(
            user_id=credited_id,
            status=SubscriptionStatus.ACTIVE,
            price=REGULAR_PRICE,
            currency=CURRENCY,
            price_tier=PriceTier.REGULAR,
            period_days=PERIOD_DAYS,
            started_at=NOW,
            expires_at=NOW + timedelta(days=PERIOD_DAYS),  # credited by migration
        )
        session.add(sub)
        await session.flush()
        session.add(
            Payment(
                user_id=credited_id,
                subscription_id=sub.id,
                order_reference="sub-credited-1",
                amount=REGULAR_PRICE,
                currency=CURRENCY,
                status=PaymentStatus.CANCELED,  # cancelled by the credit migration
                created_at=NOW,
            )
        )
        await session.commit()

    gw.status_result = approved("sub-credited-1")
    second = await reconcile_written_off(
        session_factory,
        gw,
        bot,
        admin_ids=frozenset({ADMIN_ID}),
        config=config,
        now=NOW,
        apply=True,
    )

    assert second["checked"] == 0  # neither member is in scope: both already have access
    assert (await load_sub(session_factory)).expires_at == first
    credited = await load_sub(session_factory, credited_id)
    assert credited.expires_at == NOW + timedelta(days=PERIOD_DAYS)  # not extended a second time


async def test_invite_retry_tries_once_then_tells_an_admin_and_leaves_non_payers_alone(
    session_factory, bot, recording_session
):
    """Owner's instruction, 2026-10-09: one retry, then a person — and nobody who has not paid.

    A free trial is not a payment, so a TRIAL member must never be messaged by this job even
    though they hold access.
    """
    from services.billing import retry_missing_invites

    trial_id = USER_ID + 7
    async with session_factory() as session:
        session.add(User(telegram_id=USER_ID, username="paid", first_name="Дарія"))
        session.add(User(telegram_id=trial_id, username="freebie"))
        for uid, status in (
            (USER_ID, SubscriptionStatus.ACTIVE),
            (trial_id, SubscriptionStatus.TRIAL),
        ):
            session.add(
                Subscription(
                    user_id=uid,
                    status=status,
                    price=REGULAR_PRICE,
                    currency=CURRENCY,
                    price_tier=PriceTier.REGULAR,
                    period_days=PERIOD_DAYS,
                    started_at=NOW,
                    expires_at=NOW + timedelta(days=10),
                )
            )
        await session.commit()

    # Telegram says nobody is in the channel.
    args = {"channel_id": CHANNEL_ID, "admin_ids": frozenset({ADMIN_ID})}
    first = await retry_missing_invites(session_factory, bot, now=NOW, **args)

    assert first["checked"] == 1  # the trial member was never even looked at
    assert first["retried"] == 1
    sent_to = [c.chat_id for c in recording_session.of_type("SendMessage")]
    assert USER_ID in sent_to
    assert trial_id not in sent_to  # an unpaid member hears nothing

    # The next day, still not in the channel. One retry was the limit, so now a human is told.
    # A day later, not the same instant: a link sent moments ago is left to settle, or a member
    # would get two in a row from the poller and this job.
    tomorrow = NOW + timedelta(days=1)
    recording_session.calls.clear()
    second = await retry_missing_invites(session_factory, bot, now=tomorrow, **args)
    assert second["escalated"] == 1
    assert second["retried"] == 0
    to_admin = [c for c in recording_session.of_type("SendMessage") if c.chat_id == ADMIN_ID]
    assert len(to_admin) == 1
    # Everything needed to finish it by hand: handle, id, name, reason.
    assert "@paid" in to_admin[0].text
    assert str(USER_ID) in to_admin[0].text
    assert "Дарія" in to_admin[0].text
    assert texts.INVITE_REASON_NOT_USED in to_admin[0].text
    assert USER_ID not in [c.chat_id for c in recording_session.of_type("SendMessage")][1:]

    # And it stops: an escalated member is not touched again.
    recording_session.calls.clear()
    third = await retry_missing_invites(session_factory, bot, now=NOW + timedelta(days=2), **args)
    assert third["skipped"] == 1 and third["escalated"] == 0
    assert recording_session.of_type("SendMessage") == []

    # A link sent moments ago is never followed by a second one: at startup the reconciliation
    # confirms a payment and sends a link, and this job runs straight afterwards.
    async with session_factory() as session:
        fresh = await session.scalar(select(Subscription).where(Subscription.user_id == USER_ID))
        fresh.status = SubscriptionStatus.ACTIVE
        await session.execute(
            AuditLog.__table__.delete().where(AuditLog.action == "invite.escalated")
        )
        await session.commit()
    recording_session.calls.clear()
    immediate = await retry_missing_invites(
        session_factory, bot, now=NOW + timedelta(minutes=5), **args
    )
    assert immediate["too_soon"] == 1
    assert recording_session.of_type("SendMessage") == []


async def test_a_payer_whose_period_lapsed_before_anyone_noticed_still_gets_their_link(
    session_factory, bot, recording_session
):
    """The one way a confirmed payer could otherwise be lost for good.

    They paid, the invite failed, and their month ran out before anybody looked — at which point
    the "holds paid access" filter stops seeing them and no job would ever mention them again.
    They paid for something that was never delivered, so a link is owed regardless of expiry.
    """
    from services.billing import retry_missing_invites

    async with session_factory() as session:
        session.add(User(telegram_id=USER_ID, username="lapsed", first_name="Оля"))
        sub = Subscription(
            user_id=USER_ID,
            status=SubscriptionStatus.EXPIRED,
            price=REGULAR_PRICE,
            currency=CURRENCY,
            price_tier=PriceTier.REGULAR,
            period_days=PERIOD_DAYS,
            started_at=NOW - timedelta(days=60),
            expires_at=NOW - timedelta(days=30),  # lapsed a month ago
        )
        session.add(sub)
        await session.flush()
        session.add(
            Payment(
                user_id=USER_ID,
                subscription_id=sub.id,
                order_reference="sub-lapsed-1",
                amount=REGULAR_PRICE,
                currency=CURRENCY,
                status=PaymentStatus.COMPLETE,  # the money was confirmed
                created_at=NOW - timedelta(days=60),
            )
        )
        await session.commit()

    counts = await retry_missing_invites(
        session_factory,
        bot,
        channel_id=CHANNEL_ID,
        admin_ids=frozenset({ADMIN_ID}),
        now=NOW,
    )

    assert counts["retried"] == 1
    to_member = [c for c in recording_session.of_type("SendMessage") if c.chat_id == USER_ID]
    assert len(to_member) == 1
    assert "https://t.me/+default" in to_member[0].text


async def test_the_daily_sweep_reports_once_and_will_not_repeat_on_the_next_deploy(
    session_factory, bot, config, recording_session
):
    """The sweep acts on money and access with nobody watching, so it has to say what it did —
    and say it once.

    It used to run at startup, so ten deployments in an afternoon meant ten reports about the
    same people. It is daily now, and refuses to repeat inside RECOVERY_MIN_INTERVAL whatever
    calls it.
    """
    from services.billing import RECOVERY_MIN_INTERVAL, recover_access

    await seed_due(session_factory)
    gw = FakeGateway()
    await process_due_subscriptions(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )
    async with session_factory() as session:
        payment = await session.scalar(select(Payment))
        payment.status = PaymentStatus.DENIED  # written off the way the bug wrote them off
        await session.commit()
        ref = payment.order_reference
    gw.status_result = approved(ref)

    recording_session.calls.clear()
    await recover_access(
        session_factory, gw, bot, admin_ids=frozenset({ADMIN_ID}), config=config, now=NOW
    )

    to_admin = [c for c in recording_session.of_type("SendMessage") if c.chat_id == ADMIN_ID]
    assert len(to_admin) == 1  # one report for the run, not one message per member
    report = to_admin[0].text
    assert "@member" in report and str(USER_ID) in report
    assert "Знайдено оплату" in report  # the payment was recovered
    assert "Надіслано посилання" in report  # and the link went out

    # The credit is on the audit record too: nothing else logs a payment confirmed by a job.
    async with session_factory() as session:
        entry = await session.scalar(select(AuditLog).where(AuditLog.action == "payment.complete"))
    assert entry.details["recovered"] is True
    assert entry.details["order_reference"] == ref

    # A redeploy minutes later must change nothing and tell nobody.
    recording_session.calls.clear()
    again = await recover_access(
        session_factory,
        gw,
        bot,
        admin_ids=frozenset({ADMIN_ID}),
        config=config,
        now=NOW + timedelta(minutes=5),
    )
    assert "skipped" in again
    assert recording_session.of_type("SendMessage") == []

    # A day later it runs again, as a daily job should.
    tomorrow = await recover_access(
        session_factory,
        gw,
        bot,
        admin_ids=frozenset({ADMIN_ID}),
        config=config,
        now=NOW + RECOVERY_MIN_INTERVAL + timedelta(minutes=1),
    )
    assert "skipped" not in tomorrow


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


async def test_the_participants_screen_groups_members_and_stays_one_message(
    dispatcher, bot, session_factory, recording_session
):
    """It opens on counts with a button per group: a flat roster was already several messages at
    85 members and only grows. Tapping a group sends that group alone.

    A bot cannot enumerate a channel's members, so the screen still has to say so or the count
    reads as if members were missing.
    """
    from handlers.admin import AdminMenu

    # One member who owes payment (a regular joiner is due immediately)...
    await dispatcher.feed_update(bot, make_message(make_user(USER_ID, username="m1"), "/start"))
    # ...and one who is paid up and renewing.
    paying_id = USER_ID + 31
    from db.models import utcnow

    async with session_factory() as session:
        session.add(User(telegram_id=paying_id, username="m2"))
        session.add(
            Subscription(
                user_id=paying_id,
                status=SubscriptionStatus.ACTIVE,
                price=REGULAR_PRICE,
                currency=CURRENCY,
                price_tier=PriceTier.REGULAR,
                period_days=PERIOD_DAYS,
                started_at=utcnow(),
                expires_at=utcnow() + timedelta(days=20),
            )
        )
        await session.commit()

    recording_session.calls.clear()
    await dispatcher.feed_update(
        bot, make_callback(make_user(ADMIN_ID), AdminMenu(action="users").pack())
    )

    messages = recording_session.of_type("SendMessage")
    summary = messages[0].text
    assert "Учасники бота</b> — 2" in summary
    # No roster in the summary: that is what keeps it one message as the club grows.
    assert "@m1" not in summary and "@m2" not in summary
    buttons = [button.text for row in messages[0].reply_markup.inline_keyboard for button in row]
    assert f"{texts.ADMIN_GROUP_AUTO} — 1" in buttons
    assert f"{texts.ADMIN_GROUP_UNPAID} — 1" in buttons
    # Empty groups get no button at all.
    assert not any(texts.ADMIN_GROUP_LIFETIME in b for b in buttons)
    assert "Перелік учасників каналу бот отримати не може" in "\n".join(c.text for c in messages)

    # Tapping one group sends that group, and only that group.
    recording_session.calls.clear()
    await dispatcher.feed_update(
        bot, make_callback(make_user(ADMIN_ID), "admin:users_auto", update_id=2)
    )
    body = "\n".join(c.text for c in recording_session.of_type("SendMessage"))
    assert "@m2" in body
    assert "@m1" not in body
    assert texts.money(REGULAR_PRICE, CURRENCY) in body


async def test_a_broadcast_to_unpaid_members_follows_the_text_with_a_pay_button(
    dispatcher, bot, session_factory, recording_session, fake_wayforpay
):
    """Written for the people who started the bot and never paid. The admin's text alone is an
    advert with no way to act on it, so each recipient also gets their own payment link.

    Nothing may be sent before the admin has confirmed: a broadcast cannot be recalled.
    """
    from handlers.admin import AdminMenu

    await dispatcher.feed_update(bot, make_message(make_user(USER_ID, username="owes"), "/start"))
    admin = make_user(ADMIN_ID, username="boss")

    # Pick the audience, write the text — and check that the preview alone sends nothing.
    await dispatcher.feed_update(
        bot, make_callback(admin, AdminMenu(action="broadcast").pack(), update_id=2)
    )
    await dispatcher.feed_update(bot, make_callback(admin, "bcast:unpaid", update_id=3))
    await dispatcher.feed_update(
        bot, make_message(admin, "Ціну знижено до 8 € — повертайтесь!", update_id=4)
    )
    # The price the link will charge is chosen before the preview: a win-back message almost
    # always carries an offer, and the preview has to show the one that will actually be used.
    await dispatcher.feed_update(bot, make_callback(admin, "bprice:special", update_id=5))
    await dispatcher.feed_update(bot, make_message(admin, "8", update_id=6))
    recording_session.calls.clear()
    await dispatcher.feed_update(bot, make_callback(admin, "bperiod:3", update_id=7))

    preview = recording_session.of_type("SendMessage")
    assert all(c.chat_id == ADMIN_ID for c in preview)  # the member has heard nothing yet
    assert any("Ціну знижено до 8 €" in c.text for c in preview)  # shown exactly as it arrives
    # The second message is previewed for real — its own message, straight after the main one,
    # with the button in place. Describing it in words left the admin guessing at the thing most
    # likely to decide whether anybody pays.
    sample = [c for c in preview if c.reply_markup is not None]
    assert any(
        row[0].text == texts.JOIN_CLUB_BUTTON
        for c in sample
        for row in c.reply_markup.inline_keyboard
    )
    # It carries no URL: the real link is per member, and a plausible dead link would be worse.
    pay_sample = next(
        c for c in sample if c.reply_markup.inline_keyboard[0][0].text == texts.JOIN_CLUB_BUTTON
    )
    assert pay_sample.reply_markup.inline_keyboard[0][0].url is None
    assert pay_sample.reply_markup.inline_keyboard[0][0].callback_data == "bcast:sample"
    # Months can also be typed, for a duration that is not on a button. Bounded at both ends:
    # 0 is not "no limit" (that has its own button) and a typo must not price a century.
    from handlers.broadcast import MAX_MONTHS, parse_months

    assert parse_months("4") == 4
    assert parse_months(" 12 ") == 12
    assert parse_months("0") is None
    assert parse_months(str(MAX_MONTHS + 1)) is None
    assert parse_months("3 місяці") is None

    # The offer is spelled out: the sum in the currency the club charges, and the duration in
    # months, agreed properly — «3 місяці», not «3 місяць».
    offered = texts.money(Decimal("8"), CURRENCY)
    assert any(offered in c.text and texts.months_phrase(3) in c.text for c in preview)

    # Confirm.
    recording_session.calls.clear()
    await dispatcher.feed_update(bot, make_callback(admin, "bcast:send", update_id=8))

    to_member = [c for c in recording_session.of_type("SendMessage") if c.chat_id == USER_ID]
    assert len(to_member) == 2
    assert "Ціну знижено до 8 €" in to_member[0].text
    # The second message is the way back to paying, on a live invoice.
    button = to_member[1].reply_markup.inline_keyboard[0][0]
    assert button.text == texts.JOIN_CLUB_BUTTON
    assert button.url  # a real invoice URL from the gateway
    # The offer is what was actually invoiced, not just what the message claimed.
    assert fake_wayforpay.invoice_calls[-1]["amount"] == Decimal("8.00")

    async with session_factory() as session:
        entry = await session.scalar(select(AuditLog).where(AuditLog.action == "broadcast.sent"))
        discount = await session.scalar(select(Discount).where(Discount.user_id == USER_ID))
    assert entry.actor_id == ADMIN_ID
    assert entry.details["audience"] == "unpaid"
    assert "Ціну знижено" in entry.details["text"]
    assert entry.details["offer"]["fixed_price"] == "8"
    # The price is a real discount, so it shows up in 🎟 Знижки and governs their renewal too.
    assert discount.fixed_price == Decimal("8")
    # Three months means three billing periods, so the price covers whole periods rather than
    # lapsing part-way through one.
    assert discount.valid_until is not None
    assert (discount.valid_until - discount.created_at).days == 3 * PERIOD_DAYS


async def test_the_lifetime_group_walks_the_same_broadcast_flow_as_unpaid_members(
    dispatcher, bot, session_factory, recording_session, fake_wayforpay
):
    """♾ Безстрокові is the owner's own account, so it is how the campaign gets rehearsed.

    The rehearsal is only worth anything if it is the *same* path: the price step, a real
    invoice and a real «Стати частиною клубу!» button. A test target that quietly skipped any
    of those would prove nothing about the send that reaches fifty-three members.
    """
    from db.models import utcnow
    from handlers.admin import AdminMenu
    from handlers.broadcast import AUDIENCES_WITH_PAY_LINK

    assert "lifetime" in AUDIENCES_WITH_PAY_LINK

    async with session_factory() as session:
        session.add(User(telegram_id=ADMIN_ID, username="boss"))
        session.add(
            Subscription(
                user_id=ADMIN_ID,
                status=SubscriptionStatus.ACTIVE,
                price=REGULAR_PRICE,
                currency=CURRENCY,
                price_tier=PriceTier.REGULAR,
                period_days=PERIOD_DAYS,
                started_at=utcnow(),
                expires_at=datetime(2100, 1, 1, tzinfo=UTC),
                source="lifetime",  # what the grant migration marks it with
            )
        )
        await session.commit()

    admin = make_user(ADMIN_ID, username="boss")
    await dispatcher.feed_update(bot, make_callback(admin, AdminMenu(action="broadcast").pack()))
    await dispatcher.feed_update(bot, make_callback(admin, "bcast:lifetime", update_id=2))
    await dispatcher.feed_update(bot, make_message(admin, "Тестова розсилка", update_id=3))

    # The price step is offered, exactly as it is for ⏳ Очікують оплати.
    asked_price = recording_session.of_type("SendMessage")[-1]
    assert texts.ADMIN_BROADCAST_PRICE_SPECIAL in [
        button.text for row in asked_price.reply_markup.inline_keyboard for button in row
    ]

    await dispatcher.feed_update(bot, make_callback(admin, "bprice:special", update_id=4))
    await dispatcher.feed_update(bot, make_message(admin, "8", update_id=5))
    await dispatcher.feed_update(bot, make_callback(admin, "bperiod:1", update_id=6))
    recording_session.calls.clear()
    await dispatcher.feed_update(bot, make_callback(admin, "bcast:send", update_id=7))

    to_self = [c for c in recording_session.of_type("SendMessage") if c.chat_id == ADMIN_ID]
    # The text, the pay message, and the "done" report all land in the admin's own chat.
    assert any("Тестова розсилка" == c.text for c in to_self)
    with_button = [c for c in to_self if c.reply_markup is not None]
    assert len(with_button) == 1
    assert with_button[0].reply_markup.inline_keyboard[0][0].text == texts.JOIN_CLUB_BUTTON
    assert with_button[0].reply_markup.inline_keyboard[0][0].url
    assert fake_wayforpay.invoice_calls[-1]["amount"] == Decimal("8.00")


async def test_an_admin_can_post_into_the_channel_only_after_confirming(
    dispatcher, bot, recording_session, settings
):
    """The other direction of the conversation. Nothing reaches the channel from the typing step."""
    from handlers.admin import AdminMenu

    admin = make_user(ADMIN_ID, username="boss")
    await dispatcher.feed_update(bot, make_callback(admin, AdminMenu(action="channel_post").pack()))
    recording_session.calls.clear()
    await dispatcher.feed_update(bot, make_message(admin, "Зустріч у четвер о 19:00", update_id=2))

    # Preview only: the channel has had nothing.
    assert all(c.chat_id == ADMIN_ID for c in recording_session.of_type("SendMessage"))

    recording_session.calls.clear()
    await dispatcher.feed_update(bot, make_callback(admin, "post:send", update_id=3))

    to_channel = [c for c in recording_session.of_type("SendMessage") if c.chat_id == CHANNEL_ID]
    assert len(to_channel) == 1
    assert to_channel[0].text == "Зустріч у четвер о 19:00"


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
        "next_until": "30.01.2027",
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
        "reason": "посилання надіслано, але учасник не приєднався",
        "error": "TelegramBadRequest",
        "preview": "Привіт! Ось ваше посилання.",
        "text": "Привіт!",
        "step": "перевірка оплат",
        "extra": " — не доставлено",
        "button": "Долучитися до Клубу",
        "price": "8 €",
        "validity": "діє 3 місяці",
        "months": "3 місяці",
        "max_months": 60,
        "sent": 50,
        "blocked": 2,
        "invoiced": 48,
        "failed": 0,
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
