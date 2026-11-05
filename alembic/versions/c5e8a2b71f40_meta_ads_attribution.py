"""Meta Ads attribution: the click that brought a member in

A Meta Pixel cannot exist inside Telegram — it is JavaScript in a browser, and the member lands in
an app — so ad conversions are reported server to server through the Conversions API
(`services/meta.py`). That needs three things remembered per person.

`fb_click_id` is the `fbclid` Meta appends to an ad's deep link, which the bot reads from the
`/start` payload. `fb_clicked_at` is when it arrived: Meta's `fbc` parameter is
`fb.1.<click time in ms>.<fbclid>`, and a Purchase reported a month after the click must still
carry the original moment rather than the clock at send time.

`fb_lead_sent_at` records that a `Lead` was already reported, so a second `/start` does not report
it again — Meta deduplicates on `event_id` too, but not making the request is better than making
one that is thrown away.

Schema-only and idempotent; no row is touched. NULL in all three means "arrived some other way",
which is true of every member who joined before today.

Revision ID: c5e8a2b71f40
Revises: a7d3f91c56b8
Create Date: 2026-11-05 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5e8a2b71f40'
down_revision: Union[str, Sequence[str], None] = 'a7d3f91c56b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COLUMNS = (
    ("fb_click_id", sa.String(255)),
    ("fb_clicked_at", sa.DateTime(timezone=True)),
    ("fb_lead_sent_at", sa.DateTime(timezone=True)),
)


def _existing(connection) -> set[str]:
    return {column["name"] for column in sa.inspect(connection).get_columns("users")}


def upgrade() -> None:
    connection = op.get_bind()
    present = _existing(connection)
    for column, type_ in COLUMNS:
        if column not in present:
            op.add_column("users", sa.Column(column, type_, nullable=True))


def downgrade() -> None:
    connection = op.get_bind()
    present = _existing(connection)
    for column, _type in reversed(COLUMNS):
        if column in present:
            op.drop_column("users", column)
