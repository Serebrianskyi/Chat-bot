"""the gateway-side monthly schedule, and one row per charge

WayForPay confirmed on 2026-10-29 that card tokenisation is not available on this merchant, so
`CHARGE` against a stored `recToken` can never run. Automatic renewal moves to a schedule WayForPay
owns: a **Purchase** carrying `regularMode=monthly` both takes this period's money and registers a
monthly charge on the member's card. The gateway charges them from then on and posts each result to
`serviceUrl`.

That needs two things this schema could not express.

**Where the schedule is.** `subscriptions` gains `regular_order_reference` (the orderReference of
the Purchase that created it, which is the only key `regularApi` accepts for STATUS / SUSPEND /
REMOVE / EDIT), `regular_status` and `regular_next_at`.

**Which charge a notification is about.** `payments.order_reference` is UNIQUE, and a schedule may
report every monthly charge under the original order number — so the second month cannot be stored
under it, and `apply_payment_result` would read that notification as a duplicate of the first and
extend nothing. Money in, no access. `payments` therefore gains `gateway_order_reference` (the
gateway's string, which CHECK_STATUS must be asked with) and `charge_key` (the gateway's `authCode`,
identifying one charge rather than one order), plus `is_recurring` to tell the two apart in reports.

Schema-only and idempotent; no row is touched. Existing payments keep NULL in all three, which
means "the gateway's reference is ours and this was not a scheduled charge" — true of every row
written before today.

Revision ID: a7d3f91c56b8
Revises: 8370b9c6df2c
Create Date: 2026-10-29 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3f91c56b8'
down_revision: Union[str, Sequence[str], None] = '8370b9c6df2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: (table, column, type, index?) — declared once so upgrade and downgrade cannot disagree.
COLUMNS = (
    ("subscriptions", "regular_order_reference", sa.String(64), True),
    ("subscriptions", "regular_status", sa.String(16), True),
    ("subscriptions", "regular_next_at", sa.DateTime(timezone=True), False),
    ("payments", "gateway_order_reference", sa.String(64), True),
    ("payments", "charge_key", sa.String(64), True),
)


def _existing(connection, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(connection).get_columns(table)}


def upgrade() -> None:
    connection = op.get_bind()

    for table, column, type_, indexed in COLUMNS:
        if column in _existing(connection, table):
            continue
        op.add_column(table, sa.Column(column, type_, nullable=True))
        if indexed:
            op.create_index(f"ix_{table}_{column}", table, [column])

    if "is_recurring" not in _existing(connection, "payments"):
        # server_default for the backfill, then dropped: the model has no server default, and
        # leaving one behind would make this column behave differently from every other boolean.
        op.add_column(
            "payments",
            sa.Column("is_recurring", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        with op.batch_alter_table("payments") as batch:
            batch.alter_column("is_recurring", server_default=None)


def downgrade() -> None:
    connection = op.get_bind()

    if "is_recurring" in _existing(connection, "payments"):
        op.drop_column("payments", "is_recurring")

    for table, column, _type, indexed in reversed(COLUMNS):
        if column not in _existing(connection, table):
            continue
        if indexed:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_column(table, column)
