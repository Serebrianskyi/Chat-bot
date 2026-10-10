"""free-basis members pay from 2026-11-01

The people who were added to the channel for free — the founding members seeded by
`b45da0a2bffb` — are on a trial until the end of October and owe their first payment on
**2026-11-01**. Anyone who started the bot before their entry existed, or before
`FREE_PERIOD_UNTIL` was configured, was onboarded as an ordinary joiner instead: due
immediately, invoiced daily, and shown in ⏳ Очікують оплати. This puts them where they belong.

**Tariffs are not touched.** Each of them has their own price — 8 or 10 EUR — and it lives in
their `discounts` row, which `effective_price` reads per invoice. This migration changes only
*when* the first payment falls due, plus the status that says so.

Who is "free-basis" is read from the data rather than from a second copy of the name list:
a `discounts` row with `free_first_period` set is exactly that promise, so the group stays
correct if the list is ever extended.

Not touched either:

* anyone with a `complete` payment — they have paid, their `expires_at` is theirs, and moving
  it back to 1 November would take away a period they bought;
* anyone with no `users` row, because they have never started the bot. There is nothing to put a
  subscription on, and `/start` already gives them the free period while the offer is open. The
  consequence is worth knowing: a founding member who first opens the bot **after** 1 November
  gets no free period at all, because `decide_price` closes the offer on that date. That is a
  policy question, not something a migration should decide quietly.

Open invoices are cancelled: a member who owes nothing until November should not be holding a
live payment link, and the poller should stop asking about it.

Data-only and idempotent. Run ONLINE (`alembic upgrade head`).

Revision ID: e8c5a1f3d209
Revises: d7e4b2c91a35
Create Date: 2026-10-10 11:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from datetime import UTC, datetime


# revision identifiers, used by Alembic.
revision: str = 'e8c5a1f3d209'
down_revision: Union[str, Sequence[str], None] = 'd7e4b2c91a35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The end of the free period, and the day the first payment falls due. 2026-11-01 00:00 in Kyiv
#: is 2026-10-31 22:00 UTC — Ukraine is on EET (UTC+2) by then, daylight saving having ended on
#: 25 October. Stored UTC like every other timestamp (standing gate S1), and deliberately the
#: same instant as `FREE_PERIOD_UNTIL` in the environment, so a member fixed up here and a member
#: onboarded by `/start` get the identical due date.
FREE_PERIOD_END = datetime(2026, 10, 31, 22, 0, tzinfo=UTC)


def _free_basis_user_ids(connection) -> list[int]:
    """Telegram ids of the free-basis members who have started the bot.

    Resolved from the ``discounts`` rows carrying ``free_first_period``: that flag *is* the
    promise of a free first period, so the group is read from the data rather than from a second
    copy of the name list that could drift away from it.
    """
    rows = connection.execute(
        sa.text(
            "SELECT user_id, username FROM discounts "
            "WHERE free_first_period = true AND revoked_at IS NULL"
        )
    ).all()

    ids: list[int] = []
    for user_id, username in rows:
        if user_id is not None:
            found = connection.execute(
                sa.text("SELECT telegram_id FROM users WHERE telegram_id = :uid"),
                {"uid": user_id},
            ).first()
        elif username:
            # Never claimed: the entry is still keyed by username, so match that way.
            found = connection.execute(
                sa.text(
                    "SELECT telegram_id FROM users "
                    "WHERE LOWER(COALESCE(username, '')) = :username"
                ),
                {"username": str(username).lower()},
            ).first()
        else:
            found = None
        if found is not None:
            ids.append(int(found[0]))
    return ids


def upgrade() -> None:
    connection = op.get_bind()
    now = datetime.now(UTC)

    candidates = _free_basis_user_ids(connection)
    if not candidates:
        print("free-basis members: none of them have started the bot in this database")
        return

    moved: list[int] = []
    paid: list[int] = []
    for user_id in candidates:
        has_subscription = connection.execute(
            sa.text("SELECT 1 FROM subscriptions WHERE user_id = :uid"),
            {"uid": user_id},
        ).first()
        if has_subscription is None:
            continue

        already_paid = connection.execute(
            sa.text("SELECT 1 FROM payments WHERE user_id = :uid AND status = 'complete'"),
            {"uid": user_id},
        ).first()
        if already_paid is not None:
            # They bought a period. Moving their expiry back to November would take it away.
            paid.append(user_id)
            continue

        # `price`, `currency` and `price_tier` are untouched on purpose: their tariff is their
        # own and is applied per invoice from the discounts row. Only the due date moves, and the
        # status that explains it. The chase stamps are cleared because they describe a hunt for
        # a payment that is not owed yet.
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
        # poller has nothing to ask about. Terminal rows are left as they are.
        connection.execute(
            sa.text(
                "UPDATE payments SET status = 'canceled' "
                "WHERE user_id = :uid AND status IN ('pending_payment', 'pending', 'error')"
            ),
            {"uid": user_id},
        )
        moved.append(user_id)

    if moved:
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
                    # actor_id is a foreign key to users, so it must name a row that exists.
                    "actor_id": user_id,
                    "action": "subscription.granted",
                    "target_user_id": user_id,
                    "details": {
                        "reason": "free-basis member; first payment due 2026-11-01",
                        "expires_at": FREE_PERIOD_END.isoformat(),
                        "tariff": "unchanged, applied per invoice from their discount",
                        "migration": revision,
                    },
                    "created_at": now,
                }
                for user_id in moved
            ],
        )

    print(f"free-basis members: {len(moved)} now on trial until 2026-11-01 {moved}")
    if paid:
        print(f"free-basis members: {len(paid)} already paid, left alone {paid}")


def downgrade() -> None:
    """Put them back on the ordinary schedule.

    The dates this overwrote were not kept, so the rows return to `past_due` expiring at
    `started_at` — what an unpaid subscription looks like — and the daily job invoices them like
    anyone else. Cancelled invoices stay cancelled.

    The rows are found the same way ``upgrade`` found them, by resolving the free-basis members
    again, rather than by matching on the timestamp this wrote. Comparing a bound datetime
    against a stored one is exactly the kind of thing that matches on PostgreSQL and silently
    matches nothing on SQLite, leaving a downgrade that reports success and reverts no rows.
    """
    connection = op.get_bind()
    for user_id in _free_basis_user_ids(connection):
        connection.execute(
            sa.text(
                "UPDATE subscriptions SET status = 'past_due', expires_at = started_at, "
                "free_period_granted = false "
                "WHERE user_id = :uid AND status = 'trial'"
            ),
            {"uid": user_id},
        )
