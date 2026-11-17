"""a payment may buy more than one period

Until now `apply_payment_result` extended access by exactly `period_days`, whatever the amount —
correct while every order was one month, and wrong the moment a campaign sells three. A €20 Black
Friday bundle would have granted thirty days: the member charged for three months and given one.

`payments.periods` is how many billing periods the money bought, and `payments.campaign` names the
campaign that sold it, which is what makes "how many did we sell" and "has this member already
claimed the one-per-person offer" answerable.

Schema-only and idempotent; no row is touched. Every existing payment keeps `periods = 1` and a
NULL campaign, which is exactly what it was.

Revision ID: d9f2b63c08a1
Revises: c5e8a2b71f40
Create Date: 2026-11-17 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9f2b63c08a1'
down_revision: Union[str, Sequence[str], None] = 'c5e8a2b71f40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing(connection) -> set[str]:
    return {column["name"] for column in sa.inspect(connection).get_columns("payments")}


def upgrade() -> None:
    connection = op.get_bind()
    present = _existing(connection)

    if "periods" not in present:
        # server_default for the backfill, then dropped: the model has no server default, and
        # leaving one would make this column behave unlike every other integer here.
        op.add_column(
            "payments", sa.Column("periods", sa.Integer(), nullable=False, server_default="1")
        )
        with op.batch_alter_table("payments") as batch:
            batch.alter_column("periods", server_default=None)

    if "campaign" not in present:
        op.add_column("payments", sa.Column("campaign", sa.String(32), nullable=True))
        op.create_index("ix_payments_campaign", "payments", ["campaign"])


def downgrade() -> None:
    connection = op.get_bind()
    present = _existing(connection)
    if "campaign" in present:
        op.drop_index("ix_payments_campaign", table_name="payments")
        op.drop_column("payments", "campaign")
    if "periods" in present:
        op.drop_column("payments", "periods")
