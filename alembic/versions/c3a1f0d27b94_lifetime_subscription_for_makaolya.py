"""lifetime subscription for makaolya

@makaolya (158032815) is one of the two admins. An admin does not pay the club, so the ordinary
schedule was wrong for them twice over: the daily job kept invoicing them, an unpaid invoice kept
raising an admin alert about themselves, and `/subscription` kept announcing a due date a month
out. This gives that one account a subscription with no end.

"No end" is a date far enough out to never arrive (2100-01-01), not a NULL. Every job and every
screen is a comparison against `expires_at`:

    process_due_subscriptions  WHERE expires_at <= now
    handlers/subscription.py   if subscription.expires_at <= now
    handlers/participants.py   texts.day(subscription.expires_at.date())

Making the column nullable would mean teaching each of those about a second meaning of "no date",
on the payment path, for one row. A sentinel satisfies all of them as written: the member reads
"активна", nothing comes due, and no code changes. The cost is that `/subscription` quotes
01.01.2100 as the valid-until date — visible only to that one admin.

Open invoices are closed out too, otherwise the poller would keep asking WayForPay about orders
this account is no longer expected to pay. Only `pending_payment` and `error` are closed:
`pending` means money is in flight at the gateway, and writing that off would hide a real
settlement.

Data-only and idempotent. On a database where the account has never run `/start` — a fresh CI
database — it does nothing, since there is no row to grant against. Note the consequence: after a
rebuild from empty, the first `/start` would create an ordinary monthly subscription and this
grant would need re-applying.

Run this ONLINE (`alembic upgrade head`): it reads existing rows to decide what to write, which
offline `--sql` mode cannot do.

Revision ID: c3a1f0d27b94
Revises: b45da0a2bffb
Create Date: 2026-10-06 12:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from datetime import UTC, datetime
from decimal import Decimal


# revision identifiers, used by Alembic.
revision: str = 'c3a1f0d27b94'
down_revision: Union[str, Sequence[str], None] = 'b45da0a2bffb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Keyed on the numeric id, which Telegram never changes. The username is carried only as a
#: second condition, so a migration cannot silently grant a lifetime to whoever holds the
#: handle today if the account were ever renamed.
TELEGRAM_ID = 158032815
USERNAME = "makaolya"

#: The end that never comes. Stored UTC, like every other timestamp (standing gate S1).
LIFETIME_EXPIRES_AT = datetime(2100, 1, 1, tzinfo=UTC)

#: What `source` records, so this row is recognisable as a grant rather than a paid subscription
#: — and so the downgrade knows which row it is allowed to touch.
LIFETIME_SOURCE = "lifetime"

#: Only used if the account has no subscription at all. The club's regular tariff: never
#: invoiced against this row, but a price column that lies would read as a real 0 € tariff.
PRICE = Decimal("10")
CURRENCY = "EUR"
PERIOD_DAYS = 30


def upgrade() -> None:
    connection = op.get_bind()

    user = connection.execute(
        sa.text(
            "SELECT telegram_id FROM users "
            "WHERE telegram_id = :uid AND LOWER(COALESCE(username, '')) = :username"
        ),
        {"uid": TELEGRAM_ID, "username": USERNAME},
    ).first()
    if user is None:
        # Nobody to grant to: an empty or rebuilt database. See the note in the docstring.
        print(
            f"lifetime subscription: no user {TELEGRAM_ID} (@{USERNAME}) in this database; "
            "nothing granted"
        )
        return

    existing = connection.execute(
        sa.text("SELECT id FROM subscriptions WHERE user_id = :uid"),
        {"uid": TELEGRAM_ID},
    ).first()

    now = datetime.now(UTC)

    if existing is None:
        op.bulk_insert(
            sa.table(
                "subscriptions",
                sa.column("user_id", sa.BigInteger),
                sa.column("status", sa.String),
                sa.column("price", sa.Numeric),
                sa.column("currency", sa.String),
                sa.column("price_tier", sa.String),
                sa.column("period_days", sa.Integer),
                sa.column("started_at", sa.DateTime(timezone=True)),
                sa.column("expires_at", sa.DateTime(timezone=True)),
                sa.column("free_period_granted", sa.Boolean),
                sa.column("source", sa.String),
                sa.column("granted_by", sa.BigInteger),
                sa.column("created_at", sa.DateTime(timezone=True)),
            ),
            [
                {
                    "user_id": TELEGRAM_ID,
                    "status": "active",
                    "price": PRICE,
                    "currency": CURRENCY,
                    "price_tier": "regular",
                    "period_days": PERIOD_DAYS,
                    "started_at": now,
                    "expires_at": LIFETIME_EXPIRES_AT,
                    "free_period_granted": False,
                    "source": LIFETIME_SOURCE,
                    "granted_by": TELEGRAM_ID,
                    "created_at": now,
                }
            ],
        )
    else:
        # The reminder and alert stamps are cleared as well: left set, a stale value would
        # describe a chase that no longer applies to this account.
        connection.execute(
            sa.text(
                "UPDATE subscriptions SET "
                "  status = 'active', "
                "  expires_at = :expires_at, "
                "  grace_until = NULL, "
                "  last_reminder_at = NULL, "
                "  admin_notified_at = NULL, "
                "  source = :source, "
                "  granted_by = :uid "
                "WHERE user_id = :uid"
            ),
            {"expires_at": LIFETIME_EXPIRES_AT, "source": LIFETIME_SOURCE, "uid": TELEGRAM_ID},
        )

    # Stop the poller chasing invoices this account is no longer expected to pay. `pending` is
    # deliberately excluded: that is money in flight, and it must still be allowed to settle.
    closed = connection.execute(
        sa.text(
            "UPDATE payments SET status = 'canceled' "
            "WHERE user_id = :uid AND status IN ('pending_payment', 'error')"
        ),
        {"uid": TELEGRAM_ID},
    ).rowcount

    # S6: a change of this kind must be reconstructable afterwards. actor_id is a foreign key to
    # users, so it has to be an account that exists — the grantee's own id, this being a decision
    # taken outside the bot rather than by another admin through the panel.
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
                "actor_id": TELEGRAM_ID,
                "action": "subscription.granted",
                "target_user_id": TELEGRAM_ID,
                "details": {
                    "reason": "admin account, not billed",
                    "expires_at": LIFETIME_EXPIRES_AT.isoformat(),
                    "source": LIFETIME_SOURCE,
                    "payments_closed": closed,
                    "migration": revision,
                },
                "created_at": now,
            }
        ],
    )


def downgrade() -> None:
    """Put the account back on the ordinary schedule.

    The original `expires_at` is not recoverable — this migration overwrote it and did not keep a
    copy — so the row comes back as due now (`past_due`, expiring at `started_at`), which is what
    an unpaid subscription looks like. The daily job then invoices it like anyone else. Cancelled
    payment rows are left cancelled: re-opening them would have the poller ask WayForPay about
    invoices that have long since expired at the gateway.
    """
    op.execute(
        f"UPDATE subscriptions SET status = 'past_due', expires_at = started_at, "
        f"source = 'wayforpay', granted_by = NULL "
        f"WHERE user_id = {TELEGRAM_ID} AND source = '{LIFETIME_SOURCE}'"
    )
