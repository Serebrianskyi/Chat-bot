"""Application settings, read from the environment and validated at startup.

Standing gate S5: the app must refuse to start when a required variable is missing,
with a message naming the variable. Secrets live only in the environment — never in
code, never in logs.
"""

from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_admin_ids(value: str) -> frozenset[int]:
    """Split a comma-separated id list into integers, ignoring blanks and stray whitespace."""
    return frozenset(int(part) for part in value.split(",") if part.strip())


class Settings(BaseSettings):
    """Required and optional configuration.

    Fields without a default are required; a missing one aborts startup.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Required from Phase 0 ---
    # min_length=1 makes a blank value count as missing. Without it, the `BOT_TOKEN=` line that
    # `cp .env.example .env` leaves behind validates as an empty string: startup would succeed,
    # ADMIN_IDS would parse to an empty set, and `/admin` would silently refuse everybody with
    # no hint as to why. That is precisely the failure S5 exists to prevent.
    # SecretStr, not str: pydantic's repr would otherwise print the value in full. A single
    # log.debug("%s", settings), or Sentry capturing frame locals in Phase 4, would publish it.
    # Read it with .get_secret_value() at the one place it is needed.
    bot_token: SecretStr = Field(
        min_length=1, description="@BotFather token. Test bot locally, never production."
    )
    admin_ids: str = Field(
        min_length=1, description="Comma-separated Telegram user ids with admin access."
    )
    # Optional until Phase 3, which is the first code that touches the channel (invite links,
    # ban/unban). Phase 1 never reads it, so refusing to start without it would block the
    # smoke test for features that do not need a channel. The plan requires recording it in
    # Phase 0 (G0.6) — that stays open — but it does not require the app to refuse to boot.
    # TODO(phase-3): make this required again once subscriptions need the channel.
    channel_id: str | None = Field(
        default=None, description="Private channel id the bot administrates. Needed from Phase 3."
    )

    # --- Database: SQLite locally, PostgreSQL from Phase 4 ---
    database_url: str = "sqlite+aiosqlite:///./chatbot.db"

    # --- Phase 4: webhook mode and protected job endpoints ---
    mode: Literal["polling", "webhook"] = "polling"
    base_url: str | None = None
    webhook_secret: str | None = None
    jobs_secret: str | None = None

    # --- Phase 4: monitoring ---
    sentry_dsn: str | None = None
    log_level: str = "INFO"

    # --- Phase 2A: subscription pricing ---
    # The regular monthly price for a new joiner. Per-user prices live in the price_rules table,
    # not here, because pricing is per user.
    subscription_price: Decimal = Decimal("0")
    subscription_currency: str = "UAH"
    subscription_period_days: int = 30
    #: Days after the due date before an admin is alerted.
    subscription_grace_days: int = 3

    # --- Phase 2A: WayForPay ---
    wayforpay_merchant_account: str | None = None
    wayforpay_merchant_domain: str | None = None
    #: SecretStr for the same reason as bot_token. This one signs money.
    wayforpay_secret_key: SecretStr | None = None

    # After this moment, being in the community no longer buys a free first period. Leave unset
    # while migrating; set it once the community is gated, or anyone who joins the free chat can
    # claim a free month. ISO-8601, UTC assumed if no offset is given.
    legacy_offer_deadline: datetime | None = None

    # Display time zone. Storage is always UTC — see CLAUDE.md rule 1.
    display_timezone: str = "Europe/Kyiv"

    @field_validator(
        "subscription_price",
        "subscription_currency",
        "subscription_period_days",
        "subscription_grace_days",
        "database_url",
        "log_level",
        "display_timezone",
        "mode",
        mode="before",
    )
    @classmethod
    def _blank_means_default(cls, value: object, info: ValidationInfo) -> object:
        """Treat a blank value in .env as "not set", so the field default applies.

        `SUBSCRIPTION_PRICE=` otherwise fails Decimal parsing and the bot refuses to start —
        which would make it impossible to run onboarding while the merchant account is still
        being set up. The scheduler separately refuses to bill anyone at a price of 0, so a
        blank price is safe rather than silently free.

        The default is looked up by field name rather than returned as PydanticUndefined,
        because a validator returning that sentinel passes it straight through as a value.
        """
        if isinstance(value, str) and not value.strip() and info.field_name:
            return cls.model_fields[info.field_name].default
        return value

    @field_validator("base_url", "webhook_secret", "jobs_secret", "sentry_dsn", mode="before")
    @classmethod
    def _blank_optional_is_none(cls, value: object) -> object:
        """Treat `BASE_URL=` in .env as unset rather than as the empty string.

        The template ships these blank, and pydantic would otherwise store `""` — which is
        truthy enough to pass an `if settings.base_url:` check in Phase 4 and then register a
        webhook against a nonsense URL.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("channel_id", mode="before")
    @classmethod
    def _channel_id_looks_like_a_chat_id(cls, value: object) -> object:
        """Catch a copy-paste accident before it becomes a Phase 3 failure.

        A channel id is a negative integer (-1001234567890) or an @username. Anything else —
        a stray comment, a channel *name*, a URL — would otherwise sit unnoticed until the
        first `create_chat_invite_link` call failed.

        Blank means "not set yet", which is allowed until Phase 3.
        """
        if not isinstance(value, str) or not value.strip():
            return None
        candidate = value.strip()
        if candidate.startswith("@") and len(candidate) > 1:
            return candidate
        try:
            int(candidate)
        except ValueError:
            msg = (
                f"CHANNEL_ID must be a numeric chat id like -1001234567890, or an @username; "
                f"got {value!r}"
            )
            raise ValueError(msg) from None
        return candidate

    @field_validator("database_url", mode="after")
    @classmethod
    def _normalise_database_url(cls, value: str) -> str:
        """Pin an explicit async driver on a PostgreSQL URL.

        A hosting provider hands over ``DATABASE_URL`` in libpq form. Railway uses
        ``postgresql://``, which SQLAlchemy 2.1 happens to resolve to async psycopg3 — but that is
        a default, not a promise. Others still emit the older ``postgres://``, which raises
        ``NoSuchModuleError`` outright. Naming the driver removes both risks, and leaves an
        already-explicit URL untouched.
        """
        if value.startswith("postgres://"):
            return "postgresql+psycopg://" + value[len("postgres://") :]
        if value.startswith("postgresql://"):
            return "postgresql+psycopg://" + value[len("postgresql://") :]
        return value

    @field_validator("legacy_offer_deadline")
    @classmethod
    def _deadline_must_be_aware(cls, value: datetime | None) -> datetime | None:
        """A naive deadline has no defined instant, so assume UTC (CLAUDE.md rule 1)."""
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @property
    def wayforpay_configured(self) -> bool:
        """Whether invoices can be created at all.

        Checked before the billing jobs run, so a half-configured install fails with one clear
        log line instead of an exception per subscriber.
        """
        secret = self.wayforpay_secret_key
        return all(
            (
                self.wayforpay_merchant_account,
                self.wayforpay_merchant_domain,
                # A SecretStr wrapping "" is still truthy, so check the value itself.
                secret is not None and secret.get_secret_value().strip(),
            )
        )

    @field_validator("admin_ids")
    @classmethod
    def _admin_ids_must_parse(cls, value: str) -> str:
        """Reject a malformed ADMIN_IDS at startup rather than at the first `/admin`.

        Parsing lazily in the property below would turn a typo into a ValueError raised deep
        inside a handler, which the error handler would then report to the user as a generic
        apology. Better to refuse to start.
        """
        try:
            parsed = _parse_admin_ids(value)
        except ValueError as exc:
            msg = f"ADMIN_IDS must be comma-separated Telegram user ids, got {value!r}"
            raise ValueError(msg) from exc
        if not parsed:
            msg = "ADMIN_IDS is empty: nobody would be able to open the admin panel"
            raise ValueError(msg)
        return value

    @property
    def admin_id_set(self) -> frozenset[int]:
        """`admin_ids` parsed into integers.

        Passed into the dispatcher as context and read by the ``IsAdmin`` filter. This is
        the only authority on who is an admin — see ``handlers.admin``.
        """
        return _parse_admin_ids(self.admin_ids)


class ConfigError(RuntimeError):
    """Raised when the environment is not usable, with a message naming what is wrong."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings, failing fast with a readable message (S5).

    The message names every offending variable *and* why, because the common case is a
    half-filled `.env` and "BOT_TOKEN is wrong" is not actionable on its own.
    """
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            name = str(err["loc"][0]).upper() if err["loc"] else "(unknown)"
            reason = err["msg"]
            if err["type"] == "missing":
                reason = "not set"
            elif err["type"] in {"string_too_short", "too_short"}:
                # SecretStr reports "too_short"; a plain str reports "string_too_short".
                reason = "is empty"
            problems.append(f"  {name}: {reason}")
        listed = "\n".join(problems)
        raise ConfigError(
            f"Cannot start: {len(problems)} environment variable(s) missing or invalid.\n"
            f"{listed}\n"
            f"Copy .env.example to .env and fill them in "
            f"(BOT_TOKEN comes from @BotFather; use the TEST bot)."
        ) from exc
