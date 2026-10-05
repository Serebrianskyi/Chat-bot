"""Deciding what a user pays, and when. Phase 2A.

Pure logic: no Telegram, no database, no clock of its own — ``now`` is always passed in. That is
what makes the three-way decision (A.2, A.3, A.4) and the deadline rule (A.7) testable without
a live community.

What this module decides at ``/start``:

1. the **base price** — the regular tariff, for everyone;
2. the **tier** — how the member arrived, kept for reporting;
3. whether the **first period is free**.

What it deliberately does not decide is the amount actually charged. A discount can expire, so the
charged amount is computed per invoice by ``services.discounts.effective_price``. Freezing it here
would make every discount permanent.

Two independent questions, deliberately not conflated:

* **What do they pay?** The base tariff, reduced by whatever discount is live at invoicing time.
* **Is the first period free?** For someone already in the community when they started the bot,
  or for a listed member whose entry explicitly grants one. Either way the deadline closes it.

Price and the free period stay separate questions: a discount on its own does not buy a free
month, and a free month does not imply a discount. The migration list happens to carry both.

Tier is for reporting only, highest first: discounted, community member, new joiner.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from db.models import PriceTier


@dataclass(frozen=True)
class PriceDecision:
    """What a subscription should be created with."""

    #: The member's base price. What they are charged is this, minus any live discount.
    price: Decimal
    currency: str
    tier: PriceTier
    first_period_free: bool

    @property
    def owes_payment_now(self) -> bool:
        return not self.first_period_free


def decide_price(
    *,
    regular_price: Decimal,
    regular_currency: str,
    has_discount: bool,
    in_community: bool,
    now: datetime,
    free_period_until: datetime | None,
    discount_grants_free_period: bool = False,
) -> PriceDecision:
    """Resolve base price, tier and free period for one user at ``/start``.

    ``free_period_until`` is both the end of the free period and the close of the offer: before it,
    a qualifying member is free until that date; after it, nobody gets a free period and billing is
    monthly from the start. One date rather than two settings, because they are the same decision.

    Until the channel is gated, anyone could join it and then start the bot to claim a free month,
    which is the other reason the offer needs an end.

    The base price is the regular tariff in every case. A discounted member is marked as such by
    the tier, not by a reduced ``price``: the reduction is applied per invoice, so it can end.
    """
    offer_open = free_period_until is None or now < free_period_until

    # Two independent ways to earn the free first period:
    #   * already in the community when they started the bot (the migration incentive), or
    #   * a pre-authorised entry that explicitly grants one.
    # The second exists because a bot cannot enumerate a channel's members, so most existing
    # members are undetectable and have to be listed by hand instead.
    first_period_free = (in_community or discount_grants_free_period) and offer_open

    if has_discount:
        tier = PriceTier.DISCOUNTED
    elif in_community:
        tier = PriceTier.COMMUNITY
    else:
        tier = PriceTier.REGULAR

    return PriceDecision(
        price=regular_price,
        currency=regular_currency,
        tier=tier,
        first_period_free=first_period_free,
    )


def first_expiry(
    decision: PriceDecision,
    *,
    now: datetime,
    period_days: int,
    free_period_until: datetime | None = None,
) -> datetime:
    """When the first payment falls due.

    With ``free_period_until`` set, every free first period ends on that **same date** rather than
    N days after each person happened to join. Someone starting on the 5th and someone starting on
    the 28th both pay from the same day, which is what "free until the end of the month" means —
    staggered 30-day windows would have the club billing people on 30 different dates forever.

    Without it, a free period simply runs one period from today.

    Everyone who owes immediately gets ``now``, so the due-date job picks them up on its next run
    rather than needing a separate "unpaid from the start" branch.
    """
    if not decision.first_period_free:
        return now
    if free_period_until is not None:
        # The caller only grants a free period while the offer is open, so this is in the future.
        return free_period_until
    return now + timedelta(days=period_days)


def extend(current_expiry: datetime, *, now: datetime, period_days: int) -> datetime:
    """Advance the due date by one period after a confirmed payment.

    Extends from ``current_expiry`` when the subscription is still current, so paying early never
    costs the user the days they already have. Once it has lapsed, extends from ``now``, so a
    month bought today is a month from today rather than backdated into the gap.
    """
    base = current_expiry if current_expiry > now else now
    return base + timedelta(days=period_days)
