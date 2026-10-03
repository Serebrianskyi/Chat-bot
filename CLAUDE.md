# CLAUDE.md — Club Bot

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

**Phase 1 — Skeleton, database, admin gate.** Implemented: `users` + `audit_log` models and
the first migration, `/start` registration, the `/admin` gate and menu, the global error
handler. Phases 2–7 modules are still placeholders carrying the phase that fills them.
Do not implement ahead of the current phase — if a helper has no caller yet, leave it as a
`TODO(phase-N)` instead of writing it.

Phase order: 0 Setup → 1 Skeleton/DB/admin gate → 2 Knowledge base → 3 Manual subscriptions
→ 4 Production infra → 5 WayForPay → 6 Profiles → 7 Broadcasts/stats → 8 Launch.
Phase 3 is a valid early launch point.

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

Flat at the repository root, per the plan's layout (its `club-bot/` is the repo name, not a
nesting level).

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
  - **SQLite is the working database** (`club_bot.db`, gitignored). Nothing creates tables at
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
