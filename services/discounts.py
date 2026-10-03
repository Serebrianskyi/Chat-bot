"""Discounts: finding them, applying them, granting and revoking them. Phase 2A.

The charged amount is **computed**, never read from a snapshot. ``subscriptions.price`` is the
member's base price; the amount on an invoice is that base with whatever discount is active at
that moment. Reading a stored price instead would make a three-month discount permanent, which is
the whole point of ``valid_until``.

At most one discount is active per person. ``grant`` enforces that by revoking any existing one,
so there is never an ambiguous "which discount applies" question.
"""

import logging
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Discount, DiscountKind, Subscription, normalise_username
from services.audit import Action, record_action

log = logging.getLogger(__name__)


class DiscountError(ValueError):
    """The requested discount does not make sense."""


def apply_to(base_price: Decimal, base_currency: str, discount: Discount) -> tuple[Decimal, str]:
    """The price after this discount. Returns ``(amount, currency)``.

    A percentage keeps the base currency; a fixed price carries its own, so a discount can be
    quoted in a different currency from the standard tariff if that is ever needed.
    """
    if discount.kind is DiscountKind.PERCENT:
        if not discount.percent_off:
            msg = f"discount {discount.id} is PERCENT but has no percent_off"
            raise DiscountError(msg)
        remaining = Decimal(100 - discount.percent_off) / Decimal(100)
        amount = (base_price * remaining).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return amount, base_currency

    if discount.fixed_price is None:
        msg = f"discount {discount.id} is FIXED_PRICE but has no fixed_price"
        raise DiscountError(msg)
    return discount.fixed_price, discount.currency


async def find_active(
    session: AsyncSession, *, telegram_id: int, username: str | None, now: datetime
) -> Discount | None:
    """The discount that applies to this person right now, if any.

    Matched by pinned ``user_id`` first, then by username for a list entry never claimed. Newest
    first, so a freshly granted discount wins if an older one somehow survived.
    """
    handle = normalise_username(username)
    conditions = [Discount.user_id == telegram_id]
    if handle is not None:
        conditions.append(Discount.username == handle)

    candidates = (
        (
            await session.execute(
                select(Discount)
                .where(or_(*conditions), Discount.revoked_at.is_(None))
                .order_by(Discount.created_at.desc(), Discount.id.desc())
            )
        )
        .scalars()
        .all()
    )

    for discount in candidates:
        if discount.is_active(now):
            return discount
    return None


async def claim(
    session: AsyncSession, *, discount: Discount, telegram_id: int, now: datetime
) -> None:
    """Pin a list-matched discount to a real id, so a later rename cannot lose it."""
    if discount.user_id is None:
        discount.user_id = telegram_id
        discount.claimed_at = now


async def effective_price(
    session: AsyncSession, *, subscription: Subscription, username: str | None, now: datetime
) -> tuple[Decimal, str, Discount | None]:
    """What this member owes for the coming period, and why.

    Called at invoicing time, which is what makes ``valid_until`` meaningful: the same member is
    charged the discounted amount while the discount lives and the base amount afterwards, with no
    job needed to "expire" anything.
    """
    discount = await find_active(
        session, telegram_id=subscription.user_id, username=username, now=now
    )
    if discount is None:
        return subscription.price, subscription.currency, None
    amount, currency = apply_to(subscription.price, subscription.currency, discount)
    return amount, currency, discount


async def grant(
    session: AsyncSession,
    *,
    actor_id: int,
    telegram_id: int | None = None,
    username: str | None = None,
    kind: DiscountKind,
    percent_off: int | None = None,
    fixed_price: Decimal | None = None,
    currency: str,
    days: int | None = None,
    note: str | None = None,
    now: datetime,
) -> Discount:
    """Create a discount, revoking whatever the person had before. Writes an audit row (S6).

    ``days=None`` means no expiry. The caller commits.
    """
    handle = normalise_username(username)
    if telegram_id is None and handle is None:
        msg = "a discount needs either a telegram_id or a username"
        raise DiscountError(msg)

    if kind is DiscountKind.PERCENT:
        if percent_off is None or not 1 <= percent_off <= 100:
            msg = f"percent_off must be between 1 and 100, got {percent_off!r}"
            raise DiscountError(msg)
        fixed_price = None
    else:
        if fixed_price is None or fixed_price <= 0:
            msg = f"fixed_price must be greater than zero, got {fixed_price!r}"
            raise DiscountError(msg)
        percent_off = None

    if days is not None and days <= 0:
        msg = f"days must be positive, got {days!r}"
        raise DiscountError(msg)

    # One active discount per person, so "which one applies" never needs deciding.
    for existing in await list_active(session, now=now):
        matches_id = telegram_id is not None and existing.user_id == telegram_id
        matches_name = handle is not None and existing.username == handle
        if matches_id or matches_name:
            existing.revoked_at = now

    discount = Discount(
        username=handle,
        user_id=telegram_id,
        kind=kind,
        percent_off=percent_off,
        fixed_price=fixed_price,
        currency=currency,
        valid_until=(now + timedelta(days=days)) if days is not None else None,
        note=note,
        granted_by=actor_id,
        created_at=now,
        claimed_at=now if telegram_id is not None else None,
    )
    session.add(discount)
    await session.flush()

    await record_action(
        session,
        actor_id=actor_id,
        action=Action.DISCOUNT_GRANTED,
        target_user_id=telegram_id,
        details={
            "discount_id": discount.id,
            "username": handle,
            "kind": kind.value,
            "percent_off": percent_off,
            "fixed_price": str(fixed_price) if fixed_price is not None else None,
            "currency": currency,
            "days": days,
            "valid_until": discount.valid_until.isoformat() if discount.valid_until else None,
            "note": note,
        },
    )
    log.info("Discount %s granted to %s by %s", discount.id, discount.target, actor_id)
    return discount


async def revoke(
    session: AsyncSession, *, discount: Discount, actor_id: int, now: datetime
) -> None:
    """End a discount now. Soft, so past invoices stay explainable. Writes an audit row."""
    if discount.revoked_at is not None:
        return
    discount.revoked_at = now
    await record_action(
        session,
        actor_id=actor_id,
        action=Action.DISCOUNT_REVOKED,
        target_user_id=discount.user_id,
        details={"discount_id": discount.id, "username": discount.username},
    )


async def list_active(session: AsyncSession, *, now: datetime) -> list[Discount]:
    """Every discount in force right now, newest first.

    Backs the admin list. Filtering ``is_active`` in Python rather than SQL keeps one definition
    of "active" — the model's — instead of a second copy in a WHERE clause that could drift.
    """
    rows = (
        (
            await session.execute(
                select(Discount)
                .where(Discount.revoked_at.is_(None))
                .order_by(Discount.created_at.desc(), Discount.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [row for row in rows if row.is_active(now)]


async def list_unclaimed(session: AsyncSession, *, now: datetime) -> list[Discount]:
    """Active discounts whose person has never started the bot — the gap worth watching."""
    return [d for d in await list_active(session, now=now) if d.user_id is None]
