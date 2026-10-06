# Phase 2A: Onboarding and subscription start

Scope agreed 2026-09-30. A thin vertical slice, in the plan's sense — but **not** the plan's
Phase 2. The knowledge base is deferred; this slice draws selected items from the plan's
Phase 3 (subscription dates) and Phase 5 (WayForPay).

Design mechanics live in `docs/telegram-bot-payments-design.md`. This document is scope and gate.

---

## 1. In scope

1. **Onboarding.** `/start` registers the user and creates exactly one subscription.
2. **Three-way price resolution**, decided at `/start`:
   - on the custom-price list → that price, first period free
   - already in the community → regular price, first period free
   - neither → regular price, pays immediately
3. **A payment due date** — `expires_at` — written for every user.
4. **WayForPay:** `CREATE_INVOICE` → send `invoiceUrl` to the user → `CHECK_STATUS` to confirm.
5. **Due-date handling:** invoice the user when the period ends; if it stays unpaid, **notify an
   admin**. The user is *not* removed from the community.

## 2. Explicitly out of scope

Knowledge base (plan Phase 2) · removal from the channel · `CHARGE`/`recToken` renewals ·
networking profiles · broadcasts · statistics · webhooks and hosting.

`recToken` **is** stored when WayForPay returns it, so renewals can be built later without a
migration.

### The deferred removal

Missing payment notifies an admin instead of removing the member. This is deliberate: removal is
irreversible from the member's point of view, and it must not run before the payment path has
been exercised against real money.

`TODO(removal)` markers sit in `services/subscriptions.py` at the exact point where
`ban_chat_member` + `unban_chat_member` belong. Enabling it is its own change, with the plan's
G3.6 and G3.8 as its gate.

---

## 3. Gate 2A

| # | Check | Type |
|---|---|---|
| A.1 | `/start` creates exactly one subscription; a second `/start` does not create another | AUTO |
| A.2 | User on the custom list gets that price and a free first period | AUTO |
| A.3 | Community member (not on the list) gets the regular price and a free first period | AUTO |
| A.4 | Neither → regular price, `expires_at` = now, owes payment immediately | AUTO |
| A.5 | `getChatMember` raising, or `CHANNEL_ID` unset, → treated as a new joiner, never a crash | AUTO |
| A.6 | The free period is granted once; a second `/start` does not extend `expires_at` | AUTO |
| A.7 | After the claim deadline, community membership no longer buys a free period | AUTO |
| A.8 | Custom-list match is by username, resolved to `telegram_id` at `/start` and stored | AUTO |
| A.9 | Request signatures match WayForPay's documented field order for CREATE_INVOICE and CHECK_STATUS | AUTO |
| A.10 | A tampered response signature is rejected | AUTO |
| A.11 | Duplicate `Approved` for one `order_reference` extends access exactly once | AUTO |
| A.12 | `InProcessing` neither grants access nor marks the payment failed | AUTO |
| A.13 | `Declined` marks the payment denied and leaves access untouched | AUTO |
| A.14 | Amount and currency are checked against **that subscription's** price | AUTO |
| A.15 | Raw response body stored for every call, searchable by `order_reference` | AUTO |
| A.16 | Unknown / unmatched `order_reference` is logged, not crashed on | AUTO |
| A.17 | Due date passes unpaid → admin notified, user retained, `audit_log` row written | AUTO |
| A.18 | Admin notification survives an admin who has not started the bot (403 logged, not raised) | AUTO |
| A.19 | All dates stored UTC; `expires_at` arithmetic covered by unit tests | AUTO |
| A.20 | Jobs are idempotent: a second run in the same window changes nothing | AUTO |
| A.21 | Sandbox: one real invoice created, paid, and confirmed by CHECK_STATUS | MAN |
| A.22 | Standing gate S1–S11 | — |

**Exit condition:** a member can start the bot, be priced correctly, receive a working payment
link, and have their payment confirmed — with an admin told when one is missing.

---

## 4. Known limitations, recorded before building

- **Username matching is fragile.** `@username` is mutable and there is no Bot API lookup from
  username to id. A listed member who renamed themselves will not match and falls to the regular
  price; they need a manual fix. Unclaimed list entries stay visible so the gap is auditable.
- **The community branch is unverified.** `CHANNEL_ID` is unset and the bot belongs to no chat,
  so A.3 and A.7 are proven by tests with a stubbed `getChatMember` only. They stay unverified
  against real Telegram until the bot is added to the community as administrator.
- **Phase 1's gate is still open** (S9 phone smoke test, S4 secrets scan). This slice is built on
  top of it at the owner's direction.

---

## 5. Where this stands — 2026-10-06

Every item in scope is built, and several things were added on top as the owner refined the
requirements:

| Added after the original scope | Why |
| --- | --- |
| Invoicing at `/start` rather than only in the daily job | The welcome promised a payment link; the job delivered it eight hours later |
| Pay button on the welcome message itself | It used to arrive in a second message headed "time to renew", for somebody who had just joined |
| `FREE_PERIOD_UNTIL` — one shared end date | "Free until the end of the month" cannot mean 30 days per person, or the club bills on 30 different dates forever |
| `free_first_period` on a discount | The founding list needed a free month *and* a price, and a bot cannot detect most channel members |
| Single-use channel invite on payment | Owner asked for it once `CHANNEL_ID` existed |
| Cancel autorenew, with resume | Required by the specification |
| 👥 Учасники | Owner had no visibility into who had registered |
| Command menu + welcome keyboard | `/admin` was unreachable: nothing linked to it and Telegram listed no commands |
| Two subscription cards | The paid-up card said «Діє до <today>» to someone who owed money |

**Still open on the gate:** A.21 — a sandbox or real payment confirmed end to end. Everything
else is proven by the 30 tests in `tests/test_core.py` or by having been run against the live bot.

**Known gap:** `PAYMENT_FIRST_CONFIRMED` tells a paying member «оплата автоматична», and
automatic renewal is not built. The bot is promising something untrue until `CHARGE` is wired up.
