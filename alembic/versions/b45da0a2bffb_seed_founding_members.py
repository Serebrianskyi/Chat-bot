"""seed founding members

The 17 members who were promised a price and a free first month when the club moved to the
bot. Seeded as a migration rather than a script so a fresh deployment reproduces it exactly
once: alembic records the revision, so `alembic upgrade head` on every redeploy is a no-op
after the first run.

Usernames are stored lowercased, matching `normalise_username`, because Telegram treats them
case-insensitively. Each row is matched at that person's first /start and then pinned to
their numeric id, after which a rename cannot lose it.

The INSERT skips any username already present, so re-running against a partially seeded
database is safe.

Run this ONLINE (`alembic upgrade head`), not with `--sql`: the duplicate check reads the
existing rows, which offline mode cannot do, so an offline run would silently seed nobody.
scripts/start.sh uses the online form.

Revision ID: b45da0a2bffb
Revises: eb3fa5ab8ba6
Create Date: 2026-10-01 21:26:54.817494

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from datetime import UTC, datetime
from decimal import Decimal


# revision identifiers, used by Alembic.
revision: str = 'b45da0a2bffb'
down_revision: Union[str, Sequence[str], None] = 'eb3fa5ab8ba6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


FOUNDING_MEMBERS = [
    ("strokanalina", "10"),
    ("kkirxxaa", "10"),
    ("v_maslowa", "10"),
    ("lana_tiurmina", "10"),
    ("annet070607", "10"),
    ("darynadrobakha", "8"),
    ("sonya_chile", "8"),
    ("anastasiia_lakhmaniuk", "10"),
    ("katya_smm09", "8"),
    ("kateryna_saliieva", "8"),
    ("ddianaorel", "8"),
    ("valeriakordun", "10"),
    ("magical_brands", "10"),
    ("olyovkina", "8"),
    ("shlzhnk_daria", "8"),
    ("napalm_art", "10"),
    ("ururukris", "8"),
]


def upgrade() -> None:
    """Insert one fixed-price, free-first-period discount per founding member."""
    discounts = sa.table(
        "discounts",
        sa.column("username", sa.String),
        sa.column("kind", sa.String),
        sa.column("fixed_price", sa.Numeric),
        sa.column("currency", sa.String),
        sa.column("free_first_period", sa.Boolean),
        sa.column("note", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    connection = op.get_bind()
    now = datetime.now(UTC)

    existing = {
        row[0]
        for row in connection.execute(
            sa.text("SELECT username FROM discounts WHERE username IS NOT NULL")
        )
    }

    pending = [
        {
            "username": username,
            "kind": "fixed_price",
            "fixed_price": Decimal(price),
            "currency": "EUR",
            "free_first_period": True,
            "note": "засновник клубу",
            "created_at": now,
        }
        for username, price in FOUNDING_MEMBERS
        if username not in existing
    ]
    if pending:
        op.bulk_insert(discounts, pending)


def downgrade() -> None:
    """Remove only the rows this migration added, and only if nobody has claimed them.

    A claimed row (resolved to a real user id) is left alone: by then it is part of a live
    subscription's history, not seed data.
    """
    names = ", ".join(f"'{username}'" for username, _ in FOUNDING_MEMBERS)
    op.execute(
        f"DELETE FROM discounts WHERE username IN ({names}) AND user_id IS NULL"
    )
