"""SQLAlchemy 2 ORM models.

Tables and the phase that introduces each (see the plan's data model section):

* Phase 1  — ``users``, ``audit_log``                                  <- implemented
* Phase 2A — ``subscriptions``, ``payments``, ``discounts``            <- implemented
* Phase 2  — ``categories``, ``materials`` (knowledge base, deferred)
* Phase 6 — ``profiles``
* Phase 7 — ``broadcasts``

All timestamp columns use ``UtcDateTime`` and store UTC (CLAUDE.md rule 1);
conversion to ``DISPLAY_TIMEZONE`` happens in handlers, never here. Any change to this
module ships with an Alembic migration that upgrades from the previous head (S7).
"""

import enum
import re
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-aware current time. The only clock this project reads."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator):
    """A timestamp that is always timezone-aware UTC in Python, on every backend.

    PostgreSQL's ``TIMESTAMP WITH TIME ZONE`` returns aware datetimes, but SQLite has no
    timezone support and hands back naive ones. Without this decorator the same row would
    compare fine against ``utcnow()`` in production and raise ``TypeError: can't compare
    offset-naive and offset-aware datetimes`` in local development — or, worse, silently
    differ. Phase 3's expiry arithmetic (G3.2) depends on the two behaving identically.

    On the way in: reject naive values, normalise anything else to UTC.
    On the way out: re-attach UTC when the driver dropped it.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            msg = (
                "Refusing to store a naive datetime. Use db.models.utcnow() or attach a "
                "timezone — see CLAUDE.md rule 1."
            )
            raise ValueError(msg)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base; Alembic autogenerate reads ``Base.metadata``."""


class UserRole(enum.StrEnum):
    USER = "user"
    ADMIN = "admin"


class User(Base):
    """A Telegram user who has interacted with the bot.

    ``role`` mirrors ``ADMIN_IDS`` for display and reporting. It is **not** the access
    check — see ``handlers.admin`` for why the environment stays authoritative.
    """

    __tablename__ = "users"

    # Telegram ids exceed 32 bits, so BigInteger. Not a surrogate key: no autoincrement.
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(32), default=None)
    first_name: Mapped[str | None] = mapped_column(String(64), default=None)
    role: Mapped[UserRole] = mapped_column(
        # native_enum=False keeps this a plain VARCHAR on both SQLite and PostgreSQL, so
        # adding a role later is an ordinary migration rather than a type alteration.
        # values_callable stores the enum *values* ("user"/"admin") rather than SQLAlchemy's
        # default of member names ("USER"/"ADMIN"), matching the plan's data model.
        # No DB-level CHECK: create_constraint=True makes alembic autogenerate emit the
        # constraint three times under one name, which PostgreSQL rejects. Valid values are
        # enforced in Python by validate_strings=True, and this column only mirrors
        # ADMIN_IDS — the access check never reads it.
        Enum(
            UserRole,
            native_enum=False,
            length=16,
            validate_strings=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=UserRole.USER,
    )
    is_blocked_bot: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    def __repr__(self) -> str:
        return f"<User {self.telegram_id} @{self.username} {self.role}>"


class AuditLog(Base):
    """One row per data-changing admin action (standing gate S6).

    Append-only: rows are never updated or deleted. ``actor_id`` and ``target_user_id`` are
    plain BigIntegers rather than cascading foreign keys so that an audit trail survives the
    removal of either party.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    target_user_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    details: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    # Audit questions are always "what happened to this user" or "what happened lately".
    __table_args__ = (Index("ix_audit_log_target_created", "target_user_id", "created_at"),)

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} by {self.actor_id} on {self.target_user_id}>"


class SubscriptionStatus(enum.StrEnum):
    """See docs/telegram-bot-payments-design.md section 4."""

    TRIAL = "trial"  # free first period, never paid
    ACTIVE = "active"  # paid, expires_at in the future
    PAST_DUE = "past_due"  # expires_at passed, inside grace, still in the community
    EXPIRED = "expired"  # grace ended; removal is deferred, so this means "owes money"
    CANCELLED = "cancelled"  # user stopped renewing; access until expires_at


class PriceTier(enum.StrEnum):
    """How this subscription's price was decided, kept for reporting and audit."""

    DISCOUNTED = "discounted"  # had a discount when they joined (list or admin-granted)
    COMMUNITY = "community"  # already in the community when they started the bot
    REGULAR = "regular"  # new joiner


class PaymentStatus(enum.StrEnum):
    """Modelled on SendPulse's set. Only COMPLETE grants access; REFUNDED and REVERSED revoke."""

    PENDING_PAYMENT = "pending_payment"  # invoice issued, user has not paid
    PENDING = "pending"  # paid, gateway still settling (WayForPay InProcessing)
    COMPLETE = "complete"  # Approved
    DENIED = "denied"  # Declined
    CANCELED = "canceled"  # timed out or abandoned
    REFUNDED = "refunded"
    REFUNDED_PARTIAL = "refunded_partial"
    REVERSED = "reversed"
    ERROR = "error"  # transport failure; retry the status check


def _enum_column(enum_cls: type[enum.StrEnum], length: int = 24) -> Enum:
    """Store enum *values* as VARCHAR on every backend. See the note on User.role."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


# Money is Numeric, never float: 0.1 + 0.2 must not drift in a column people are billed from.
MONEY = Numeric(12, 2)


class Subscription(Base):
    """One row per user. The club's schedule lives here, not at WayForPay.

    ``expires_at`` **is** the due date: every job is a comparison against it. ``price`` is stored
    per subscription because pricing is per user, so an invoice is always built from this row
    rather than from a global setting.
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id"), unique=True, index=True
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        _enum_column(SubscriptionStatus), default=SubscriptionStatus.TRIAL
    )
    price: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default="UAH")
    price_tier: Mapped[PriceTier] = mapped_column(_enum_column(PriceTier))
    period_days: Mapped[int] = mapped_column(default=30)

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    grace_until: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_reminder_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    admin_notified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    # Free period granted once, tracked explicitly so a second /start cannot extend it (A.6).
    free_period_granted: Mapped[bool] = mapped_column(default=False)

    # Stored as soon as WayForPay returns it, so renewals need no migration later.
    wayforpay_rec_token: Mapped[str | None] = mapped_column(String(255), default=None)

    source: Mapped[str] = mapped_column(String(16), default="wayforpay")
    granted_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)

    def __repr__(self) -> str:
        return f"<Subscription user={self.user_id} {self.status} expires={self.expires_at}>"


class Payment(Base):
    """One row per attempted transaction. Several rows per subscription over time.

    ``order_reference`` is UNIQUE and is the join key to WayForPay. That constraint is what makes
    a repeated ``Approved`` extend access exactly once (A.11).
    """

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), default=None, index=True
    )

    order_reference: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[PaymentStatus] = mapped_column(
        _enum_column(PaymentStatus), default=PaymentStatus.PENDING_PAYMENT
    )

    invoice_url: Mapped[str | None] = mapped_column(String(512), default=None)

    # Verbatim gateway fields, kept for reconciliation (A.15, plan G5.13).
    wayforpay_status: Mapped[str | None] = mapped_column(String(32), default=None)
    reason_code: Mapped[str | None] = mapped_column(String(16), default=None)
    raw_response: Mapped[dict | None] = mapped_column(JSON, default=None)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    settled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    def __repr__(self) -> str:
        return f"<Payment {self.order_reference} {self.status} {self.amount} {self.currency}>"


class DiscountKind(enum.StrEnum):
    """How a discount is expressed."""

    PERCENT = "percent"  # e.g. 20% off whatever the regular price is
    FIXED_PRICE = "fixed_price"  # e.g. exactly 8 EUR, whatever the regular price becomes


class Discount(Base):
    """A reduced price for one person, optionally for a limited time.

    Two ways in:

    * **By list** — ``username`` is set, ``user_id`` is not. Matched the first time that person
      starts the bot, then pinned to their numeric id. Usernames are mutable and the Bot API
      cannot resolve one to an id, so a name is only ever matched once.
    * **By admin** — ``user_id`` is set directly for someone already known.

    ``valid_until`` is what makes this more than a price override: a discount can expire. That is
    why the charged amount is computed at invoicing time by ``services.discounts.effective_price``
    rather than read from ``subscriptions.price`` — a snapshot would make every discount permanent.

    Revoking sets ``revoked_at`` rather than deleting, so the history behind a member's past
    invoices survives. At most one discount should be active per person; the service enforces that
    rather than the schema, because a person may legitimately have several over time.
    """

    __tablename__ = "discounts"

    id: Mapped[int] = mapped_column(primary_key=True)

    username: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, default=None, index=True)

    kind: Mapped[DiscountKind] = mapped_column(_enum_column(DiscountKind))
    #: Set when kind is PERCENT. 1-100.
    percent_off: Mapped[int | None] = mapped_column(default=None)
    #: Set when kind is FIXED_PRICE.
    fixed_price: Mapped[Decimal | None] = mapped_column(MONEY, default=None)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")

    #: None means no expiry.
    valid_until: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    #: Whether this entitles the holder to a free first period, independently of whether they are
    #: already in the channel. Used by the migration list: those members were promised both a
    #: price and a free month, and most of them are not detectable as channel members because a
    #: bot cannot enumerate a channel's membership.
    free_first_period: Mapped[bool] = mapped_column(default=False)

    note: Mapped[str | None] = mapped_column(String(255), default=None)
    granted_by: Mapped[int | None] = mapped_column(BigInteger, default=None)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    __table_args__ = (
        # "which discounts are live right now" is the query the admin list and every invoice runs.
        Index("ix_discounts_live", "revoked_at", "valid_until"),
    )

    def is_active(self, now: datetime) -> bool:
        """Whether this discount applies at ``now``."""
        if self.revoked_at is not None:
            return False
        return self.valid_until is None or self.valid_until > now

    @property
    def target(self) -> str:
        """How to name this discount's holder in a message."""
        if self.username:
            return f"@{self.username}"
        return f"id {self.user_id}"

    def __repr__(self) -> str:
        detail = (
            f"{self.percent_off}%"
            if self.kind is DiscountKind.PERCENT
            else f"{self.fixed_price} {self.currency}"
        )
        return f"<Discount {self.target} {detail} until={self.valid_until}>"


#: Telegram's rule is 5-32 characters of a-z, 0-9 and underscore, starting with a letter.
#: Four is allowed here because some legacy accounts predate the five-character minimum.
USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{3,31}$")


def looks_like_username(candidate: str | None) -> bool:
    """Whether this could be a real Telegram username.

    Used before storing one. ``normalise_username`` only tidies the string — it will happily
    return "!!!" — so without this check a typo becomes a discount that can never match anybody
    and sits in the list looking as though someone simply has not claimed it.
    """
    return candidate is not None and bool(USERNAME_PATTERN.match(candidate))


def normalise_username(username: str | None) -> str | None:
    """Canonical form for username matching: lowercase, no leading ``@``, no surrounding space.

    Telegram usernames are case-insensitive, so "@Makaolya" and "makaolya" are the same account.
    Matching on the raw string would silently miss list entries typed with different casing.
    """
    if username is None:
        return None
    cleaned = username.strip().lstrip("@").lower()
    return cleaned or None


# TODO(phase-2): Category, Material  (knowledge base, deferred)
# TODO(phase-6): Profile
# TODO(phase-7): Broadcast
