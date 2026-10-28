"""founding members keep their own price, and all run to 2026-11-01

Two corrections for the members onboarded from the beginning — the ones seeded by
`b45da0a2bffb` with their own 8 or 10 EUR price and a free first period.

**1. Their price becomes their own.** `subscriptions.price` held the club's list price, 10 EUR,
with the 8 EUR living only in their `discounts` row and applied per invoice. The amounts charged
were always right, but every screen that quotes `subscriptions.price` told an 8 EUR member they
pay 10 — and more to the point, 8 EUR *is* their tariff, not a temporary reduction of 10. Owner's
decision, 2026-10-22: make the subscription say so.

Consequence worth knowing: revoking one of these discounts now leaves the member on 8 rather
than returning them to 10, because 8 is the base price from here on.

**2. Their period ends 2026-11-01, not thirty days after they happened to join.** With
`FREE_PERIOD_UNTIL` unset in the deployed environment, `first_expiry` falls back to "one period
from today", so each of them got a different end date — a member who started on 21 October was
given 20 November. `e8c5a1f3d209` fixed the ones who had joined by then; anybody who has joined
since got the fallback again, because the environment variable is still the real cause and no
migration can fix the future. **Set `FREE_PERIOD_UNTIL=2026-11-01T00:00:00+02:00` as well.**

Not touched:

* a member with a `complete` payment keeps their dates — they bought a period, and moving the
  expiry back to November would take it away. Their price is still corrected.
* anybody with no `users` row, because there is no subscription to correct.

Data-only and idempotent. Run ONLINE (`alembic upgrade head`).

Revision ID: f1b6c0d84e27
Revises: e8c5a1f3d209
Create Date: 2026-10-22 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from datetime import UTC, datetime
from decimal import Decimal


# revision identifiers, used by Alembic.
revision: str = 'f1b6c0d84e27'
down_revision: Union[str, Sequence[str], None] = 'e8c5a1f3d209'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: End of the free period: 2026-11-01 00:00 in Kyiv is 2026-10-31 22:00 UTC, Ukraine being on
#: EET (UTC+2) by then. The same instant `e8c5a1f3d209` and `FREE_PERIOD_UNTIL` use, so a member
#: corrected here and one onboarded by `/start` get an identical date.
FREE_PERIOD_END = datetime(2026, 10, 31, 22, 0, tzinfo=UTC)


def _founding_members(connection) -> list[tuple[int, Decimal, str]]:
    """``(telegram_id, their own price, currency)`` for every founding member who has started.

    Read from the ``discounts`` table rather than from a second copy of the name list: a live
    ``fixed_price`` row *is* "this member has their own tariff", so the group cannot drift away
    from the seed. Matched by pinned ``user_id`` first, then by username for a row never claimed.
    """
    rows = connection.execute(
        sa.text(
            "SELECT user_id, username, fixed_price, currency FROM discounts "
            "WHERE fixed_price IS NOT NULL AND revoked_at IS NULL"
        )
    ).all()

    found: list[tuple[int, Decimal, str]] = []
    for user_id, username, fixed_price, currency in rows:
        if user_id is not None:
            row = connection.execute(
                sa.text("SELECT telegram_id FROM users WHERE telegram_id = :uid"),
                {"uid": user_id},
            ).first()
        elif username:
            row = connection.execute(
                sa.text(
                    "SELECT telegram_id FROM users "
                    "WHERE LOWER(COALESCE(username, '')) = :username"
                ),
                {"username": str(username).lower()},
            ).first()
        else:
            row = None
        if row is not None:
            found.append((int(row[0]), Decimal(str(fixed_price)), str(currency)))
    return found


def upgrade() -> None:
    connection = op.get_bind()
    now = datetime.now(UTC)

    members = _founding_members(connection)
    if not members:
        print("founding members: none of them have started the bot in this database")
        return

    repriced: list[int] = []
    redated: list[int] = []
    paid: list[int] = []

    for user_id, price, currency in members:
        has_subscription = connection.execute(
            sa.text("SELECT 1 FROM subscriptions WHERE user_id = :uid"),
            {"uid": user_id},
        ).first()
        if has_subscription is None:
            continue

        # Their own tariff, on the subscription itself. `price_tier` is set too so the admin list
        # and any reporting agree with the price rather than contradicting it.
        # `price` is bound with an explicit Numeric type. A bare Decimal through `sa.text()`
        # reaches SQLite's driver unconverted and is rejected outright — it works on PostgreSQL
        # and fails locally, which is the worst way round for something only CI would catch.
        connection.execute(
            sa.text(
                "UPDATE subscriptions SET price = :price, currency = :currency, "
                "  price_tier = 'discounted' "
                "WHERE user_id = :uid"
            # Numeric(12, 2) matches db.models.MONEY.
            ).bindparams(sa.bindparam("price", type_=sa.Numeric(12, 2))),
            {"price": price, "currency": currency, "uid": user_id},
        )
        repriced.append(user_id)

        already_paid = connection.execute(
            sa.text("SELECT 1 FROM payments WHERE user_id = :uid AND status = 'complete'"),
            {"uid": user_id},
        ).first()
        if already_paid is not None:
            # They bought a period; the dates are theirs. Only the price was corrected.
            paid.append(user_id)
            continue

        connection.execute(
            sa.text(
                "UPDATE subscriptions SET "
                "  status = 'trial', "
                "  expires_at = :expires_at, "
                "  free_period_granted = true, "
                "  grace_until = NULL, "
                "  last_reminder_at = NULL, "
                "  admin_notified_at = NULL "
                "WHERE user_id = :uid"
            ),
            {"expires_at": FREE_PERIOD_END, "uid": user_id},
        )

        # Nothing is owed until November, so no live payment link should be outstanding and the
        # poller has nothing left to ask about. Terminal rows are left alone.
        connection.execute(
            sa.text(
                "UPDATE payments SET status = 'canceled' "
                "WHERE user_id = :uid AND status IN ('pending_payment', 'pending', 'error')"
            ),
            {"uid": user_id},
        )
        redated.append(user_id)

    if repriced:
        op.bulk_insert(
            sa.table(
                "audit_log",
                sa.column("actor_id", sa.BigInteger),
                sa.column("action", sa.String),
                sa.column("target_user_id", sa.BigInteger),
                sa.column("details", sa.JSON),
                sa.column("created_at", sa.DateTime(timezone=True)),
            ),
            [
                {
                    # The grantee owns the record: the decision was taken outside the bot, and
                    # actor_id is a foreign key to users so it must name a row that exists.
                    "actor_id": user_id,
                    "action": "subscription.granted",
                    "target_user_id": user_id,
                    "details": {
                        "reason": "founding member: own tariff on the subscription",
                        "expires_at": (
                            FREE_PERIOD_END.isoformat() if user_id in redated else "unchanged"
                        ),
                        "migration": revision,
                    },
                    "created_at": now,
                }
                for user_id in repriced
            ],
        )

    print(f"founding members: {len(repriced)} repriced to their own tariff {repriced}")
    print(f"founding members: {len(redated)} now on trial until 2026-11-01 {redated}")
    if paid:
        print(f"founding members: {len(paid)} have paid — price fixed, dates left alone {paid}")
    print(
        "REMINDER: set FREE_PERIOD_UNTIL=2026-11-01T00:00:00+02:00 in the environment, or the "
        "next member to join before November gets a per-person date again."
    )


def downgrade() -> None:
    """Put the list price back and return the dates to an unpaid shape.

    The values this overwrote were not kept, so `price` returns to the club's regular tariff and
    the dates to `past_due` expiring at `started_at`, which is what an unpaid subscription looks
    like. The same members are resolved the way `upgrade` resolved them rather than by matching
    on the timestamp this wrote — comparing a bound datetime against a stored one matches on
    PostgreSQL and silently matches nothing on SQLite.
    """
    connection = op.get_bind()
    for user_id, _price, currency in _founding_members(connection):
        connection.execute(
            sa.text(
                "UPDATE subscriptions SET price = 10, currency = :currency "
                "WHERE user_id = :uid"
            ),
            {"currency": currency, "uid": user_id},
        )
        connection.execute(
            sa.text(
                "UPDATE subscriptions SET status = 'past_due', expires_at = started_at, "
                "  free_period_granted = false "
                "WHERE user_id = :uid AND status = 'trial'"
            ),
            {"uid": user_id},
        )
