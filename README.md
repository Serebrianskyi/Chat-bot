# ChatBot

A paid Telegram community bot: private-channel access, WayForPay recurring subscriptions,
a knowledge base, networking profiles and an admin panel.

**Status: deployed on Railway, taking payments.** `/start` registers a member, prices them,
invoices them on the spot, and confirms the payment by polling `CHECK_STATUS`. A paid member gets
a single-use channel invite. An overdue member is reported to an admin — **not** removed; removal
is deliberately deferred until the payment path has been proven with real money.

**Biggest gap:** automatic renewals are not built, and the payment confirmation already tells
members «оплата автоматична». Until `CHARGE` is wired up, a renewal needs them to tap a link again.

`CLAUDE.md` holds the full done/next list. Scope and gate: `docs/phase-2a-scope.md`.
Mechanics: `docs/telegram-bot-payments-design.md`.

## The three documents that govern this project

| Document | Role |
| --- | --- |
| [`docs/telegram-bot-implementation-plan.md`](docs/telegram-bot-implementation-plan.md) | **What** to build: architecture, stack, data model, phases 0–8 |
| [`docs/telegram-bot-quality-gates.md`](docs/telegram-bot-quality-gates.md) | **How you prove** a phase is done: standing gate S1–S11, gates G0–G8 |
| [`CLAUDE.md`](CLAUDE.md) | The enforceable distillation of both — the rules every change must hold |

Read the relevant phase section before writing code. The plan and gates documents are
authoritative; if `CLAUDE.md` disagrees with them, they win and `CLAUDE.md` gets corrected.

The governing rule: **build in thin vertical slices.** Each phase ends with something that
works end to end, and you don't start phase N+1 until phase N's gate passes and is recorded in
[`docs/gates/`](docs/gates/).

## Setup

The bot runs on this laptop from PyCharm — there is no deployment. See
[Running it from PyCharm](#running-it-from-pycharm) if you would rather skip the command line.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt     # requirements.txt alone for production
cp .env.example .env                    # then fill it in
```

The bot is [@yourstoryclub_bot](https://t.me/yourstoryclub_bot) and its token is already in
`.env`. The plan calls for a second, test bot; the owner chose to run the live one instead, so
**every local run touches production** — real members message this bot. Only one process may hold
a token at a time, so stop anything else that is polling before you start it.

Python 3.14 is required. `.idea/` is gitignored, so your PyCharm settings stay local; the
whole toolchain is driven by `pyproject.toml` and works the same from the command line.

## Run

```bash
python main.py      # polling mode (development, phases 0–3)
```

From PyCharm, use the **2 Bot (polling)** run configuration — see
[Running it from PyCharm](#running-it-from-pycharm) for the full first-run walkthrough.

With a filled `.env` this connects to Telegram and answers `/start` and `/admin`. With
required variables missing it raises `ConfigError` naming them, which is standing gate S5.

Webhook mode (`MODE=webhook`, serving `web/routes.py` under uvicorn) arrives in Phase 4 — until
then it raises `NotImplementedError` by design.

### What works today

| Command | Who | Result |
| --- | --- | --- |
| `/start` | anyone | Registers the user, prices them, creates a subscription and a due date |
| `/admin` | an id in `ADMIN_IDS` | The admin panel: Statistics, Broadcast, Users, Knowledge Base, Add subscriber |
| `/admin` | anyone else | "You don't have access." |
| a menu tap | admin | "<item> arrives in a later phase." — the buttons are placeholders |
| a forged admin callback | anyone else | Refused, not silently ignored |

Anything that raises inside a handler is logged with its traceback and the user gets a short
apology rather than silence.

## Running it from PyCharm

This is the primary way to run the bot: on this laptop, from the IDE, against SQLite. There is
no deployment — the laptop is the test server.

Four run configurations ship in `.idea/runConfigurations/`, so they appear in the Run dropdown:

| Configuration | What it does | When |
| --- | --- | --- |
| **1 Migrate (alembic upgrade head)** | Applies migrations to `chatbot.db` | Once now, then after any `db/models.py` change |
| **2 Bot (polling)** | Runs `main.py` | To start the bot |
| **3 Tests (pytest)** | Runs the suite | Before calling anything done |
| **4 Lint (ruff check)** | Lints | Same |

The XML was written by hand and PyCharm has not opened it yet. **1 Migrate** and **2 Bot** are
ordinary script/module runs and should be fine. **3 Tests** and **4 Lint** run pytest and ruff
in module mode; PyCharm usually prefers its own pytest configuration type, so if they do not
appear in the dropdown or fail to launch, recreate them through the IDE. Either way these work
from the terminal:

```bash
.venv/bin/pytest
.venv/bin/ruff check .
```

Order matters the first time: **Migrate before Bot.** Nothing creates tables at startup, so
running the bot against an unmigrated database fails on the first `/start`.

### First run, start to finish

1. `cp .env.example .env` then `chmod 600 .env` — the copy is world-readable by default,
   and this file ends up holding a live bot token
2. Message [@BotFather](https://t.me/BotFather) → `/newbot` → create a **test** bot → copy the
   token into `BOT_TOKEN`
3. Message [@userinfobot](https://t.me/userinfobot) → copy your numeric id into `ADMIN_IDS`
4. `CHANNEL_ID` — create the private channel, add the test bot as administrator with "Invite
   users via link" and "Ban users", then take the id (e.g. `-1001234567890`). Phase 1 does not
   use it yet, but startup validates it
5. Run **1 Migrate**, then **2 Bot (polling)**
6. From your phone: `/start`, then `/admin`

The bot loads `.env` itself through pydantic-settings, so no IDE plugin is needed — the run
configurations only set the working directory to the project root.

### What you should see

```
Polling as @yourclub_test_bot (admins: [111111111])
```

If something is wrong you get a message rather than a traceback:

```
Cannot start: 3 environment variable(s) missing or invalid.
  BOT_TOKEN: is empty
  ADMIN_IDS: is empty
  CHANNEL_ID: is empty
```

or, for a token Telegram refuses:

```
Telegram rejected BOT_TOKEN. Check it is copied whole from @BotFather
(format 123456789:AA...), and that it is the TEST bot's token.
```

Both exit with status 1.

### Inspecting the local database

```bash
sqlite3 chatbot.db '.tables'
sqlite3 chatbot.db 'SELECT telegram_id, username, role FROM users;'
sqlite3 chatbot.db 'SELECT * FROM audit_log ORDER BY created_at DESC LIMIT 10;'
```

`chatbot.db` is gitignored. Delete it and re-run **1 Migrate** to start clean.

### Starting and stopping

The bot **is** the process. While `main.py` runs it polls Telegram and answers; the moment it
exits the bot is silent. Nothing starts it at boot, and there is no server — closing PyCharm or
stopping the run stops the bot.

Telegram holds undelivered updates for about 24 hours, so messages sent while it is down are not
lost. They queue, and the next time a poller starts it receives the backlog at once — which is
worth remembering before starting it against a live bot.

From a terminal:

```bash
.venv/bin/python main.py        # run in the foreground; Ctrl-C stops it
```

In the foreground, Ctrl-C is all you need. If it ever ends up detached, match on `main.py`
alone — **not** on the launcher path:

```bash
pgrep -fl 'main\.py'                  # what is running (check the output is really the bot)
pkill -TERM -f 'Python main\.py'      # stop it
```

`.venv/bin/python` does **not** appear in the running process's command line. The venv launcher
re-execs as the real interpreter, so `ps` shows
`/opt/homebrew/.../Python.app/Contents/MacOS/Python main.py`. A pattern containing
`.venv/bin/python` matches only the shell that started it — kill that and the Python process is
reparented to PID 1 and keeps polling, while `pgrep` with the same wrong pattern reports nothing
running. That combination is how you end up believing a live bot is off when it is not.

Verify a stop by what it *does*, not only by `pgrep`: with nothing polling, a message sent to the
bot goes unanswered and Telegram's `pending_update_count` rises and stays up.

### One token, one process

Telegram delivers each update to exactly one poller. If you start a second copy — another IDE
run, a terminal — both hold the token and updates land at random (CLAUDE.md rule 4). Stop one
before starting the other.

Because this is the live bot, a local run intercepts messages from real members and answers them
with whatever is built so far.

## Credentials

`.env` holds **live production credentials** for `@yourstoryclub_bot`. It is the only place on
this machine they exist, its permissions are `600` (owner-only), and it is gitignored.

### Before the first commit, enable the hook

`.gitignore` stops files you never add. It cannot stop `git add -f .env`, or a token pasted into
a `.py` file. The hook in `.githooks/` covers those:

```bash
git init
git config core.hooksPath .githooks    # do this before the first commit
```

It refuses any commit that stages a `.env` file (`.env.example` is fine), or whose staged
content contains something shaped like a Telegram bot token or a WayForPay secret. CI runs
`gitleaks` as well (standing gate S4), but that only fires after a push — this fires before the
commit exists.

### What must never be committed

| Path | Why |
| --- | --- |
| `.env` | The bot token and admin ids |
| `chatbot.db` | Real user rows |
| `.idea/` | Local IDE state, including run configurations |

`.env.example` **is** committed: names only, no values.

### If a credential ever does get committed

Removing it in a later commit is not enough — it stays in the history and the reflog. Revoke it
instead: `/revoke` in @BotFather issues a new token and kills the old one immediately. Same
principle for WayForPay keys in Phase 5.

Note that this bot's token has been pasted into a chat transcript, so it already exists outside
`.env`. Revoking and reissuing is the clean fix whenever that is convenient.

## Quality gate commands

Run all of these before calling any change done:

```bash
ruff check .                # S1  lint
ruff format --check .       # S2  formatting  (drop --check to rewrite)
pytest                      # S3  tests
alembic upgrade head        # S7  migrations apply from empty
alembic check               # S7  models and migrations agree
```

Creating a migration after changing `db/models.py`:

```bash
alembic revision --autogenerate -m "add users and audit_log"
```

`alembic/env.py` takes the database URL from `DATABASE_URL`, never from `alembic.ini`, so
migrations can't accidentally target a different database than the app. It also renders custom
column types as their standard SQLAlchemy equivalent, so a migration never imports application
code — migrations are frozen history and must keep running after the models move on.

`alembic check` fails when `db/models.py` has drifted from the migrations. Run it before
opening a pull request; CI runs it too.

## Environment variables

All of these live in `.env` locally and in platform environment variables in production.
`.env` is gitignored and must never be committed; `.env.example` lists the names only.

| Variable | Required from | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | Phase 0 | @BotFather token. Currently the **live** bot — see Credentials above. |
| `ADMIN_IDS` | Phase 0 | Comma-separated Telegram user ids with admin access |
| `CHANNEL_ID` | Phase 3 | The private channel the bot administrates. Validated if set; Phase 1 does not read it |
| `DATABASE_URL` | Phase 1 | SQLite (`chatbot.db`); PostgreSQL if this ever moves to a host |
| `SUBSCRIPTION_PRICE` | Phase 2A | Regular monthly price for a new joiner |
| `SUBSCRIPTION_CURRENCY` | Phase 2A | Defaults to `UAH` |
| `SUBSCRIPTION_PERIOD_DAYS` | Phase 2A | Defaults to 30 |
| `SUBSCRIPTION_GRACE_DAYS` | Phase 2A | Days overdue before an admin is alerted. Defaults to 3 |
| `LEGACY_OFFER_DEADLINE` | optional | After this, community membership no longer buys a free month |
| `WAYFORPAY_MERCHANT_ACCOUNT` | Phase 2A | Merchant login |
| `WAYFORPAY_MERCHANT_DOMAIN` | Phase 2A | Domain registered with WayForPay |
| `WAYFORPAY_SECRET_KEY` | Phase 2A | Secret Key. Never logged, never committed |
| `MODE` | Phase 4 | `polling` (dev) or `webhook` (prod) |
| `BASE_URL` | Phase 4 | Public HTTPS base used to register the webhook |
| `WEBHOOK_SECRET` | Phase 4 | Matched against `X-Telegram-Bot-Api-Secret-Token` |
| `JOBS_SECRET` | Phase 4 | Required header on `POST /jobs/*` |
| `SENTRY_DSN` | Phase 4 | Error reporting |
| `LOG_LEVEL` | optional | Defaults to `INFO` |
| `DISPLAY_TIMEZONE` | optional | Display only — storage is always UTC. Defaults to `Europe/Kyiv`. |

WayForPay credentials (`WAYFORPAY_MERCHANT_ACCOUNT`, `WAYFORPAY_SECRET_KEY`,
`SUBSCRIPTION_PRICE`, `SUBSCRIPTION_CURRENCY`) are commented out in `.env.example` until
Phase 5, and in production belong only in platform environment variables — never in logs.

## CI

`.github/workflows/ci.yml` runs the six steps from the quality-gates document in order:
install, `ruff check`, `ruff format --check`, `gitleaks detect`, `alembic upgrade head`
against a throwaway Postgres, then `pytest --cov=services`.

The migration stage does three things: `upgrade head` from an empty database, a
`downgrade base` and back (a one-way migration would make the Phase 4 rollback drill
impossible), and `alembic check` for model drift.

Every step is live. Coverage is **not** enforced and the 80 % target in the gates document
no longer applies: the suite was deliberately cut to ~30 tests on 2026-10-02 covering the paths
where a bug costs money, access or trust. See `CLAUDE.md` rule 11.

`psycopg[binary]` is installed even though the application uses SQLite, because the CI
migration check runs against a throwaway Postgres as the gates document specifies. That CI run
is now the only place the PostgreSQL path gets exercised.

Enable branch protection on GitHub so `main` only takes green builds.

## Layout

```
main.py        entry point: polling (dev) or FastAPI app (prod)
config.py      env vars, validated at startup
handlers/      aiogram routers — start, admin, errors, materials, profiles, payments
middlewares/   session.py — one database session per update
services/      business logic — users, audit, subscriptions, payments, broadcast, stats
web/           routes.py: /telegram/webhook, /payments/wayforpay, /jobs/*, /health
db/            models.py, session.py
alembic/       migrations
tests/         unit tests for services, integration tests for handlers and routes
docs/gates/    one record per passed gate
```

Business logic goes in `services/` so it can be unit-tested without Telegram or network.
Handlers stay thin: parse the update, call a service, render the reply.

Two conventions are load-bearing and explained in `CLAUDE.md`: routers are built by
`build_router()` factories rather than created at import time, and the gated admin router is
registered *before* the router that issues the refusal.

## Phase status

See [`docs/gates/README.md`](docs/gates/README.md) for the live table and for the Gate 0
items that still need a human (creating the two bots, adding the test bot to the channel with
"Invite users via link" and "Ban users", recording `CHANNEL_ID`, pushing and confirming CI).

| Phase | Content | Gate record |
| --- | --- | --- |
| 0 | Setup and hello world | repo side done; bots and CI need a human |
| 1 | Skeleton, database, admin gate | code and tests done; awaiting phone smoke test |
| 2 | Knowledge base | — |
| 3 | Manual subscriptions and channel access | — (valid early launch point) |
| 4 | Production infrastructure | — |
| 5 | WayForPay payments | — |
| 6 | Networking profiles | — |
| 7 | Broadcasts and statistics | — |
| 8 | Launch | — |

## Operations

### Rollback

TODO(phase-4): write the exact steps once the rollback drill (G4.11) has been run — target is
redeploying the previous commit in under 5 minutes.

Locally, rolling a schema change back is `alembic downgrade -1` (CI proves every migration
round-trips). Rolling code back needs git, which is not initialised yet.

### Back up and restore the local database

Not the Phase 4 restore drill (G4.9) — that needs managed backups on a host — but worth knowing:

```bash
cp chatbot.db chatbot.db.backup      # back up
cp chatbot.db.backup chatbot.db      # restore
```

Both procedures are required in writing before launch (G8.5), and each must have been
rehearsed at least once rather than merely documented.
