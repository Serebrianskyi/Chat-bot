"""Time-limited offers. One campaign at a time, one window, no engine.

Black Friday 2026 is the first: **three months for the price of two**, 27–30 November. What makes
it different from a discount is that it is a *bundle* — one payment buying three billing periods —
and from a promo code that it is bought rather than redeemed.

Three rules shape everything here.

1. **The price is derived, never written down.** The bundle is `PAY_FOR` times the member's own
   standing monthly price, so €10 becomes €20 and the legacy €8 tier becomes €16 by arithmetic. A
   future tier needs no code, and the €16 figure exists in no string — the owner's instruction is
   that it must not appear in any broadcast.
2. **It is priced off the standing price, promos excluded.** Doubling a promo price would hand the
   same discount over twice.
3. **After the bundle, nothing reverts by itself** — because nothing changed. `subscriptions.price`
   and any standing discount are untouched, so the member returns to their own tariff by
   arithmetic. The gateway schedule is told the same figure, dated to the end of the bundle.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Payment, PaymentStatus, Subscription
from services import discounts as discount_service

log = logging.getLogger(__name__)

#: Telegram's broadcast ceiling, shared with `services.broadcast` (rule 11).
SEND_INTERVAL = 1 / 20

#: The campaign's name, as stored on every payment it sells. Short and stable: reports and the
#: "already claimed" check both key on it.
BLACK_FRIDAY = "bf2026"

#: Three periods granted, two paid for.
PERIODS = 3
PAY_FOR = 2


@dataclass(frozen=True)
class Offer:
    """What one member would pay, and what it buys. Built per member, never shared."""

    campaign: str
    periods: int
    amount: Decimal
    currency: str
    #: What the gateway schedule should charge **after** the bundle runs out: their own monthly
    #: price. The spec's "reverts to the baseline" is this field and no job.
    recurring_amount: Decimal

    @property
    def saving(self) -> Decimal:
        """What the member does not pay, for the copy that quotes both figures."""
        return self.recurring_amount * self.periods - self.amount


def is_open(now: datetime, *, starts_at: datetime, ends_at: datetime) -> bool:
    """Whether the campaign is running at ``now``. Inclusive of both ends.

    Compared in UTC like every other timestamp (rule 1); the window is configured with an offset
    so "27 November, Kyiv" means what it says.
    """
    return starts_at <= now <= ends_at


async def offer_for(
    session: AsyncSession,
    *,
    subscription: Subscription,
    username: str | None,
    now: datetime,
    campaign: str = BLACK_FRIDAY,
) -> Offer:
    """The bundle this member would buy, priced from their own standing tariff."""
    standing, currency = await discount_service.standing_price(
        session, subscription=subscription, username=username, now=now
    )
    return Offer(
        campaign=campaign,
        periods=PERIODS,
        amount=(standing * PAY_FOR).quantize(Decimal("0.01")),
        currency=currency,
        recurring_amount=standing,
    )


async def already_claimed(
    session: AsyncSession, *, user_id: int, campaign: str = BLACK_FRIDAY
) -> bool:
    """Whether this member has already bought the bundle. «Разова пропозиція», so once each.

    Only a **settled** payment counts: an abandoned invoice must not lock somebody out of the
    offer they never managed to pay for.
    """
    found = await session.scalar(
        select(Payment.id)
        .where(
            Payment.user_id == user_id,
            Payment.campaign == campaign,
            Payment.status == PaymentStatus.COMPLETE,
        )
        .limit(1)
    )
    return found is not None


def is_active_member(subscription: Subscription, *, now: datetime) -> bool:
    """Which of the two audiences this member is in.

    The spec's own test: `subscription_ends_at > NOW()`. Deliberately the date and not `status`,
    which is a derived mark and can lag a payment by a sweep.
    """
    return subscription.expires_at > now


# --- the scheduled broadcasts ------------------------------------------------------------------
#
# Three sends, each in two variants. The audience split is the spec's own test, the date in the
# copy is the recipient's own, and the button carries **their** invoice — a bundle priced from
# their tariff, which is how the legacy tier pays €16 without that figure existing in any string.


@dataclass(frozen=True)
class Wave:
    """One scheduled send: which copy each audience gets."""

    name: str
    active: str
    lapsed: str


def waves(texts_module) -> dict[str, "Wave"]:
    """The three waves, by name. Takes the texts module so this stays importable without it."""
    return {
        "launch": Wave(
            name="launch",
            active=texts_module.BF_LAUNCH_ACTIVE,
            lapsed=texts_module.BF_OFFER_NEW,
        ),
        "reminder": Wave(
            name="reminder",
            active=texts_module.BF_REMINDER_ACTIVE,
            lapsed=texts_module.BF_REMINDER_NEW,
        ),
        "final": Wave(
            name="final",
            active=texts_module.BF_FINAL_ACTIVE,
            lapsed=texts_module.BF_FINAL_NEW,
        ),
    }


async def send_wave(
    session_factory,
    bot,
    *,
    wave: str,
    settings,
    wayforpay=None,
    billing_config=None,
    admin_ids: frozenset[int] = frozenset(),
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Send one campaign wave to everybody, in the variant their situation calls for.

    **Refuses to send outside the window.** A wave is scheduled for a date and a deploy can be
    late, a clock can drift, and a campaign message arriving on 1 December is worse than one that
    never arrives.

    Each recipient gets their **own** invoice, so the button charges their tariff doubled. That is
    one Purchase per member, which the client paces at `PURCHASE_MIN_INTERVAL` — about seventy
    seconds for the whole club, against an endpoint that answers 429 when pushed harder.

    Anyone who has already bought the bundle is skipped: they have nothing left to buy, and «Ви
    вже скористалися» is not a broadcast.

    ``dry_run`` reports who would receive what and sends nothing.
    """
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

    import texts as texts_module
    from db.models import User
    from handlers import blackfriday
    from services import billing, users

    now = now or datetime.now(tz=UTC)
    counts = {"sent": 0, "blocked": 0, "failed": 0, "claimed": 0, "no_link": 0, "skipped": 0}

    if not is_open(now, starts_at=settings.black_friday_start, ends_at=settings.black_friday_end):
        log.warning("Campaign wave %r not sent: the window is closed", wave)
        return counts

    copy = waves(texts_module).get(wave)
    if copy is None:
        msg = f"unknown campaign wave {wave!r}"
        raise ValueError(msg)

    async with session_factory() as session:
        recipients = list(
            (
                await session.execute(
                    select(Subscription.user_id, Subscription.expires_at)
                    .join(User, User.telegram_id == Subscription.user_id)
                    .where(User.is_blocked_bot.is_(False))
                    .order_by(Subscription.user_id)
                )
            ).all()
        )

    for user_id, expires_at in recipients:
        async with session_factory() as session:
            if await already_claimed(session, user_id=user_id):
                counts["claimed"] += 1
                continue

            user = await session.get(User, user_id)
            link, active = await blackfriday.bundle_link(
                session,
                telegram_id=user_id,
                username=user.username if user else None,
                wayforpay=wayforpay,
                billing_config=billing_config,
                now=now,
            )

        body = (copy.active if active else copy.lapsed).format(until=texts_module.day(expires_at))
        if link is None:
            # The text without the link is the half that does not work, so it is not sent.
            counts["no_link"] += 1
            log.warning("No campaign link for %s; not sending them the wave", user_id)
            continue

        # The spec gives each variant its own buttons: an active member gets the pay button alone,
        # a new or lapsed one also gets «Детальніше» and «Назад» — the same three they would meet
        # inside the bot, so a broadcast is not a worse version of the offer screen.
        markup = blackfriday.offer_keyboard(pay_url=link, active_member=active)

        if dry_run:
            counts["skipped"] += 1
            log.info("Would send %r to %s (%s)", wave, user_id, "active" if active else "lapsed")
            continue

        try:
            await bot.send_message(user_id, body, reply_markup=markup)
            counts["sent"] += 1
        except TelegramForbiddenError:
            counts["blocked"] += 1
            async with session_factory() as session:
                await users.mark_blocked_bot(session, user_id, blocked=True)
        except TelegramRetryAfter as exc:
            # Honoured rather than skipped: dropping somebody because Telegram asked us to slow
            # down is the failure rule 11 exists to prevent.
            await asyncio.sleep(exc.retry_after)
            try:
                await bot.send_message(user_id, body, reply_markup=markup)
                counts["sent"] += 1
            except Exception:
                counts["failed"] += 1
                log.exception("Campaign wave %r failed for %s after a retry", wave, user_id)
        except Exception:
            counts["failed"] += 1
            log.exception("Campaign wave %r failed for %s", wave, user_id)

        await asyncio.sleep(SEND_INTERVAL)

    if admin_ids:
        await billing.notify_admins(
            bot,
            admin_ids,
            texts_module.ADMIN_CAMPAIGN_REPORT.format(
                wave=wave,
                sent=counts["sent"],
                blocked=counts["blocked"],
                claimed=counts["claimed"],
                no_link=counts["no_link"],
                failed=counts["failed"],
            ),
        )

    log.info("Campaign wave %r: %s", wave, counts)
    return counts
