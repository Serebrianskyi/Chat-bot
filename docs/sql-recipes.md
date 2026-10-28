# SQL recipes for the production database

Hand-written SQL for the Railway Postgres. Everything here is **ANSI** and tested against SQLite
locally unless a line says otherwise; `GREATEST` and `INTERVAL` are Postgres and are marked.

## Before you run anything

- **Timestamps are UTC.** The bot converts to Europe/Kyiv for display (`texts.day`). So
  «діє до 01.11.2026» is stored as `2026-10-31 22:00:00+00:00`, and a value of
  `2026-11-01 00:00` would display as **01.11** only because 02:00 Kyiv is still that day.
  Getting this wrong moves somebody's access by a day.
- **Enum values are lowercase strings.** `'past_due'`, not `'PAST_DUE'` — comparing against the
  wrong case matches nothing and raises no error.
- **`canceled` vs `cancelled`:** `payments.status` uses one `l`, `subscriptions.status` uses two.
  An easy way to get a silently empty result.
- **Nothing here writes an audit row.** Every path in the bot does; SQL does not. Prefer the
  admin panel where one exists — 🎁 Подарувати місяць, 🎟 Знижки — and keep SQL for what has no
  screen yet.

| What you want | Prefer |
|---|---|
| give somebody a free month | 🎁 Подарувати місяць (audited, tells them, sends the link) |
| change somebody's price | 🎁 Надати знижку |
| see who is in what state | `python -m scripts.diagnose access` |
| what the gateway said | `python -m scripts.diagnose order <ref>` / `reasons` / `fields` |

---

## Looking

### One member, everything about their subscription

```sql
SELECT s.id, s.user_id, u.username, u.first_name, s.status,
       s.price, s.currency, s.price_tier, s.period_days,
       s.started_at, s.expires_at, s.grace_until, s.free_period_granted,
       s.wayforpay_rec_token, s.source
FROM subscriptions s
LEFT JOIN users u ON u.telegram_id = s.user_id
WHERE s.user_id = 1687532834;
```

### Find a member when you only know part of the id

```sql
SELECT s.id, s.user_id, u.username, s.status, s.expires_at
FROM subscriptions s
LEFT JOIN users u ON u.telegram_id = s.user_id
WHERE CAST(s.user_id AS TEXT) LIKE '%1687%'
ORDER BY s.id;
```

`subscriptions.id` is a small serial (1, 2, 3 …), so searching *it* for a long number matches
nothing — it is `user_id` that holds the Telegram id.

### Who has a stored card (so renewals can actually charge them)

```sql
SELECT user_id, status, expires_at, wayforpay_rec_token
FROM subscriptions
WHERE wayforpay_rec_token IS NOT NULL;
```

Empty is the expected answer today — see `telegram-bot-payments-design.md` §1a.

### Free-period members whose end date is not 1 November

The symptom of `FREE_PERIOD_UNTIL` having been unset when they joined: each got 30 days from
their own `/start` instead of the one shared date.

```sql
SELECT s.user_id, u.username, s.status, s.expires_at
FROM subscriptions s
LEFT JOIN users u ON u.telegram_id = s.user_id
WHERE s.free_period_granted = true
  AND s.expires_at <> '2026-10-31 22:00:00+00:00'
ORDER BY s.expires_at;
```

### Payments that are not denied

```sql
SELECT p.order_reference, p.status, p.wayforpay_status, p.reason_code,
       p.amount, p.currency, u.username, p.created_at
FROM payments p
JOIN users u ON u.telegram_id = p.user_id
WHERE p.status <> 'denied'
ORDER BY p.created_at DESC;
```

### Members whose every payment was denied

The shape left behind by the `Declined`-with-no-detail write-off bug.

```sql
SELECT s.user_id, u.username, s.status, s.expires_at
FROM subscriptions s
JOIN users u ON u.telegram_id = s.user_id
WHERE EXISTS (SELECT 1 FROM payments p WHERE p.subscription_id = s.id)
  AND NOT EXISTS (
      SELECT 1 FROM payments p
      WHERE p.subscription_id = s.id AND p.status <> 'denied'
  );
```

---

## Changing

### Set an exact end date

The instant to use is **the last second of that day in Kyiv**, so the member keeps the whole day
and the bot displays the date you meant:

| You want them to see | Store |
|---|---|
| 01.11.2026 (free period ends) | `2026-10-31 22:00:00+00:00` |
| 18.11.2026 (access through all of it) | `2026-11-18 21:59:59+00:00` |
| 30.11.2026 | `2026-11-30 21:59:59+00:00` |

```sql
UPDATE subscriptions
SET expires_at = '2026-11-18 21:59:59+00:00'
WHERE user_id = 1687532834;
```

Kyiv is UTC+2 from the last Sunday in October until the last Sunday in March, and UTC+3 the rest
of the year — so a summer date needs `21:59:59` minus one more hour (`20:59:59`).

### Put a free-period member back on 1 November

```sql
UPDATE subscriptions
SET expires_at = '2026-10-31 22:00:00+00:00',
    status = 'trial',
    free_period_granted = true,
    grace_until = NULL, last_reminder_at = NULL, admin_notified_at = NULL
WHERE user_id = <telegram_id>;
```

### Gift a month by hand — Postgres only

Prefer 🎁 Подарувати місяць, which does this *and* audits it *and* tells the member *and* sends
the channel link. If you must do it in SQL:

```sql
UPDATE subscriptions
SET expires_at = GREATEST(expires_at, now()) + INTERVAL '30 days',
    status = 'active',
    grace_until = NULL, last_reminder_at = NULL, admin_notified_at = NULL
WHERE user_id = <telegram_id>;
```

`GREATEST(expires_at, now())` is the important part — it is the arithmetic a real payment uses
(`services/pricing.extend`), so the month is **added**. Setting an absolute date instead silently
takes away weeks somebody has already paid for.

### Stop chasing somebody for a payment they do not owe

```sql
UPDATE payments SET status = 'canceled'
WHERE user_id = <telegram_id>
  AND status IN ('pending_payment', 'error');
```

Leave `'pending'` alone unless you have credited them by hand: that is money in flight at the
gateway, and writing it off hides a real settlement.

### Forget a stored card

```sql
UPDATE subscriptions SET wayforpay_rec_token = NULL WHERE user_id = <telegram_id>;
```

Nothing can be charged afterwards. The member goes back to paying by link.

---

## Running it

Railway's Postgres service has a **Data** tab with a query editor — that is the simplest route.
Otherwise take the connection string from the service's variables and use any client.

The read-only `scripts/diagnose.py` reports answer most questions without SQL at all, and they
mask the payment signature (standing gate S5) which a raw `SELECT *` on `payments.raw_response`
would not.
