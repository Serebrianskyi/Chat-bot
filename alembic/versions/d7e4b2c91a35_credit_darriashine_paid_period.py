"""credit darriashine the period they paid for

@darriashine paid on 2026-10-06 and never got the confirmation, because `poll_open_payments` was
dying on every run at the time (see `parse_amount` in services/wayforpay.py: WayForPay answered
CHECK_STATUS with a blank `amount` and `Decimal("")` raised out of the job before it could commit
anything). Their invoice therefore timed out as unpaid while the money had in fact been taken.

This restores the period by hand: active, from 2026-10-06, for one regular month.

What this migration does **not** do, and must not be mistaken for:

* It does not mark any `payments` row as settled. That table is the reconciliation surface against
  WayForPay, and the order they actually paid cannot be identified from here — there are possibly
  several invoices for them and no gateway body was ever stored for any of them. Writing
  `complete` onto a guessed row would corrupt the one record that can be checked against the
  gateway's own. The whole story goes into `audit_log` instead. Pull the order reference out of the
  WayForPay dashboard and that exact row can be settled properly in a follow-up.
* It does not put them in the channel. `deliver_invite` only ever runs from a confirmed payment
  inside `poll_open_payments`, so this migration makes `/subscription` tell the truth but cannot
  grant channel access. Their link comes from the daily `retry_missing_invites` job, which finds
  paid members who are not in the channel — or from 🔗 Надіслати запрошення, immediately.

Open invoices are cancelled, including `pending`. That is the opposite of what `c3a1f0d27b94` does
for @makaolya, and deliberately so: there, nothing had been credited, so money in flight had to be
left alone to settle. Here the period is being credited by hand, so a late settlement would hand
them a *second* month for one payment (standing rule 5).

Data-only and idempotent. Run ONLINE (`alembic upgrade head`): it reads rows to decide what to
write, which offline `--sql` mode cannot do.

Revision ID: d7e4b2c91a35
Revises: c3a1f0d27b94
Create Date: 2026-10-09 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from datetime import UTC, datetime
from decimal import Decimal


# revision identifiers, used by Alembic.
revision: str = 'd7e4b2c91a35'
down_revision: Union[str, Sequence[str], None] = 'c3a1f0d27b94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


USERNAME = "darriashine"

#: Their numeric Telegram id, once known — ids are stable where usernames are not, and an id lets
#: this run even on a database where they have no `users` row yet. Left None, the account is
#: resolved by username instead, which is how the founding-member seed works and is correct as long
#: as they have started the bot. They must have, to have been invoiced at all.
TELEGRAM_ID: int | None = 1687532834

#: They paid on 2026-10-06, so the period runs from then. The end is the close of 05.11.2026 in
#: Kyiv, which is 22:00 UTC: Ukraine is on EET (UTC+2) in November, DST having ended on 25.10.
#: Chosen over `06.10 00:00 UTC + 30 days` so they keep every hour of their last local day; the
#: daily job then invoices them on the morning of 06.11.
STARTED_AT = datetime(2026, 10, 6, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 11, 5, 22, 0, tzinfo=UTC)

#: The regular tariff — SUBSCRIPTION_PRICE / SUBSCRIPTION_PERIOD_DAYS. Written onto the row only
#: when there is no row to update; an existing subscription keeps the price it was created with.
PRICE = Decimal("10")
CURRENCY = "EUR"
PERIOD_DAYS = 30


def _resolve_user(connection) -> int | None:
    """Find the account, by id when one is configured and by username otherwise.

    Returns None when there is nobody to credit. The two reasons for that are very different, so
    they are reported differently: an empty `users` table is a fresh or CI database and this
    migration simply has no work, while a populated one that does not contain them means the
    credit did **not** happen and somebody has to act on it.
    """
    if TELEGRAM_ID is not None:
        row = connection.execute(
            sa.text("SELECT telegram_id FROM users WHERE telegram_id = :uid"),
            {"uid": TELEGRAM_ID},
        ).first()
        if row is not None:
            return int(row[0])
        # An id was given and no row matches it: insert the account rather than skip. The id came
        # from an admin reading it off the participants screen, so it is a real account that for
        # whatever reason is not in this particular database.
        op.bulk_insert(
            sa.table(
                "users",
                sa.column("telegram_id", sa.BigInteger),
                sa.column("username", sa.String),
                sa.column("role", sa.String),
                sa.column("is_blocked_bot", sa.Boolean),
                sa.column("created_at", sa.DateTime(timezone=True)),
            ),
            [
                {
                    "telegram_id": TELEGRAM_ID,
                    "username": USERNAME,
                    "role": "user",
                    "is_blocked_bot": False,
                    "created_at": STARTED_AT,
                }
            ],
        )
        print(f"credit {USERNAME}: inserted missing users row for {TELEGRAM_ID}")
        return TELEGRAM_ID

    row = connection.execute(
        sa.text("SELECT telegram_id FROM users WHERE LOWER(COALESCE(username, '')) = :username"),
        {"username": USERNAME},
    ).first()
    if row is not None:
        return int(row[0])

    total = connection.execute(sa.text("SELECT COUNT(*) FROM users")).scalar() or 0
    if total == 0:
        print(f"credit {USERNAME}: empty users table, nothing to credit")
    else:
        # Loud on purpose. Silence here would mean a member who paid stays uncredited and nobody
        # learns about it until they complain a second time.
        print(
            f"WARNING: credit {USERNAME}: no user named @{USERNAME} among {total} users. "
            f"NOTHING WAS CREDITED. Set TELEGRAM_ID in migration {revision} and re-apply it, "
            f"or check whether they have since changed their Telegram username."
        )
    return None


def upgrade() -> None:
    connection = op.get_bind()
    user_id = _resolve_user(connection)
    if user_id is None:
        return

    now = datetime.now(UTC)

    existing = connection.execute(
        sa.text("SELECT id FROM subscriptions WHERE user_id = :uid"),
        {"uid": user_id},
    ).first()

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
                sa.column("created_at", sa.DateTime(timezone=True)),
            ),
            [
                {
                    "user_id": user_id,
                    "status": "active",
                    "price": PRICE,
                    "currency": CURRENCY,
                    "price_tier": "regular",
                    "period_days": PERIOD_DAYS,
                    "started_at": STARTED_AT,
                    "expires_at": EXPIRES_AT,
                    # They paid, so no free period was ever involved.
                    "free_period_granted": False,
                    # The money did come through WayForPay; only the confirmation was lost.
                    "source": "wayforpay",
                    "created_at": now,
                }
            ],
        )
    else:
        # `price`, `price_tier` and `started_at` are left as the onboarding set them — the period
        # is what was lost, not the tariff. The three chase stamps are cleared because they
        # describe a hunt for a payment that had in fact already been made.
        connection.execute(
            sa.text(
                "UPDATE subscriptions SET "
                "  status = 'active', "
                "  expires_at = :expires_at, "
                "  grace_until = NULL, "
                "  last_reminder_at = NULL, "
                "  admin_notified_at = NULL "
                "WHERE user_id = :uid"
            ),
            {"expires_at": EXPIRES_AT, "uid": user_id},
        )

    # Every still-open invoice for them is written off, `pending` included. See the docstring: the
    # period is credited here, so a late settlement must not add a second one.
    closed = connection.execute(
        sa.text(
            "UPDATE payments SET status = 'canceled' "
            "WHERE user_id = :uid AND status IN ('pending_payment', 'pending', 'error')"
        ),
        {"uid": user_id},
    ).rowcount

    # A live discount would make the renewal quote differ from the regular month credited here.
    # Reported rather than changed: which one is right is the owner's call, not this migration's.
    discounted = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM discounts "
            "WHERE (user_id = :uid OR LOWER(COALESCE(username, '')) = :username) "
            "  AND revoked_at IS NULL"
        ),
        {"uid": user_id, "username": USERNAME},
    ).scalar()
    if discounted:
        print(
            f"note: @{USERNAME} has {discounted} live discount(s); their next invoice will quote "
            "the discounted amount, not the regular one credited here"
        )

    # S6. actor_id is a foreign key to users, so it has to name an account that exists; the
    # grantee's own id is used, as in c3a1f0d27b94, since the decision was taken outside the bot
    # rather than by an admin through the panel. The details carry who and why.
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
                "actor_id": user_id,
                "action": "subscription.granted",
                "target_user_id": user_id,
                "details": {
                    "reason": "paid 2026-10-06; confirmation lost to the poller crash",
                    "credited_from": STARTED_AT.isoformat(),
                    "expires_at": EXPIRES_AT.isoformat(),
                    "period_days": PERIOD_DAYS,
                    "payments_cancelled": closed,
                    "no_payment_row_settled": (
                        "the paid order_reference is unknown; settle it from the WayForPay "
                        "dashboard to reconcile"
                    ),
                    "invite_still_owed": "deliver_invite does not run from a migration",
                    "migration": revision,
                },
                "created_at": now,
            }
        ],
    )


def downgrade() -> None:
    """Take the credited period back.

    The subscription returns to `past_due`, expiring at `started_at`, which is what an unpaid
    subscription looks like — the value this migration overwrote was not kept, and reconstructing
    it is not worth the guesswork. Cancelled invoices stay cancelled: re-opening them would have
    the poller ask WayForPay about orders that expired at the gateway weeks ago.
    """
    if TELEGRAM_ID is not None:
        where = f"user_id = {TELEGRAM_ID}"
    else:
        where = (
            "user_id IN (SELECT telegram_id FROM users "
            f"WHERE LOWER(COALESCE(username, '')) = '{USERNAME}')"
        )
    op.execute(
        f"UPDATE subscriptions SET status = 'past_due', expires_at = started_at WHERE {where}"
    )
