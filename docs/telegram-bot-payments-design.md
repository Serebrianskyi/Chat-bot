# Payments and subscription lifecycle: design

Decided 2026-09-29. Companion to `telegram-bot-implementation-plan.md` (Phases 3 and 5) and
`telegram-bot-quality-gates.md` (Gates 3 and 5). Where this document and those disagree, they
win and this one gets corrected.

---

## 1. Decision

**One invoice per user per period. No WayForPay-side subscriptions.**

WayForPay can hold a real subscription object and charge on its own schedule (`regularApi`).
We are not using it, for two reasons:

1. **Per-user pricing.** Each period's charge is built from that user's own price, so
   discounts, founder rates and one-off deals need no provider-side plan objects.
2. **No public HTTPS.** The bot runs on a laptop in polling mode. WayForPay announces each
   automatic charge by POSTing to `serviceUrl`, which nothing here can receive. Merchant-driven
   charging inverts that: we initiate, and the answer arrives in the same HTTP response.

Consequence: `expires_at` in our database **is** the schedule. WayForPay only executes charges
when asked.

---

## 2. Mechanism

Every call is `POST https://api.wayforpay.com/api`, differing by `transactionType`.

| Step | Call | User action | Result |
|---|---|---|---|
| First payment | `CREATE_INVOICE` | taps the link | `invoiceUrl` → poll `CHECK_STATUS` |
| Confirmation | `CHECK_STATUS` | none | `Approved` → activate, store `recToken` |
| Every renewal | `CHARGE` + `recToken` | **none** | synchronous `transactionStatus` |

A fresh `orderReference` per transaction — it is UNIQUE and is the join key to WayForPay.
`recToken` is the only value carried across periods.

The bot does not *receive* the payment link; it requests it. `CREATE_INVOICE` returns
`invoiceUrl` in the response body, which goes on an inline button with `url=`.

### Signatures — HMAC-MD5 over `;`-joined UTF-8, keyed with the merchant Secret Key

| Purpose | String |
|---|---|
| `CREATE_INVOICE` / `CHARGE` request | `merchantAccount;merchantDomainName;orderReference;orderDate;amount;currency;productName[0..n];productCount[0..n];productPrice[0..n]` |
| `CHECK_STATUS` request | `merchantAccount;orderReference` |
| Any response — verify before trusting | `merchantAccount;orderReference;amount;currency;authCode;cardPan;transactionStatus;reasonCode` |

Request order is all names, then all counts, then all prices. `amount` must be formatted
**identically** in the signature and the payload — `"300"` vs `"300.00"` is the usual cause of a
signature mismatch.

---

## 3. Payment statuses

Modelled on SendPulse's set rather than the plan's three, because refunds and settlement delays
each need their own state.

| `payments.status` | WayForPay `transactionStatus` | Terminal | Grants access |
|---|---|---|---|
| `pending_payment` | *(no transaction yet)* | no | no |
| `pending` | `InProcessing` | no | **no** |
| `complete` | `Approved` | yes | **yes** |
| `denied` | `Declined` | yes | no |
| `canceled` | *(timed out / abandoned)* | yes | no |
| `refunded` | `Refunded` | yes | **revokes** |
| `refunded_partial` | partial refund | yes | policy decision |
| `reversed` | gateway reversal, no merchant action | yes | **revokes** |
| `error` | transport failure | no | no — retry the check |

Two rules follow:

- **Only `complete` grants or extends access.** `pending` means the user has paid and sees
  success while the money is still settling: keep polling, do not grant, do not fail it.
- **`refunded` and `reversed` revoke access.** The plan's gate does not cover this path; it is
  why G5.14 requires refunding a real payment and checking the resulting state.

---

## 4. Subscription lifecycle

| `subscriptions.status` | Meaning | In the group |
|---|---|---|
| `trial` | free month, never paid | yes |
| `active` | paid, `expires_at` in the future | yes |
| `past_due` | `expires_at` passed, inside grace | yes |
| `expired` | grace ended, removed | no |
| `cancelled` | user stopped renewals, access until `expires_at` | until `expires_at` |

### Enrolment

**A bot cannot list group members.** The Bot API offers only `getChatMember` (one known id),
`getChatMemberCount` and `getChatAdministrators` — there is no roster method. So enrolment is by
registration: members `/start` the bot and receive `trial` with `expires_at = now + 30 days`,
`source = import`. Anyone who never starts the bot is invisible to us and is removed when the
group is gated.

### Job 1 — `renew_due`, daily

| Condition | Action |
|---|---|
| `expires_at - 3d ≤ now < expires_at` | reminder; include a pay link if no `recToken` |
| `expires_at ≤ now` and `recToken` present | `CHARGE`. `Approved` → `expires_at += 1 period`, `active`. `Declined` → `past_due` |
| `expires_at ≤ now` and no `recToken` | `past_due`, `grace_until = now + 3d`, send `invoiceUrl` |
| `past_due`, `grace_until > now` | daily reminder |
| `past_due`, `grace_until ≤ now` | `ban_chat_member` then `unban_chat_member`, `expired`, notify |

### Job 2 — `poll_pending`, every 2 minutes

`CHECK_STATUS` on every `pending_payment` / `pending` / `error` row younger than ~30 minutes.
`Approved` → verify amount and currency against **that user's** price, store `recToken`, extend,
send a single-use invite (`create_chat_invite_link`, `member_limit=1`, short `expire_date`).

Both jobs must be idempotent: a second run in the same window changes nothing (G3.4).
Scheduling is APScheduler in-process — the plan allows this before Phase 4. Nothing is missed
across restarts because every rule is a date comparison, not a timer.

---

## 5. Schema

Beyond the plan's data model:

**`subscriptions`** — `grace_until`, `last_reminder_at` (so reminders fire once, G3.7),
`wayforpay_rec_token`, `payment_source` (`wayforpay` / `manual` / `patreon`).

**Per-user price.** Because pricing is per user, the amount cannot come from a single env var.
Store `price` and `currency` on the subscription (or on the user, as the plan's `plans` would
have), set when the subscription is created and read when building each invoice or charge.

This refines **G5.8**: "amount and currency checked against expected price" means against *that
subscription's* price, not a global constant. A test must cover a discounted user, or the check
degenerates into comparing a value to itself.

---

## 6. Gate consequences

**Changed shape.** The plan's Gate 5 assumes an inbound callback. With no public URL there is no
callback handler, so these become status-polling equivalents:

- **G5.3** duplicate callback → duplicate `CHECK_STATUS` returning `Approved` twice extends
  access exactly once (`order_reference` UNIQUE)
- **G5.6** signed accept response → not applicable; no endpoint exists to reply from
- **G5.7** store the raw callback body → store the raw `CHECK_STATUS` / `CHARGE` response body,
  searchable by `order_reference`

**Added.**

- Refund and reversal revoke access, and the user is told
- `pending` / `InProcessing` neither grants nor fails
- A per-user price is honoured by both `CREATE_INVOICE` and `CHARGE`
- Reconciliation sweep for rows stuck `pending_payment` past their timeout — the
  polling-only replacement for WayForPay's 4-day callback retry

**Unchanged.** G5.1, G5.2 (signature accept/reject), G5.4 (out-of-order), G5.5 (unknown order),
G5.13 (dashboard reconciliation), G5.14 (one real payment, then refunded), G5.15 (secrets only
in env).

---

## 7. Other funding sources

Patreon, if added, follows the same pull shape: poll the campaign-members endpoint, treat an
active pledge as paid for the period, set `payment_source = 'patreon'`. Job 1 then treats it
identically — one lifecycle, several funding sources.

---

## 8. Sources

- [Accept payment (Purchase)](https://wiki.wayforpay.com/en/view/852102)
- [Create invoice](https://wiki.wayforpay.com/en/view/608996852)
- [Check Status](https://wiki.wayforpay.com/en/view/852117)
- [Charge (host2host)](https://wiki.wayforpay.com/en/view/852194)
- [Regular payments](https://wiki.wayforpay.com/en/view/852496) — considered, not used
- [Recurrent payment status](https://wiki.wayforpay.com/en/view/852526) — considered, not used
- [Telegram Bot API](https://core.telegram.org/bots/api) — no member-enumeration method
- SendPulse, for the status model:
  [payment statuses](https://sendpulse.ua/knowledge-base/account-settings/accept-payments/payment-statuses),
  [WayForPay setup](https://sendpulse.ua/knowledge-base/account-settings/accept-payments/wayforpay)

---

## 9. The Ukrainian specification, against what is built

A Ukrainian specification was supplied on 2026-09-30. Its message copy is now in `texts.py`,
with spec-quoted strings marked `SPEC`. Where the spec's mechanism differs from what was decided
earlier, the difference is deliberate and recorded here.

| Spec says | Built | Why |
| --- | --- | --- |
| WayForPay **Webhook** delivers payment status | `CHECK_STATUS` polling | No public HTTPS on a laptop; §1 |
| WayForPay holds a **recurrent profile** and charges on schedule | We charge with `CHARGE` + `recToken` | Per-user pricing; §1. The member's experience is the same — money is taken automatically and reminders are informational |
| Unpaid member is **removed** from the channel | An admin is notified instead | Owner's decision: removal only after the payment path is proven. `TODO(removal)` marks the spot |
| Existing members are given `Active` by **import** | They claim it by `/start` | A bot cannot enumerate members; `getChatMember` then sets their tier |
| Retry the charge after a failure, then remove | Not built | Renewals themselves are not built yet |

### Spec behaviour still to build

- **Reminder one day before the charge** (`texts.RENEWAL_REMINDER`) — needs a third job.
- **Automatic renewal** via `CHARGE` + `recToken`, and the retry-after-failure that
  `texts.CHARGE_FAILED` describes. The token is already stored.
- **"Скасувати автопродовження"** (`texts.CANCEL_AUTORENEW_BUTTON`) — cancel the recurring
  token at WayForPay, keep access to `expires_at` (`texts.AUTORENEW_CANCELLED`).
- **Removal**, with `texts.ACCESS_SUSPENDED` as the notice.
- **Knowledge base** and **networking catalogue**, both gated on an active subscription. The
  spec's category names and profile-card layout are in `texts.DEFAULT_CATEGORIES` and
  `texts.PROFILE_CARD`.

### Two approved corrections to the supplied copy

Both settled by the owner on 2026-09-30, and applied:

1. `CHARGE_FAILED` now begins «Не вдалося **виконати** автоматичне списання» — the form for a
   completed attempt.
2. The hours are rendered by `texts.hours_phrase`, which agrees the noun with the numeral
   («24 години», «5 годин», «1 годину»). `texts.RETRY_AFTER_HOURS` is 24. A hard-coded form would
   have been wrong for most values, so the helper stays even though only one value is in use.

Every other `SPEC` string is verbatim. Copy changes need the owner's decision, not a developer's.
