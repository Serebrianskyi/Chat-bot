"""Manage discounts from the command line.

The same thing the admin panel does, for when a list arrives as a file rather than one name at a
time. Everything here is also available in the bot: 🎟 Знижки and 🎁 Надати знижку.

    python -m scripts.discounts list
    python -m scripts.discounts fixed 8 --note "учень" -- student_one @Student_Two
    python -m scripts.discounts percent 20 --days 90 --note "промо" -- someone_else
    python -m scripts.discounts revoke student_one

Usernames are stored lowercased without the leading ``@``; Telegram treats them
case-insensitively. A discount for someone who has not started the bot waits until they do.
"""

import argparse
import asyncio
import sys
from decimal import Decimal, InvalidOperation

from config import get_settings
from db.models import DiscountKind, looks_like_username, normalise_username, utcnow
from db.session import create_engine, create_session_factory
from services import discounts as discount_service
from services.discounts import DiscountError

#: Recorded as the actor for anything done from the shell, so audit rows are never anonymous.
CLI_ACTOR = 0


def _factory():
    settings = get_settings()
    engine = create_engine(settings.database_url)
    return settings, engine, create_session_factory(engine)


def _describe(discount) -> str:
    if discount.kind is DiscountKind.PERCENT:
        return f"{discount.percent_off}% off"
    return f"{discount.fixed_price} {discount.currency}"


async def cmd_list() -> int:
    _, engine, factory = _factory()
    now = utcnow()
    try:
        async with factory() as session:
            active = await discount_service.list_active(session, now=now)
            if not active:
                print("No active discounts.")
                return 0
            unclaimed = sum(1 for d in active if d.user_id is None)
            print(f"{len(active)} active, {unclaimed} not yet activated by their holder\n")
            for d in active:
                until = d.valid_until.date().isoformat() if d.valid_until else "no expiry"
                claimed = "claimed" if d.user_id is not None else "WAITING"
                note = f"  ({d.note})" if d.note else ""
                print(f"  {d.target:<26} {_describe(d):<14} until {until:<12} {claimed}{note}")
    finally:
        await engine.dispose()
    return 0


async def _grant(
    kind: DiscountKind, value: str, days: int | None, note: str | None, usernames: list[str]
) -> int:
    settings, engine, factory = _factory()
    now = utcnow()
    failures = 0
    try:
        async with factory() as session:
            for raw in usernames:
                target_id = int(raw) if raw.lstrip("-").isdigit() else None
                handle = normalise_username(raw) if target_id is None else None
                if target_id is None and not looks_like_username(handle):
                    print(f"  skipped {raw!r}: not a valid username or id", file=sys.stderr)
                    failures += 1
                    continue
                try:
                    discount = await discount_service.grant(
                        session,
                        actor_id=CLI_ACTOR,
                        telegram_id=target_id,
                        username=None if target_id is not None else handle,
                        kind=kind,
                        percent_off=int(value) if kind is DiscountKind.PERCENT else None,
                        fixed_price=Decimal(value) if kind is DiscountKind.FIXED_PRICE else None,
                        currency=settings.subscription_currency,
                        days=days,
                        note=note,
                        now=now,
                    )
                except (DiscountError, InvalidOperation, ValueError) as exc:
                    print(f"  skipped {raw!r}: {exc}", file=sys.stderr)
                    failures += 1
                    continue
                print(f"  {discount.target}: {_describe(discount)}")
            await session.commit()
    finally:
        await engine.dispose()
    return 1 if failures else 0


async def cmd_revoke(username: str) -> int:
    _, engine, factory = _factory()
    now = utcnow()
    try:
        async with factory() as session:
            handle = normalise_username(username)
            target_id = int(username) if username.lstrip("-").isdigit() else None
            found = await discount_service.find_active(
                session, telegram_id=target_id or 0, username=handle, now=now
            )
            if found is None:
                print(f"No active discount for {username}.", file=sys.stderr)
                return 1
            await discount_service.revoke(session, discount=found, actor_id=CLI_ACTOR, now=now)
            await session.commit()
            print(f"Revoked the discount for {found.target}.")
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="every discount in force")

    fixed = sub.add_parser("fixed", help="a fixed price for one or more people")
    fixed.add_argument("price")
    fixed.add_argument("--days", type=int, default=None, help="omit for no expiry")
    fixed.add_argument("--note", default=None)
    fixed.add_argument("usernames", nargs="+")

    percent = sub.add_parser("percent", help="a percentage off for one or more people")
    percent.add_argument("percent")
    percent.add_argument("--days", type=int, default=None, help="omit for no expiry")
    percent.add_argument("--note", default=None)
    percent.add_argument("usernames", nargs="+")

    revoke = sub.add_parser("revoke", help="end someone's discount now")
    revoke.add_argument("username")

    args = parser.parse_args(argv)

    if args.command == "list":
        return asyncio.run(cmd_list())
    if args.command == "fixed":
        return asyncio.run(
            _grant(DiscountKind.FIXED_PRICE, args.price, args.days, args.note, args.usernames)
        )
    if args.command == "percent":
        return asyncio.run(
            _grant(DiscountKind.PERCENT, args.percent, args.days, args.note, args.usernames)
        )
    if args.command == "revoke":
        return asyncio.run(cmd_revoke(args.username))
    return 1


if __name__ == "__main__":
    sys.exit(main())
