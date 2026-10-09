"""Why does a member who paid not have access? Read-only.

    python -m scripts.diagnose access     # who paid, who got a link, who is in the channel
    python -m scripts.diagnose reasons     # what WayForPay actually said, grouped
    python -m scripts.diagnose access --channel   # as above, plus a live membership check

Written for one question: of the people who paid, which ones did the bot fail and which ones
simply never used their link? The two look identical from the outside and need opposite responses
— one is a bug to fix, the other a nudge to send.

The evidence used is already in the database:

* a ``payments`` row at ``complete`` means money was confirmed;
* an ``audit_log`` row at ``invite.sent`` means a link was actually delivered — ``deliver_invite``
  writes it only after the member's DM succeeded;
* ``payments.wayforpay_status`` / ``reason_code`` / ``raw_response`` hold the gateway's own words,
  stored verbatim before any business logic ran (standing rule 6).

``--channel`` is the only part that reaches the network: one ``getChatMember`` per member, which
is the sole way a bot can ask about a channel it cannot enumerate. Nothing here writes, anywhere.
"""

import argparse
import asyncio
import sys
from collections import Counter, defaultdict

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy import select

from config import get_settings
from db.models import AuditLog, Payment, PaymentStatus, Subscription, User, utcnow
from db.session import create_engine, create_session_factory
from services.audit import Action

# The same definition the retry job uses. Imported rather than repeated: a diagnostic that
# disagrees with the job about who is in the channel is worse than no diagnostic.
from services.billing import IN_CHANNEL
from texts import day, money


def _handle(user: User | None, user_id: int) -> str:
    """How a member is named in this report: @username, else first name, else the bare id."""
    if user is not None and user.username:
        return f"@{user.username}"
    if user is not None and user.first_name:
        return f"{user.first_name} (id {user_id})"
    return f"id {user_id}"


async def _membership(bot: Bot, channel_id: str, user_id: int) -> str:
    """Live channel membership for one member, or why it could not be read.

    Never raises: a diagnostic that dies on the first unreachable member is useless.
    """
    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - the reason is the useful part of the output
        return f"unknown ({type(exc).__name__})"
    return member.status


async def cmd_access(check_channel: bool) -> int:
    """Classify every member who has a confirmed payment, and everyone the bot may have stranded."""
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    now = utcnow()

    bot = None
    if check_channel:
        if not settings.channel_id:
            print("CHANNEL_ID is not set, so membership cannot be checked.", file=sys.stderr)
            return 1
        bot = Bot(
            token=settings.bot_token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

    try:
        async with factory() as session:
            users = {u.telegram_id: u for u in (await session.execute(select(User))).scalars()}
            subscriptions = {
                s.user_id: s for s in (await session.execute(select(Subscription))).scalars()
            }
            payments = list((await session.execute(select(Payment))).scalars())

            invited = {
                row.target_user_id
                for row in (
                    await session.execute(
                        select(AuditLog).where(AuditLog.action == Action.INVITE_SENT)
                    )
                )
                .scalars()
                .all()
                if row.target_user_id is not None
            }

            by_user: dict[int, list[Payment]] = defaultdict(list)
            for payment in payments:
                by_user[payment.user_id].append(payment)

            paid = sorted(
                uid
                for uid, rows in by_user.items()
                if any(p.status is PaymentStatus.COMPLETE for p in rows)
            )

            print(f"now = {now.isoformat()}")
            print(f"{len(users)} users, {len(payments)} payments, {len(paid)} with a confirmed one")

            # --- the people the question is about -------------------------------------------
            print("\n== CONFIRMED PAYMENT ==")
            if not paid:
                print("  Nobody. No payment has ever reached `complete`.")
            verdicts: Counter[str] = Counter()
            for uid in paid:
                user = users.get(uid)
                sub = subscriptions.get(uid)
                has_invite = uid in invited
                status = await _membership(bot, settings.channel_id, uid) if bot else None

                if status is not None and status in IN_CHANNEL:
                    verdict = "fine: in the channel"
                elif not has_invite:
                    # The bot took the money and never delivered a link. This is on us.
                    verdict = "BOT FAILED THEM: paid, no invite ever sent"
                elif status is None:
                    verdict = "invite sent; membership unchecked (use --channel)"
                else:
                    # A link was delivered and they are not in. Single-use links also expire
                    # after INVITE_VALID_DAYS, so an old one may simply have run out.
                    verdict = "invite sent, not in the channel: they never used it"
                verdicts[verdict] += 1

                until = day(sub.expires_at.date()) if sub else "—"
                line = f"  {_handle(user, uid):<28} until {until:<12} {verdict}"
                print(line if status is None else f"{line} [{status}]")

            # --- the people who may have paid without the bot noticing ----------------------
            # A terminal payment on a subscription that is still unpaid is the shape of the
            # Declined-too-early bug: the order was written off minutes after being issued, so a
            # payment made afterwards was never looked for. These are the rows to check against
            # the WayForPay dashboard by order reference.
            print("\n== WRITTEN OFF WHILE STILL UNPAID (check these against the dashboard) ==")
            suspect = 0
            for uid, rows in sorted(by_user.items()):
                if any(p.status is PaymentStatus.COMPLETE for p in rows):
                    continue
                sub = subscriptions.get(uid)
                if sub is None or sub.expires_at > now:
                    continue
                terminal = [
                    p for p in rows if p.status in (PaymentStatus.DENIED, PaymentStatus.CANCELED)
                ]
                if not terminal:
                    continue
                suspect += 1
                newest = max(terminal, key=lambda p: p.created_at)
                print(
                    f"  {_handle(users.get(uid), uid):<28} {len(terminal)} written off, "
                    f"newest {newest.order_reference} "
                    f"({newest.wayforpay_status}/{newest.reason_code}) "
                    f"{money(newest.amount, newest.currency)}"
                )
            if not suspect:
                print("  None.")

            print("\n== SUMMARY ==")
            for verdict, count in verdicts.most_common():
                print(f"  {count:>4}  {verdict}")
            print(f"  {suspect:>4}  written off while still unpaid")
    finally:
        if bot is not None:
            await bot.session.close()
        await engine.dispose()
    return 0


async def cmd_reasons() -> int:
    """Group every gateway answer we have stored, so the real cause is visible rather than guessed.

    ``transactionStatus`` plus ``reasonCode`` is what distinguishes "this invoice was never opened"
    from "the merchant account refused the currency" — two diagnoses with opposite fixes.
    """
    settings = get_settings()
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            payments = list((await session.execute(select(Payment))).scalars())

            grouped: Counter[tuple[str, str, str]] = Counter()
            samples: dict[tuple[str, str, str], Payment] = {}
            for payment in payments:
                raw = payment.raw_response or {}
                reason = str(raw.get("reason", "")) if isinstance(raw, dict) else ""
                key = (
                    payment.status.value,
                    str(payment.wayforpay_status),
                    f"{payment.reason_code} {reason}".strip(),
                )
                grouped[key] += 1
                samples.setdefault(key, payment)

            print(f"{len(payments)} payments\n")
            print(f"{'our status':<16} {'gateway':<14} {'reasonCode / reason':<34} count  example")
            for (our, gateway, reason), count in grouped.most_common():
                example = samples[(our, gateway, reason)]
                print(f"{our:<16} {gateway:<14} {reason:<34} {count:>5}  {example.order_reference}")

            # The field that decides whether a Declined is real: a refused card comes back with an
            # amount and a masked card number, an untouched invoice with neither.
            print("\nDeclined rows, by whether the gateway sent any payment detail:")
            detail: Counter[str] = Counter()
            for payment in payments:
                if payment.wayforpay_status != "Declined":
                    continue
                raw = payment.raw_response if isinstance(payment.raw_response, dict) else {}
                has_amount = bool(str(raw.get("amount", "") or "").strip())
                has_card = bool(str(raw.get("cardPan", "") or "").strip())
                if has_amount or has_card:
                    detail["with amount/cardPan — a genuine refusal"] += 1
                else:
                    detail["no amount, no cardPan — nothing was ever attempted"] += 1
            for label, count in detail.most_common():
                print(f"  {count:>5}  {label}")
            if not detail:
                print("  none")
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("report", choices=["access", "reasons"])
    parser.add_argument(
        "--channel",
        action="store_true",
        help="also ask Telegram whether each payer is in the channel (one API call per member)",
    )
    args = parser.parse_args(argv)
    if args.report == "reasons":
        return asyncio.run(cmd_reasons())
    return asyncio.run(cmd_access(check_channel=args.channel))


if __name__ == "__main__":
    sys.exit(main())
