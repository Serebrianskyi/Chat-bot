"""Promo codes: creating a campaign, redeeming it, and spending it. Phase 2A extension.

A promo code is **not** a discount, and keeping them apart is the point of this module.

* A ``Discount`` is "this person's price" — the 8 EUR a founding member was promised. One per
  person, standing until revoked.
* A ``PromoCode`` is "this offer, for whoever types it" — temporary, counted in billing periods.

They **layer**: a member with their own 8 EUR tariff who redeems a promo pays the promo price
while it lasts and then returns to 8, not to the club's 10. ``resolve_price`` below is where that
order is decided, and ``services.discounts.effective_price`` defers to it.

Two month counts that are easy to conflate, and are deliberately independent:

* how long the **code** may be redeemed — a wall-clock window, ``redeemable_until``
* how many **periods** a redeemer gets — ``discount_months``, counted down by invoices

So a code may be redeemable for three months while granting two months of discount.
"""

import logging
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    Discount,
    DiscountKind,
    Payment,
    PaymentStatus,
    PromoCode,
    PromoRedemption,
    Subscription,
)
from services.audit import Action, record_action

log = logging.getLogger(__name__)

#: Offered as buttons when an admin creates a code. Any whole percentage may be typed instead.
PERCENT_CHOICES = (5, 10, 20, 33)

#: Offered as both month counts. Kept small on purpose: a promo that outlives the quarter is a
#: discount, and there is already a screen for those.
MONTH_CHOICES = (1, 2, 3)

#: Days per month for the redemption window. A promo window is approximate by nature — "about
#: three months" — so this does not need calendar arithmetic, and using it keeps the window in
#: the same units as everything else in the project.
WINDOW_DAYS_PER_MONTH = 30


class PromoError(Exception):
    """A code could not be created or redeemed. The message is shown to the admin."""


def normalise_code(raw: str | None) -> str | None:
    """Canonical form: lowercase, trimmed, no leading ``#``.

    Members type whatever casing they like, and a code that works as "halloween" but not
    "Halloween" is a support conversation nobody needs. Applied on **both** create and redeem, so
    the two cannot disagree.
    """
    if raw is None:
        return None
    cleaned = raw.strip().lstrip("#").lower()
    return cleaned or None


def looks_like_code(candidate: str | None) -> bool:
    """Whether this could be a promo code rather than a typo or a sentence.

    Letters, digits, dashes and underscores, 3-32 characters. Checked before storing so an
    accidental "ok" or a pasted paragraph does not become a live campaign.
    """
    if not candidate or not 3 <= len(candidate) <= 32:
        return False
    return all(ch.isalnum() or ch in "-_" for ch in candidate)


async def create(
    session: AsyncSession,
    *,
    actor_id: int,
    code: str,
    kind: DiscountKind,
    percent_off: int | None = None,
    fixed_price: Decimal | None = None,
    currency: str,
    discount_months: int,
    window_months: int,
    new_members_only: bool,
    max_redemptions: int | None = None,
    now: datetime,
) -> PromoCode:
    """Create a campaign. Writes an audit row (S6). The caller commits.

    ``window_months`` is how long the code may be redeemed; ``discount_months`` is what a
    redeemer gets. They are separate arguments because they are separate decisions.
    """
    normalised = normalise_code(code)
    if not looks_like_code(normalised):
        msg = f"{code!r} is not a usable code"
        raise PromoError(msg)

    if kind is DiscountKind.PERCENT:
        if percent_off is None or not 1 <= percent_off <= 100:
            msg = f"percent_off must be between 1 and 100, got {percent_off!r}"
            raise PromoError(msg)
        fixed_price = None
    else:
        if fixed_price is None or fixed_price <= 0:
            msg = f"fixed_price must be greater than zero, got {fixed_price!r}"
            raise PromoError(msg)
        percent_off = None

    for label, months in (("discount_months", discount_months), ("window_months", window_months)):
        if months < 1:
            msg = f"{label} must be at least 1, got {months!r}"
            raise PromoError(msg)

    existing = await by_code(session, normalised)
    if existing is not None:
        # Codes are unique, and silently reusing one would quietly change an offer people may
        # already have been given.
        msg = f"code {normalised!r} already exists"
        raise PromoError(msg)

    promo = PromoCode(
        code=normalised,
        kind=kind,
        percent_off=percent_off,
        fixed_price=fixed_price,
        currency=currency,
        discount_months=discount_months,
        redeemable_until=now + timedelta(days=window_months * WINDOW_DAYS_PER_MONTH),
        new_members_only=new_members_only,
        max_redemptions=max_redemptions,
        created_by=actor_id,
        created_at=now,
    )
    session.add(promo)
    await session.flush()

    await record_action(
        session,
        actor_id=actor_id,
        action=Action.PROMO_CREATED,
        details={
            "code": normalised,
            "kind": kind.value,
            "percent_off": percent_off,
            "fixed_price": None if fixed_price is None else str(fixed_price),
            "currency": currency,
            "discount_months": discount_months,
            "window_months": window_months,
            "new_members_only": new_members_only,
            "max_redemptions": max_redemptions,
        },
        now=now,
    )
    log.info(
        "Admin %s created promo %s: %s for %s period(s), redeemable until %s",
        actor_id,
        normalised,
        f"{percent_off}%" if percent_off else fixed_price,
        discount_months,
        promo.redeemable_until.isoformat(),
    )
    return promo


async def revoke(session: AsyncSession, *, promo: PromoCode, actor_id: int, now: datetime) -> None:
    """Stop future redemptions. Discounts already granted run to their last period.

    Soft, like a discount revocation: somebody who redeemed yesterday keeps what they were
    promised. Taking that back would be a worse surprise than the campaign ending.
    """
    promo.revoked_at = now
    await record_action(
        session,
        actor_id=actor_id,
        action=Action.PROMO_REVOKED,
        details={"code": promo.code, "redemptions": promo.redemptions},
        now=now,
    )
    log.info(
        "Admin %s revoked promo %s after %s redemption(s)",
        actor_id,
        promo.code,
        promo.redemptions,
    )


async def by_code(session: AsyncSession, code: str | None) -> PromoCode | None:
    """Find a campaign by code, however the caller spelled it."""
    normalised = normalise_code(code)
    if normalised is None:
        return None
    return await session.scalar(select(PromoCode).where(PromoCode.code == normalised))


async def list_live(session: AsyncSession, *, now: datetime) -> list[PromoCode]:
    """Campaigns that may still be redeemed, newest first."""
    rows = (
        (
            await session.execute(
                select(PromoCode)
                .where(PromoCode.revoked_at.is_(None), PromoCode.redeemable_until > now)
                .order_by(PromoCode.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [promo for promo in rows if promo.is_redeemable(now)]


async def live_redemption(
    session: AsyncSession, *, user_id: int, now: datetime
) -> PromoRedemption | None:
    """This member's promo discount, if one still has a period to give.

    Deliberately does **not** check whether the campaign is still redeemable: once redeemed, the
    member keeps what they were promised even if the code is revoked or its window closes.
    """
    return await session.scalar(
        select(PromoRedemption)
        .where(PromoRedemption.user_id == user_id, PromoRedemption.periods_remaining > 0)
        .order_by(PromoRedemption.redeemed_at.desc())
        .limit(1)
    )


async def _has_standing_discount(session: AsyncSession, *, user_id: int) -> bool:
    """Whether this member has a price of their own, promo aside.

    Not time-filtered: a discount with a future `valid_until` still counts as "already has one"
    for the purposes of a new-members-only promo. Someone holding a dated discount is not new.
    """
    found = await session.scalar(
        select(Discount.id)
        .where(Discount.user_id == user_id, Discount.revoked_at.is_(None))
        .limit(1)
    )
    return found is not None


async def _has_paid(session: AsyncSession, *, user_id: int) -> bool:
    found = await session.scalar(
        select(Payment.id)
        .where(Payment.user_id == user_id, Payment.status == PaymentStatus.COMPLETE)
        .limit(1)
    )
    return found is not None


async def redeem(
    session: AsyncSession, *, promo: PromoCode, user_id: int, now: datetime
) -> PromoRedemption:
    """Give one member this code's offer. Writes an audit row (S6). The caller commits.

    **Redemption is not payment.** It writes one row and nothing else: no invoice, no change to
    ``expires_at``, no channel link, and the member's standing discount is untouched. What they
    get is a price for their next ``discount_months`` invoices, whenever those fall.

    Raises ``PromoError`` with a member-facing reason. Every refusal is deliberate:

    * already redeemed — the row survives exhaustion, so this also blocks coming back at the end
      of a three-month promo for more cheap months;
    * the window has closed, or the cap is reached;
    * ``new_members_only`` and this member is not new;
    * a different promo is still running for them, because two live promos would make "which
      price applies" a question nobody should have to answer.
    """
    if not promo.is_redeemable(now):
        if promo.revoked_at is not None:
            msg = "promo revoked"
        elif promo.redeemable_until <= now:
            msg = "promo expired"
        else:
            msg = "promo exhausted"
        raise PromoError(msg)

    already = await session.scalar(
        select(PromoRedemption).where(
            PromoRedemption.promo_code_id == promo.id, PromoRedemption.user_id == user_id
        )
    )
    if already is not None:
        # Checked here for a clear message; the UNIQUE constraint is what actually guarantees it.
        msg = "already redeemed"
        raise PromoError(msg)

    running = await live_redemption(session, user_id=user_id, now=now)
    if running is not None:
        msg = "another promo is still running"
        raise PromoError(msg)

    if promo.new_members_only:
        if await _has_standing_discount(session, user_id=user_id):
            msg = "not for members who already have a discount"
            raise PromoError(msg)
        if await _has_paid(session, user_id=user_id):
            msg = "not for members who have already paid"
            raise PromoError(msg)

    redemption = PromoRedemption(
        promo_code_id=promo.id,
        user_id=user_id,
        periods_remaining=promo.discount_months,
        redeemed_at=now,
    )
    session.add(redemption)
    # Maintained alongside the rows so the cap can be checked without counting them.
    promo.redemptions += 1
    await session.flush()

    await record_action(
        session,
        actor_id=user_id,  # the member redeemed it themselves
        action=Action.PROMO_REDEEMED,
        target_user_id=user_id,
        details={
            "code": promo.code,
            "periods": promo.discount_months,
            "kind": promo.kind.value,
            "percent_off": promo.percent_off,
            "fixed_price": None if promo.fixed_price is None else str(promo.fixed_price),
        },
        now=now,
    )
    log.info("%s redeemed promo %s for %s period(s)", user_id, promo.code, promo.discount_months)
    return redemption


def apply_promo(base: Decimal, base_currency: str, promo: PromoCode) -> tuple[Decimal, str]:
    """The price this promo produces from whatever the member would otherwise pay.

    ``base`` is the member's **own** price — their standing discount if they have one, the club's
    tariff if not. A percentage therefore compounds with a standing discount: 20% for a founding
    member on 8 EUR is 6.40, not 8. Owner's decision, 2026-10-29; taking the percentage off the
    club tariff would hand the already-discounted nothing at all, which is precisely the group
    most likely to type a code.

    A fixed price replaces outright and carries its own currency.
    """
    if promo.kind is DiscountKind.PERCENT:
        if not promo.percent_off:
            msg = f"promo {promo.code} is PERCENT but has no percent_off"
            raise PromoError(msg)
        remaining = Decimal(100 - promo.percent_off) / Decimal(100)
        amount = (base * remaining).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return amount, base_currency

    if promo.fixed_price is None:
        msg = f"promo {promo.code} is FIXED_PRICE but has no fixed_price"
        raise PromoError(msg)
    return promo.fixed_price, promo.currency


async def consume_period(
    session: AsyncSession, *, user_id: int, now: datetime
) -> PromoRedemption | None:
    """Spend one period of this member's promo, if they have one. Returns the row it touched.

    Called where the price is actually **charged** — when an invoice is issued and when a stored
    card is charged — not when the promo is redeemed. Decrementing at redemption would hand every
    redeemer one period regardless of whether they ever paid for it, and would start the clock on
    somebody who redeems today and pays in three weeks.

    Reaching zero needs no job: ``resolve_price`` stops seeing the row, so the member falls back
    to their standing discount by arithmetic alone.
    """
    redemption = await live_redemption(session, user_id=user_id, now=now)
    if redemption is None:
        return None

    redemption.periods_remaining -= 1
    if redemption.periods_remaining <= 0:
        redemption.exhausted_at = now
        log.info("Promo for %s is now spent", user_id)
    return redemption


async def promo_price(
    session: AsyncSession,
    *,
    subscription: Subscription,
    standing_amount: Decimal,
    standing_currency: str,
    now: datetime,
) -> tuple[Decimal, str, PromoCode | None]:
    """Layer a live promo over the price the member would otherwise pay.

    Returns the standing amount unchanged when there is no live promo, so the caller can use this
    unconditionally. This is the single place the layering order is decided.
    """
    redemption = await live_redemption(session, user_id=subscription.user_id, now=now)
    if redemption is None:
        return standing_amount, standing_currency, None

    promo = await session.get(PromoCode, redemption.promo_code_id)
    if promo is None:
        # The campaign row is gone, which should not happen; the member keeps their standing
        # price rather than being charged something nobody can explain.
        log.error("Redemption %s points at a missing promo code", redemption.id)
        return standing_amount, standing_currency, None

    amount, currency = apply_promo(standing_amount, standing_currency, promo)
    return amount, currency, promo


async def redemption_count(session: AsyncSession, *, promo_id: int) -> int:
    """How many members have redeemed this code, counted from the rows themselves."""
    return (
        await session.scalar(
            select(func.count(PromoRedemption.id)).where(PromoRedemption.promo_code_id == promo_id)
        )
    ) or 0
