# Payments and subscription lifecycle: design

Decided 2026-09-29. Companion to `telegram-bot-implementation-plan.md` (Phases 3 and 5) and
`telegram-bot-quality-gates.md` (Gates 3 and 5). Where this document and those disagree, they
win and this one gets corrected.

---

## 1. Decision

> **Reversed on 2026-10-29 by §1b.** WayForPay support confirmed that card tokenisation is not
> available on this merchant, which makes merchant-driven `CHARGE` unreachable and a
> WayForPay-side schedule the only route to automatic renewal. §1 and §1a are kept as the record
> of how that was arrived at; **§1b is the design in force.**

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

## 1a. Correction, 2026-10-20, resolved 2026-10-28 — how a `recToken` is actually obtained

WayForPay support, asked directly:

> Оскільки ви приймаєте платежі за API, ви маєте передавати у запиті параметри регулярності. У
> такому випадку платіж буде вважатися регулярним… Якщо платіж було прийнято без даних
> параметрів, зробити його регулярним вже неможливо.

Checked against their documentation the same day:

- **`CREATE_INVOICE` accepts no recurring parameters at all.** Its parameter list is
  `transactionType, merchantAccount, merchantAuthType, merchantDomainName, merchantSignature,
  apiVersion, language, serviceUrl, orderReference, amount, currency, alternativeAmount,
  alternativeCurrency, orderTimeout, productName[], productPrice[], productCount[],
  paymentSystems, clientFirstName, clientLastName, clientEmail, clientPhone`. No `regularMode`,
  `regularAmount`, `dateNext` or `dateEnd`.
  ([create invoice](https://wiki.wayforpay.com/en/view/608996852))
- **A `recToken` is issued by `Purchase` (full card details entered) or `Card Verify`**
  ([tokenization](https://wiki.wayforpay.com/en/view/852175)). Paying an invoice link *is* a
  Purchase underneath, so an invoice payment does generate one — see the next point.
- **For an invoice, `recToken` arrives in the `serviceUrl` notification**, not in the
  `CREATE_INVOICE` response. The CREATE_INVOICE page documents its own `serviceUrl` callback and
  lists `recToken` among the fields it carries
  ([create invoice](https://wiki.wayforpay.com/en/view/608996852)).
- **`CHECK_STATUS` does not return it.** Settled by evidence on 2026-10-28: order
  `sub-478719815-1793107379`, a real `Approved` 10 EUR payment, came back with
  `merchantAccount, reason, reasonCode, orderReference, amount, currency, authCode, createdDate,
  processingDate, cardPan, cardType, issuerBankCountry, issuerBankName, transactionStatus,
  refundAmount, settlementDate, settlementAmount, fee, merchantSignature, baseAmount,
  baseCurrency` — and **no `recToken`**. So polling can never obtain a token, however long it
  runs. (That body also had an **empty `cardPan` on an Approved payment**, which is worth
  remembering: `refine_status` treats a `Declined` with neither `amount` nor `cardPan` as
  untouched, and this shows `cardPan` alone would be an unsafe signal.)
- A payment already taken without regularity parameters **cannot be made regular afterwards**, so
  there is no retroactive fix for the periods already sold.

### What this means for §1

§1 chose merchant-driven `CHARGE` over WayForPay-side regularity, and that choice still holds —
`CHARGE` is implemented and tested (`billing.attempt_charge`). But §1 assumed the token would
simply arrive with the first payment. It will not, on the current `CREATE_INVOICE` integration.

**Automatic renewal is blocked on one thing only: the `serviceUrl` callback**, which needs a
public HTTPS endpoint — the Phase 4 webhook. `serviceUrl` is an optional `CREATE_INVOICE`
parameter and `web/routes.py` already reserves `POST /payments/wayforpay` for it.

**Members do not have to pay differently.** `CREATE_INVOICE` stays; it gains `serviceUrl`. The
earlier worry — that tokenization would force the `Purchase` flow and change the member's
experience — is contradicted by the CREATE_INVOICE page documenting `recToken` on its own
callback.

Two details of the callback, both easy to get wrong:

- **It must be answered**, or WayForPay retries. The reply is
  `{orderReference, status: "accept", time, signature}` with
  `signature = HMAC_MD5("orderReference;status;time", secret)` — a **different** field list from
  the response signature verified elsewhere in this document.
- **The incoming signature covers** `merchantAccount;orderReference;amount;currency;authCode;
  cardPan;transactionStatus;reasonCode`, which is exactly `RESPONSE_SIGNATURE_FIELDS` already in
  `services/wayforpay.py`, so `verify_response` works on it unchanged.

`CHARGE` also requires `merchantTransactionType` and `merchantTransactionSecureType`, both
mandatory and both absent from the first implementation — corrected the same day, `SALE` and
`NON3DS` (nobody is present at a renewal to answer a 3-D Secure challenge).

---

## 1b. Reversal, 2026-10-29 — renewal runs on a WayForPay-side schedule

WayForPay support, asked whether tokenisation could be enabled:

> Наразі робота з токенами недоступна. Для щомісячних підписок Ви можете використовувати
> регулярні платежі. Регулярні платежі доступні при сплаті клієнтом на платіжній сторінці методом
> Purchase з передачею параметрів регулярного платежу. (wiki 852102)

So the token §1a was waiting for is never coming, and `attempt_charge` — correct, tested code —
can never fire on this merchant. The owner's decision the same day: **rebuild the payment flow on
`Purchase` and stop waiting.**

### What changes

**The payment link.** `CREATE_INVOICE`'s documented parameter list (wiki 608996852) contains no
recurring fields at all — verified field by field — so recurring is unreachable from the invoice
flow. A payment link that should renew is now a **Purchase**: `POST
secure.wayforpay.com/pay?behavior=offline`, which answers `{"url": ...}` — the only shape usable
from a bot, since a Telegram button cannot POST a form. Confirmed against the live endpoint on
2026-10-29: JSON body accepted, EUR accepted, recurring parameters accepted.

Fields added over an invoice: `regularMode=monthly`, `regularOn=1`, `regularBehavior=preset`,
`regularAmount`, `dateNext`. **None of them are signed** — the signature is byte-identical to
`CREATE_INVOICE`'s, same nine fields in the same order, so `invoice_signature_fields` is reused
unchanged. `merchantTransactionSecureType` is `AUTO`, not the `NON3DS` that `CHARGE` uses: there
*is* a cardholder on this page, and 3DS must be allowed to run.

**`dateNext` is the end of the period being bought**, not the expiry the member currently holds.
The gateway refuses a date that is not in the future, and 151 of 172 subscriptions are past due.
It is computed with the same `extend()` the payment itself applies, so the gateway's schedule and
`expires_at` agree by construction rather than by coincidence.

**Who charges whom.** One payment by the member settles the period *and* registers the schedule.
Every month after, WayForPay charges the card and POSTs the result to `serviceUrl`. Consequently:

- a member with an active schedule is **skipped** by the daily due-date sweep — chasing them would
  hand out a second payment page and take the money twice — but only for two days past the due
  date. A declined card leaves the schedule `active`, because the gateway may retry and cancelling
  what it might still collect would be worse; skipping on that status alone would mean the member
  is never sent a link, lapses, and is removed by `remove_lapsed_members` having never been asked
  to pay. Past the window they are chased by hand with the schedule left in place, so a charge that
  does land later simply extends them again;
- `renews_automatically()` replaces the bare `recToken` check, so the reminder's «Завтра
  відбудеться автоматичне списання» is finally true for somebody;
- a renewal sends **no** invite — unless the member is outside the channel, which happens when a
  card declines long enough for `remove_lapsed_members` to take them out and then starts working
  again. Without that check they are charged, extended and left outside what they just paid for;
  an unreadable membership answer is treated as outside, since a spare link costs nothing beside
  that;
- **removal stops the schedule before showing anybody the door.** Same reasoning in reverse: a
  retry that succeeds after a removal takes money for a channel they are no longer in;
- `CHECK_STATUS` and the reconcile sweep ask with `payments.gateway_order_reference`, because our
  reference for a scheduled charge is one WayForPay has never heard of — polling with it would
  leave the row at ERROR for ever.

**A schedule is never created without a `serviceUrl`.** The client raises instead. A schedule whose
charges nothing is listening for is money taken with no access granted, monthly and silently —
the 77-order failure of 2026-10-09, except permanent. `AUTO_RENEW_ENABLED` is therefore ignored
unless `MODE=webhook` with a `BASE_URL`.

### The one thing the documentation does not say

**Under which `orderReference` a monthly charge is reported.** The wiki does not state it, support
was asked, and the owner chose not to wait. Both possibilities are handled and the branch taken is
logged at warning level, so the first real charge settles it from one log line:

| Case | How it is recognised | What happens |
|---|---|---|
| Same reference as the original | row exists, already `complete`, **different `charge_key`** | a new `payments` row, gateway's string in `gateway_order_reference` |
| A reference of the gateway's own | no row; resolved via `subscriptions.regular_order_reference`, then a prefix match, then the `sub-<id>-<ts>` shape **but only for a member who has a live schedule** — every order ever written has that shape, so matching on it alone would turn a late notification about an old one-off order into a free period | a new `payments` row |
| A repeat of a charge already applied | same `charge_key` | nothing, and no second period (rule 5) |
| Unplaceable | none of the above | acknowledged, and an admin is told |

`charge_key` is the gateway's `authCode` — unique per approved charge — or a digest of the varying
fields for a refusal, which carries no `authCode`. Without it, month two under the original
reference reads as a duplicate of month one: `payments.order_reference` is UNIQUE so no second row
can be written, and `apply_payment_result` ignores a result for an already-`complete` row. The
money would arrive and nobody's access would be extended.

### Amount checking changes shape for a scheduled charge

Rule 8 verifies the amount against *that user's* price, because on a member-initiated payment a
mismatch means a tampered body. A scheduled charge is different: the gateway holds its own copy of
`regularAmount`, fixed when the schedule was created, while this club computes a price per invoice
— so a discount ending or a promo running out makes the two disagree **legitimately**. The body is
signature-verified either way, so the money is real. The period is granted and an admin is told the
two copies have parted company; the fix is a `regularApi` EDIT, which is a person's decision.

### Cancellation inverts

«Скасувати автопродовження» must stop the schedule **at WayForPay before** anything local changes,
because the charges are not ours to stop. `regularApi` (`https://api.wayforpay.com/regularApi`)
authenticates with `merchantPassword`, a 32-character credential WayForPay issues on request
(sales@wayforpay.com) — established empirically on 2026-10-29: the secret key is refused for
length and its MD5 answers *"Can`t authorize merchant with Password"*.

**Until that credential exists, a schedule cannot be cancelled from the bot.** `stop_schedule`
returns False, the member is told their request is being processed — never that it is done — and
an admin is sent the order reference to remove in the dashboard. A card charged after «скасовано»
is the worst trust failure available here, which is why no local flag is flipped on a failure.

### `CREATE_INVOICE` is gone entirely (owner's decision, 2026-10-29)

Not kept for one-offs. Purchase does both: omit the recurring fields and it is a single payment,
which is what a member who already has a schedule gets (a second schedule would charge them twice
a month) and what an admin-issued one-off is. Everything the invoice call offered —
`orderTimeout`, `clientEmail`, `serviceUrl` — Purchase takes too, so nothing was traded away.

Keeping it would have left two ways of asking for money where only one renews, and the failure
that produces is silent: a member pays a link that never repeats, and nobody notices until their
access lapses a month later.

A 100% discount still creates no gateway order at all — nothing to pay means nothing to ask for.

### What stays
`CHECK_STATUS` stays as the confirmation fallback for a callback that never arrives.
`attempt_charge` and `recToken` stay in place, unreachable but correct, in case tokenisation is
ever enabled — and `renews_automatically()` keeps a token-holder out of the chasing path so a
token appearing later cannot double-charge anybody.

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
