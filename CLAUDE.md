# CLAUDE.md — ChatBot

Paid Telegram community bot: private-channel access, WayForPay recurring subscriptions,
knowledge base, networking profiles, admin panel.

## Authoritative documents

Two documents govern this project. They are the source of truth; this file is the
enforceable distillation of them. Read the relevant phase section before touching code.

- `docs/telegram-bot-implementation-plan.md` — **what** to build, in which phase, with the
  target architecture, stack, repository layout and data model.
- `docs/telegram-bot-quality-gates.md` — **how you prove** a phase is done: standing gate
  S1–S11, per-phase gates G0–G8, verification types, CI pipeline, gate record template.
- `docs/telegram-bot-payments-design.md` — decided design for payments, plus (section 9) how
  the Ukrainian specification of 2026-09-30 maps onto what is built. Covers Phases 3 and 5: one invoice per
  user per period, no WayForPay-side subscriptions, per-user pricing, the status model, the two
  jobs, and which Gate 5 items change shape because there is no callback endpoint.

If this file and those documents ever disagree, the documents win — and this file must be
corrected in the same change.

## The one rule that shapes everything

**Build in thin vertical slices.** Each phase ends with something that works end to end.
Do not start phase N+1 until phase N's gate passes and is recorded in `docs/gates/phase-N.md`.
A failing gate item is fixed and the whole gate re-run — never carried forward, least of all
into the payment phase.

## Current status

Last updated 2026-10-06. **Working and pushed** (`Serebrianskyi/Chat-bot`, branch `main`),
deployed on Railway. 40 tests, 7 migrations.

### Built

| Area | What works |
| --- | --- |
| Onboarding | `/start` registers a member, resolves their price, writes a due date, and invoices on the spot. Two messages: the club pitch, then the tariff carrying the **Стати учасником** pay button |
| Pricing | 10 EUR base. Three tiers: discounted (list or admin grant), already-in-the-channel, new joiner |
| Free period | Ends on `FREE_PERIOD_UNTIL` — one shared date, not 30 days per person. Set to 2026-11-01 Kyiv. After it, nobody gets one |
| Founding members | 17 usernames seeded by migration with their 8/10 EUR price and a free first period. Idempotent across deploys |
| Discounts | Percent or fixed price, optional expiry, soft revocation. The charged amount is computed **per invoice**, so a time-limited discount actually ends |
| WayForPay | `CREATE_INVOICE`, then `CHECK_STATUS` polled every 2 min. Signature verified both ways, amount checked against the invoice, idempotent on a repeated `Approved` |
| Channel invite | Single-use, 3-day link on a confirmed payment. Every failure path tells the member something true and alerts an admin. An admin can also send one by hand from the panel, for a member the automatic path missed |
| Due dates | Daily job at 09:00 Kyiv invoices whoever has come due, then alerts an admin if still unpaid |
| Recovery report | After the daily sweep, admins get **one** message: who had a payment recovered, who was sent a link, and who still needs a person — each with a tap-to-copy id and the reason. Built from `audit_log`, so it reports what was recorded, not what was attempted. A failed step is named. Nothing to report means no message |
| Invite recovery | Daily at **11:00 Warsaw** (12:00 Kyiv), as part of the recovery sweep: finds paid members who are not in the channel and sends one retry. If that does not get them in, an admin is told directly — handle, id, name, reason — and the member is not touched again. Scope is everyone holding paid access, **plus** anyone with a confirmed payment who has never been sent a link at all, even if their period has lapsed — otherwise a payer whose invite failed drops out of scope when their month runs out. A free trial is **not** a payment: TRIAL subscriptions are never messaged by it |
| Reconciliation | Re-asks WayForPay about orders this bot wrote off and credits the ones really paid, with their invites. Runs daily at 11:00 Warsaw inside `recover_access`, and by hand as `run_jobs reconcile` (a dry run until `--apply`) or `run_jobs recover` (the whole sweep, forced past the daily guard). Only members who currently have **no** access are in scope, so a hand-written credit cannot be doubled, and one member is credited at most once per sweep |
| Admin panel | 🎟 Знижки · 🎁 Надати знижку · 👥 Учасники · 🔗 Надіслати запрошення · ✍️ Написати учаснику (admin dictates, the bot delivers — the only way to reach a member with no username) · 📣 Розсилка · 📢 Написати в канал. The other four answer "later". 👥 Учасники opens on counts with a button per group — 🔄 автопродовження · ⏹ скасували автопродовження · 🎁 пробний період · ⏳ очікують оплати · ♾ безстрокові · ❓ без підписки — so it stays one message as the club grows; tapping a group lists only that group, naming people without a username by their first name |
| Member area | `/subscription` with status, next amount and date; **Скасувати автопродовження** keeps the paid period |
| Copy | All Ukrainian, all in `texts.py`. Owner-supplied strings marked `SPEC` |
| Broadcast | 📣 Розсилка: pick a group (the same groups 👥 Учасники uses) → write the text → choose the price the link will charge (💰 звичайна, or 🎟 спеціальна: a sum like `8` or a percentage like `20%`, lasting 1/2/3/6/12 months by button, any number of months up to 60 by typing it, or without limit — months, because the club bills monthly, converted to `months × period_days` so a price always covers whole billing periods) → **see it rendered exactly as it will arrive — including the pay message as its own second message, button in place** → confirm. A special price is applied by granting each recipient a real `Discount`, so it flows through `effective_price` like every other price, shows up in 🎟 Знижки and is revocable — and because the rule is one active discount per person, the preview says how many existing discounts it would replace. For ⏳ Очікують оплати each recipient also gets a second message with its own **Долучитися до Клубу** button and their own live invoice — worded differently from the `/start` button on purpose, since the text alone gives them no way to act. Held to 20 messages/second, honours `RetryAfter` without skipping or duplicating anybody (rule 10). One `audit_log` row per broadcast, carrying the audience, the text and the photo `file_id`. A message may be text, or a photo whose caption is the text — one message per send; collecting several messages into one broadcast is **parked** (see below). ♾ Безстрокові takes the identical flow — price step, real invoice, real button — because that group is the owner's own account and is therefore how a campaign gets rehearsed before members see it |
| Channel posts | 📢 Написати в канал: the bot publishes an admin's text in the private channel, with the same preview-then-confirm step, so the club can speak there as well as read |
| Operator tools | `scripts/discounts.py`, `scripts/run_jobs.py` (`status` is read-only), `scripts/diagnose.py` (read-only: `access` tells a bot failure from a member who never used their link, `reasons` groups what WayForPay actually said), `scripts/start.sh` |

### Live configuration

Bot `@yourstoryclub_bot`; channel `Create Your Story | Club`, id `-1003980549671`, bot is
administrator with *Invite users via link* and *Ban users*. Admins: `158032815`, `386701736`.

`@makaolya` (`158032815`) holds a **lifetime subscription**: `expires_at` is 2100-01-01, so no job
invoices them and no screen shows them a due date. Granted by migration `c3a1f0d27b94`, marked
`source='lifetime'`. A sentinel date rather than a nullable column, because every job and screen
is a comparison against `expires_at`.

### Parked mid-change

**Composing a broadcast from several messages.** The ask: an admin sends the photo and the words
as separate messages rather than one. The groundwork was started and then reverted on request, so
the tree is consistent at one-message-per-step; `handlers/compose.py` carries the shape it would
take. What it needs: a collecting state that appends each message to a `parts` list in FSM data
until a «✅ Готово» button is tapped, `replay()` to preview the parts in order, and
`send_broadcast` taking `parts` instead of `text`/`photo` — with the rate limit counting every
part, not every recipient. Albums (several photos sent as one group) are a separate problem: they
arrive as separate updates sharing a `media_group_id` and need batching middleware plus
`send_media_group`.

### Not built — in the order I would do it

1. **Automatic renewals** (`CHARGE` with the stored `recToken`). **This is the most important gap,
   because the copy already promises it**: `PAYMENT_FIRST_CONFIRMED` tells a paying member
   «оплата автоматична». Today a renewal requires them to tap a link again, so the bot is saying
   something that is not yet true. The token is captured; the charging is not written.
2. **Removal of non-payers.** Five `TODO(removal)` markers mark the exact spot. Deliberately
   deferred until the payment path has been exercised with real money; an admin is alerted instead.
3. **Renewal reminder**, one day before the charge. Copy exists (`RENEWAL_REMINDER`), no job.
4. **Knowledge base** (plan Phase 2) — 10 TODOs; category names already in `texts.py`.
5. **Networking catalogue** (plan Phase 6) — 6 TODOs; card layout already in `texts.py`.
6. **Statistics** (plan Phase 7). Broadcasts are built — see the table above; statistics are not.
7. **Webhooks, Sentry, uptime monitoring, backups** (plan Phase 4) — 7 TODOs. Polling is in use;
   a webhook needs a public HTTPS endpoint.

### Real money: one payment taken, confirmation never delivered

`@darriashine` paid on **2026-10-06** — the first real payment, *per the owner*: there is no
gateway record of it on this side, which is the whole problem. The confirmation never reached
them: `poll_open_payments` was crashing on every run (WayForPay answered CHECK_STATUS with a blank
`amount`, and `Decimal("")` raised out of the job before it could commit), so their invoice timed
out as unpaid while the money had in fact been taken. The poller is fixed; their period was
credited by migration `d7e4b2c91a35`.

Two things are still open from it:

- **The channel invite is owed by hand.** `deliver_invite` only ever runs from a confirmed payment
  inside the poller, so no migration can send it.
- **No `payments` row is marked settled for them.** The order they paid cannot be identified from
  the database, so nothing was guessed at. Settle the exact row once the order reference is read
  off the WayForPay dashboard.

A second, larger fault was found in the logs of **2026-10-09** and fixed the same day: one poll run
checked 77 invoices a minute old and WayForPay answered `Declined` with a blank `amount` for every
one. `DENIED` is terminal, so each order left the poll query about a minute after being issued —
while its payment link stayed valid for two hours. Anyone who paid after that first poll was never
asked about again: money taken, no invite. `refine_status` now demotes a `Declined` carrying
neither an `amount` nor a `cardPan` back to `pending_payment`, so the order stays pollable until
the invoice genuinely times out. A refusal that does carry card detail is still terminal.

Run `python -m scripts.diagnose reasons` against production to confirm the cause from the stored
bodies, and `access --channel` to list who is still outside the channel.

The daily sweep is `recover_access`, at **11:00 Europe/Warsaw** — the owner's own clock, because its only output is a report an admin reads, while everything member-facing stays on `display_timezone` (Kyiv). Scheduled as a zone, not an offset, so it stays 11:00 local through both countries' daylight-saving changes. **Not** at startup, which is where it began.
Ten deployments in an afternoon meant ten sweeps and ten admin reports about the same people, so
`RECOVERY_MIN_INTERVAL` (20 h) now refuses to repeat however the sweep is triggered; an
`access.recovered` audit row is the marker, written whatever the outcome so a failed sweep cannot
be retried minutes later by a redeploy either. `run_jobs recover` passes `force=True` to mean it.

The `refine_status` fix is **forward-only**: the orders already written off are terminal and the
poller will never look at them again. Recovering them is what `run_jobs reconcile` is for, and getting a link to
whoever turns out to have paid is what the 10:00 invite-recovery job and the 🔗 admin screen are
for. The owner's standing instruction of 2026-10-09: **members who have not paid are not to be
messaged yet** — a discount offer is planned for them instead — which is why both the sweep and
the retry job are scoped to paid access only.

So confirmation → invite has still never completed end to end against real money. That remains the
single most valuable thing left to try, and the fixes above are what make it worth trying again.

### Phase numbering

The plan's order was changed on 2026-09-30: the knowledge base (its Phase 2) is deferred, and
**Phase 2A** — onboarding plus the start of the subscription mechanism — was built instead,
drawing items from the plan's Phases 3 and 5. `docs/phase-2a-scope.md` holds its item list;
`docs/gates/README.md` tracks every gate.

## Non-negotiable rules for every change

These are the standing gate plus the invariants the plan's risk table depends on.

1. **UTC everywhere.** All timestamps stored in UTC. Convert to local time (Europe/Kyiv) for
   display only. Time-zone bugs in expiry mean early or late kicks.
2. **Audit every admin action.** Every data-changing admin action writes an `audit_log` row
   with actor, action, target and details. Tested per action (S6).
3. **Secrets only in env vars.** Never commit `.env`. Never log a token, payment key or
   signature. `config.py` validates required vars at startup and fails fast with a clear
   message naming the missing var (S5).
   - **`.env` currently holds LIVE production credentials** for `@yourstoryclub_bot` — there is
     no separate test bot. Treat every local run as touching production: real users message
     this bot, and local polling intercepts their messages. Mode `600`, gitignored.
   - `.githooks/pre-commit` blocks committing a `.env` or a token-shaped string. Enable it with
     `git config core.hooksPath .githooks` immediately after `git init`, before the first
     commit.
   - Never echo `settings.bot_token` into a log, an error message, or a test fixture.
4. **One process per bot token.** Never poll the production token locally. Development uses
   the separate test bot; production uses webhooks. Two processes on one token produce
   erratic behaviour.
5. **Idempotent money and expiry paths.** A repeated `Approved` for the same
   `order_reference` must extend access exactly once (`order_reference` is UNIQUE). Running
   the expiry or renewal job twice must change nothing the second time.
6. **Store raw payment responses.** Every `CHECK_STATUS` / `CHARGE` response body is persisted
   and searchable by `order_reference`, before any business logic runs.
7. **Only `complete` grants access.** `InProcessing` neither grants nor fails — keep polling.
   `refunded` and `reversed` revoke. Verify `amount` and `currency` against *that user's* price,
   since pricing is per user. See `docs/telegram-bot-payments-design.md`.
8. **Migrations accompany schema changes.** Any change to `db/models.py` ships with an Alembic
   migration that upgrades from the previous head (S7).
9. **No silent failures.** A global error handler gives the user a friendly message; the
   exception goes to Sentry. Silence is a gate failure (S8).
10. **Respect Telegram rate limits.** Broadcasts stay at or under 20 messages/second and honour
   `RetryAfter` without skipping or duplicating recipients.
11. **Tests are deliberately few.** One file, `tests/test_core.py`, capped at about **30
    tests**. Owner's decision on 2026-10-02: unit testing was costing more than it caught.
    - Add a test only for a path where a bug costs **money, access or trust**: signature
      construction, idempotent payment handling, the admin gate, discount arithmetic and
      expiry, degradation when Telegram or WayForPay fails.
    - Do **not** test wording, menu layout, or every branch of every message. Those are visible
      the moment the bot runs. The manual checks in `docs/gates/` cover them.
    - When a bug is found, prefer *fixing* it plus one test that would have caught it, over a
      new family of tests around it.
    - Never let the suite touch the network. The WayForPay client is injected, so tests get a
      fake; a real one built from test credentials once made the suite call the live gateway.
12. **README and `.env.example` updated** whenever run instructions, env vars or commands
    change (S10).
13. **The bot speaks Ukrainian, and every string lives in `texts.py`.** Never write member-facing
    copy inline in a handler or service. Strings marked `SPEC` are the client's own wording —
    quote them exactly and do not "improve" them; raise a copy question instead. Sums are
    rendered by `texts.money` («300 грн»), dates by `texts.day` («31.12.2026»).

## Definition of done for any change

Before calling a change complete, the standing gate must hold:

```
ruff check .
ruff format --check .
pytest
```

plus: new `services/` logic has unit tests · admin actions write `audit_log` ·
Alembic migration present if models changed · tried on the **test** bot from a phone (S9) ·
README / `.env.example` updated if needed.

`.github/pull_request_template.md` carries this as a checklist.

## Layout

Flat at the repository root. The plan's layout shows a `club-bot/` folder; that is a repo name,
not a nesting level. This repo is **ChatBot** (`Serebrianskyi/Chat-bot`).

```
main.py        entry point: polling (dev, Phases 0–3) or FastAPI app (prod, Phase 4+)
config.py      env vars, validated at startup
handlers/      start, admin, errors, materials, profiles, payments — aiogram routers
middlewares/   session.py: one DB session per update, injected into handlers
services/      users, audit, pricing, subscriptions, wayforpay, billing, broadcast, stats
web/           routes.py: /telegram/webhook, /payments/wayforpay, /jobs/*, /health
db/            models.py, session.py
alembic/       migrations
tests/         unit + integration
docs/gates/    one record per passed gate
```

The plan names `services/payments.py` for the WayForPay client; it is `services/wayforpay.py`
here, so a second provider (Patreon) slots in beside it rather than inside it, with
`services/billing.py` orchestrating whichever is in play. `middlewares/`, `handlers/errors.py`,
`services/users.py`, `services/audit.py` and `services/pricing.py` are further additions
to the layout printed in the plan. The plan's list is illustrative, not exhaustive: the session
middleware and error handler are dispatcher concerns with nowhere else to live, and the two
services exist because handlers must stay thin.

Business logic belongs in `services/` so it can be unit-tested without Telegram. Handlers stay
thin: parse the update, call a service, render the reply.

**Routers come from `build_router()` factories, not module-level singletons.** An aiogram
`Router` can only attach to one dispatcher, so a module-level instance makes a second
dispatcher impossible in the same process — which every test needs.

**Router order is access control.** The gated admin router is registered before the denied
router; an update the `IsAdmin` filter rejects falls through to the denial. Reversing them
would deny everyone.

**`ADMIN_IDS` is the only authority on who is an admin.** The `users.role` column is a mirror
for display. Never make the database authoritative — that would put privilege escalation one
`UPDATE` away.

**Timestamps use `db.models.UtcDateTime`, never bare `DateTime`.** It refuses naive values on
write and re-attaches UTC on read, so SQLite and PostgreSQL behave identically. Read the clock
through `db.models.utcnow()`.

## Environment

- **Python 3.14** (`.venv/`). The plan specifies 3.12; 3.14 is what this machine has and the
  full stack (aiogram 3.31, FastAPI, SQLAlchemy 2.1, Alembic) resolves on it with native
  wheels. Local and CI both pin 3.14 — keep them identical.
- Database: SQLite locally → PostgreSQL from Phase 4.
- Dependencies pinned in `requirements.txt`; ruff and pytest configured in `pyproject.toml`.

## Approved deviations from the plan

- **Python 3.14** instead of 3.12 — see Environment above.
- **No deployment: this laptop is the test server.** The owner decided on 2026-09-28 to run
  the bot locally from PyCharm rather than deploy to Railway/Render. Consequences to respect:
  - **Polling, not webhooks.** There is no public HTTPS URL. `MODE=webhook` and `web/routes.py`
    stay Phase 4 work; do not wire them up as a workaround.
  - **SQLite is the working database** (`chatbot.db`, gitignored). Nothing creates tables at
    startup, so `alembic upgrade head` must be run after any schema change — the
    **1 Migrate** run configuration exists for this. PostgreSQL is exercised only by CI.
  - **No Docker.** Files for a containerised run were written and then removed on request
    after Docker Desktop here could not pull the base images. Do not reintroduce them without
    being asked.
  - **This is not Phase 4.** Staging/prod split, secret-protected endpoints, `/health`, Sentry,
    UptimeRobot, and the restore and rollback drills all remain open.
  - Run configurations live in `.idea/runConfigurations/` (gitignored with the rest of
    `.idea/`, so they are local to this machine).
- **`CHANNEL_ID` is optional until Phase 3.** No Phase 1 code touches the channel, so
  refusing to start without it would block the smoke test for features that do not need one.
  It is still format-checked when present, and G0.6 (record it) stays open.

## Deliberately out of scope

Do not build these without an explicit decision to revisit: web admin dashboard, a VPS or any
remote host, Redis or a task queue, multi-language UI.
