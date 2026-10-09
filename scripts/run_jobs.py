"""Run a billing job now, instead of waiting for its schedule.

The scheduler runs ``poll`` every two minutes and ``due`` once a day at 09:00. Neither is
convenient while testing, and both are safe to run by hand because both are idempotent.

    python -m scripts.run_jobs due     # invoice whoever has come due, then alert admins
    python -m scripts.run_jobs poll    # ask WayForPay about invoices that have not settled
    python -m scripts.run_jobs status  # show what the jobs would see, changing nothing
    python -m scripts.run_jobs invites # get a link to paid members who are not in the channel
    python -m scripts.run_jobs recover # the full daily sweep now, ignoring the once-a-day guard
    python -m scripts.run_jobs reconcile          # dry run: which written-off orders were paid
    python -m scripts.run_jobs reconcile --apply  # credit them, and send their invites

``due`` sends real messages and creates a real WayForPay invoice. ``status`` does neither.
``reconcile`` is read-only until ``--apply``; it never messages anyone who turns out not to have
paid. ``invites`` messages paid members who are outside the channel, and tells an admin about
anyone a retry could not get in.
"""

import argparse
import asyncio
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select

from config import get_settings
from db.models import Payment, PaymentStatus, Subscription, User, utcnow
from db.session import create_engine, create_session_factory
from services.billing import (
    poll_open_payments,
    process_due_subscriptions,
    reconcile_written_off,
    recover_access,
    retry_missing_invites,
)
from services.scheduler import build_billing_config, build_client
from texts import day, money


async def cmd_status() -> int:
    """Read-only: what the jobs would act on right now."""
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    now = utcnow()
    try:
        async with factory() as session:
            subs = (await session.execute(select(Subscription))).scalars().all()
            print(f"now = {now.isoformat()}\n")
            if not subs:
                print("No subscriptions yet. Someone needs to /start the bot.")
            for sub in subs:
                user = await session.get(User, sub.user_id)
                handle = f"@{user.username}" if user and user.username else str(sub.user_id)
                due = "DUE NOW" if sub.expires_at <= now else f"due {day(sub.expires_at.date())}"
                print(
                    f"  {handle:<20} {sub.status.value:<12} {sub.price_tier.value:<11} "
                    f"base {money(sub.price, sub.currency):<10} {due}"
                )

            payments = (await session.execute(select(Payment))).scalars().all()
            open_rows = [
                p
                for p in payments
                if p.status
                in (PaymentStatus.PENDING_PAYMENT, PaymentStatus.PENDING, PaymentStatus.ERROR)
            ]
            print(f"\n  payments: {len(payments)} total, {len(open_rows)} awaiting a result")
            for p in payments[-5:]:
                amount = money(p.amount, p.currency)
                print(f"    {p.order_reference:<28} {p.status.value:<16} {amount}")
    finally:
        await engine.dispose()
    return 0


async def _run(job: str, apply: bool = False) -> int:
    settings = get_settings()
    client = build_client(settings)
    if client is None:
        print("WayForPay is not configured; nothing to do.", file=sys.stderr)
        return 1

    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    bot = Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    config = build_billing_config(settings)
    try:
        if job == "due":
            counts = await process_due_subscriptions(
                factory, client, bot, admin_ids=settings.admin_id_set, config=config
            )
        elif job == "reconcile":
            counts = await reconcile_written_off(
                factory,
                client,
                bot,
                admin_ids=settings.admin_id_set,
                config=config,
                apply=apply,
            )
            if not apply:
                print("DRY RUN — nothing was written. Re-run with --apply to credit these.")
        elif job == "recover":
            # force: asked for by hand, so the once-a-day guard does not apply.
            counts = await recover_access(
                factory,
                client,
                bot,
                admin_ids=settings.admin_id_set,
                config=config,
                force=True,
            )
        elif job == "invites":
            counts = await retry_missing_invites(
                factory,
                bot,
                channel_id=config.channel_id,
                admin_ids=settings.admin_id_set,
            )
        else:
            counts = await poll_open_payments(factory, client, bot, config=config)
        print(counts)
    finally:
        await bot.session.close()
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job", choices=["due", "poll", "status", "reconcile", "invites", "recover"])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="reconcile only: actually credit the orders that come back paid",
    )
    args = parser.parse_args(argv)
    if args.job == "status":
        return asyncio.run(cmd_status())
    return asyncio.run(_run(args.job, apply=args.apply))


if __name__ == "__main__":
    sys.exit(main())
