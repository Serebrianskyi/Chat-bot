# Telegram Community Bot: Implementation Plan

A step-by-step plan for building a paid community bot (private channel access, WayForPay subscriptions, knowledge base, networking profiles, admin panel) from zero experience to a production service.

The rule for the whole project: **build in thin vertical slices.** Each phase ends with something that works end to end, and you don't start the next phase until the current one passes its quality gate.

Companion document: `telegram-bot-admin-panel.md` (what the admin panel does and looks like).

---

## 1. Target architecture

### How the bot connects to Telegram

- **@BotFather** issues a token. The token *is* the bot; whoever holds it controls the bot.
- **Your Python program** runs on a server you control and talks to `https://api.telegram.org/bot<TOKEN>/...`. Telegram does not host your code.
- **Users** find the bot by `@username`. Telegram forwards their messages to your program and delivers your replies.
- If the program isn't running, the bot is silent. Production therefore needs an always-on server.

### Receiving updates

| Mode | How it works | When to use |
|---|---|---|
| Polling | Your program asks Telegram for new updates in a loop | Local development (Phases 0–3) |
| Webhook | Telegram POSTs each update to your public HTTPS URL | Production (Phase 4 onward) |

Only **one** process may use a token at a time. Never run local polling and the production bot on the same token; use a separate test bot.

### Production setup

```
                 ┌──────────────────────────── Railway / Render ───────────────────────────┐
Telegram ──POST──▶ /telegram/webhook ┐                                                     │
WayForPay ─POST──▶ /payments/wayforpay├─▶ FastAPI app (aiogram inside) ──▶ PostgreSQL       │
Cron ─────POST───▶ /jobs/expire      ┘            │                                        │
UptimeRobot ─GET─▶ /health                        └──▶ Telegram Bot API (replies, kicks)   │
                 └──────────────────────────────────────────────────────────────────────────┘
                         Logs → platform console      Errors → Sentry
```

### Stack

| Concern | Choice | Why |
|---|---|---|
| Language / framework | Python 3.12 + aiogram 3 | Most popular, async, many examples |
| Web server | FastAPI + uvicorn | Webhooks and payment callbacks in one app |
| Database | SQLite locally → PostgreSQL in production | Zero setup first, managed backups later |
| ORM / migrations | SQLAlchemy 2 + Alembic | Schema changes without losing data |
| Scheduler | Platform cron hitting `/jobs/*` (APScheduler acceptable early) | Survives web process restarts |
| Hosting | Railway or Render | Git push to deploy, free HTTPS, managed Postgres |
| Payments | WayForPay (recurring) | Required by the business |
| Monitoring | Sentry (errors), UptimeRobot (uptime) | Free tiers are enough |
| Quality tooling | ruff, pytest, pytest-asyncio, GitHub Actions | Cheap, fast, standard |

Alternative: Node.js + grammY if you are stronger in JavaScript. The plan is the same.

### Where the code lives

1. **Your laptop**: where you write and run it during development.
2. **GitHub (private repo)**: source of truth and transport. Nothing runs here.
3. **The server**: the only copy that talks to real users. Deploys happen on push to `main`.

### Repository layout

```
club-bot/
├── main.py                 # entry point: polling (dev) or FastAPI app (prod)
├── config.py               # reads env vars, validates them at startup
├── .env.example            # variable names only, committed
├── .env                    # real secrets, NEVER committed
├── requirements.txt
├── alembic/                # DB migrations
├── handlers/
│   ├── start.py            # /start, user menu
│   ├── admin.py            # /admin panel and admin gate
│   ├── materials.py        # knowledge base (user + admin flows)
│   ├── profiles.py         # networking questionnaire and catalog
│   └── payments.py         # subscribe button
├── services/
│   ├── subscriptions.py    # grant / extend / expire / kick
│   ├── payments.py         # WayForPay client, signature checks
│   ├── broadcast.py        # segmented, rate-limited sending
│   └── stats.py
├── web/
│   └── routes.py           # /telegram/webhook, /payments/wayforpay, /jobs/*, /health
├── db/
│   ├── models.py
│   └── session.py
└── tests/
```

### Data model

| Table | Key fields |
|---|---|
| `users` | telegram_id (PK), username, first_name, role (`user`/`admin`), is_blocked_bot, created_at |
| `subscriptions` | id, user_id, status (`active`/`expired`/`cancelled`), started_at, expires_at, source (`wayforpay`/`manual`), wayforpay_rec_token, granted_by |
| `payments` | id, user_id, order_reference (UNIQUE), amount, currency, status, raw_callback (JSON), created_at |
| `profiles` | user_id, name, occupation, offers, looking_for, contacts, is_visible |
| `materials` | id, category_id, title, body, file_id, url, created_by, created_at |
| `categories` | id, name, sort_order |
| `audit_log` | id, actor_id, action, target_user_id, details (JSON), created_at |
| `broadcasts` | id, created_by, segment, content, sent_count, failed_count, created_at |

All timestamps stored in **UTC**. Convert to local time for display only.

---

## 2. Quality gates: how they work

Every phase has an **exit gate**. A phase is done only when every item in its gate is checked. If an item fails, fix it before moving on; don't carry debt into the payment phase.

### Standing gate (applies to every phase from Phase 1 on)

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `pytest` passes locally and in GitHub Actions CI
- [ ] No secrets in git (`git log -p | grep -i token` finds nothing sensitive; `.env` is in `.gitignore`)
- [ ] `config.py` fails fast with a clear message if a required env var is missing
- [ ] Every data-changing admin action writes an `audit_log` row
- [ ] Manual smoke test from a real phone on the test bot
- [ ] README updated: how to run, env vars, anything new you had to learn

### Test levels used in this plan

- **Unit tests**: pure logic in `services/` (date math, segment selection, signature verification). No Telegram, no network.
- **Integration tests**: handlers against a test database with a mocked Bot object; FastAPI routes via `httpx.AsyncClient`.
- **Manual acceptance**: scripted checklist run on the test bot from a phone.

---

## 3. Phases

Estimates assume part-time work (~10–15 h/week) by someone new to bots.

### Phase 0: Setup and "hello world" (1–2 days)

**Goal:** a bot that answers `/start`, running from your laptop.

Tasks:
- Create two bots in @BotFather: `@yourclub_bot` (production) and `@yourclub_test_bot` (development)
- Create a private GitHub repo, the folder layout above, `.gitignore`, `.env.example`
- Install Python 3.12, create a virtualenv, install aiogram, python-dotenv, ruff, pytest
- Write `main.py` that replies to `/start` using polling
- Add a GitHub Actions workflow running ruff + pytest on every push
- Create the private channel; add the **test** bot as administrator with "Invite users via link" and "Ban users"; record `CHANNEL_ID`

**Exit gate**
- [ ] Test bot replies to `/start` from your phone
- [ ] CI runs green on an empty test
- [ ] `.env` is not in the repo; `.env.example` is
- [ ] Bot is admin in the test channel with the two permissions above

---

### Phase 1: Skeleton, database, admin gate (2–3 days)

**Goal:** users are registered; admins get a menu, others don't.

Tasks:
- SQLAlchemy models + first Alembic migration (`users`, `audit_log`)
- `/start` upserts the user (id, username, name)
- `ADMIN_IDS` env var; middleware or filter that marks admin updates
- `/admin` shows an inline keyboard: Statistics, Broadcast, Users, Knowledge Base, Add subscriber (placeholders)
- Non-admins get "You don't have access" (or silence; choose one and be consistent)
- Learn and use aiogram's FSM (states) and callback queries; you will reuse them everywhere

**Exit gate** (plus standing gate)
- [ ] Test: `/start` twice creates one user row, updates username if changed
- [ ] Test: non-admin `/admin` gets no menu; admin gets the menu
- [ ] Test: a non-admin cannot trigger admin callbacks by crafting callback data
- [ ] Migration runs from an empty DB with `alembic upgrade head`

---

### Phase 2: Knowledge base (3–5 days)

**Goal:** first real feature, no money involved. Teaches the full dialog pattern.

Tasks:
- Tables `categories`, `materials` + migration
- Admin flow: Add material → choose category → send text / file / link → confirm → saved
- Admin flow: rename / delete category, edit / delete material
- User flow: browse categories → list → open material (files re-sent by `file_id`)
- `/cancel` exits any admin dialog at any step
- Pagination for lists longer than ~8 items

**Exit gate** (plus standing gate)
- [ ] Tests for create / edit / delete of categories and materials
- [ ] Test: `/cancel` from every FSM state returns to idle with no partial writes
- [ ] Manual: add a PDF, a text, and a link; open each as a regular user
- [ ] Manual: sending an unexpected content type (sticker, voice) mid-dialog gets a polite retry, not a crash

---

### Phase 3: Manual subscriptions and channel access (3–4 days)

**Goal:** a working paid club with manual payments. **Possible early launch point.**

Tasks:
- Table `subscriptions` + migration
- Admin: find user by `@username` or `telegram_id` → show status, expiry, source
- Admin: grant access for N days or forever; extend; revoke
- On grant: generate a single-use invite (`create_chat_invite_link`, `member_limit=1`, short `expire_date`) and send it to the user
- On revoke / expiry: `ban_chat_member` then `unban_chat_member` (removes without permanent ban), and notify the user
- Expiry job: finds `active` subscriptions with `expires_at < now`, marks `expired`, removes from channel, notifies
- Reminder job: notify users 3 days before expiry
- Handle `TelegramForbiddenError` (user blocked the bot): mark `is_blocked_bot`, continue

**Exit gate** (plus standing gate)
- [ ] Unit tests for expiry date math (N days, forever, extend active, extend expired)
- [ ] Test: expiry job is **idempotent** (running it twice changes nothing the second time)
- [ ] Test: expiry job continues past a user who blocked the bot
- [ ] Manual: grant 1 day to a second account, join via link, confirm the link can't be reused, fast-forward expiry (set `expires_at` in DB), run the job, confirm removal
- [ ] Every grant / revoke visible in `audit_log` with admin id

---

### Phase 4: Production infrastructure (2–4 days)

**Goal:** the bot runs as a real service **before** any payment code is written.

Tasks:
- Create Railway (or Render) project with two environments: `staging` (test bot) and `production` (real bot)
- Provision managed PostgreSQL in each; switch `DATABASE_URL`; run migrations on deploy
- Move to one FastAPI app: `/telegram/webhook`, `/health`, `/jobs/expire`, `/jobs/remind`
- Register the webhook on startup with a `secret_token`; reject requests without the matching header
- Protect `/jobs/*` with a secret header; schedule them with platform cron
- Set all secrets as platform env vars: `BOT_TOKEN`, `ADMIN_IDS`, `CHANNEL_ID`, `DATABASE_URL`, `WEBHOOK_SECRET`, `JOBS_SECRET`, `BASE_URL`, `SENTRY_DSN`
- Sentry for errors; UptimeRobot on `/health`
- Structured logging to stdout
- Backups: confirm platform daily backups; add a weekly `pg_dump` to an S3-compatible bucket
- Deploy flow: PR → CI green → merge to `main` → auto-deploy to staging → manual promote to production

**Exit gate** (plus standing gate)
- [ ] Staging bot works end to end with the phone (Phases 1–3 features)
- [ ] Webhook rejects requests with a wrong or missing secret (test)
- [ ] `/jobs/*` rejects requests without the secret (test)
- [ ] A deliberately raised exception appears in Sentry within a minute
- [ ] UptimeRobot alert fires when you stop the service, clears when you restart
- [ ] **Restore drill:** restore last night's backup into a scratch DB and query it successfully
- [ ] Redeploy doesn't lose data
- [ ] Rollback tested: redeploy the previous commit in under 5 minutes

---

### Phase 5: WayForPay integration (5–8 days) — highest risk

**Goal:** users pay by card, get access automatically, and are charged monthly.

Tasks:
- WayForPay merchant account + sandbox credentials
- `payments` table with `order_reference` **UNIQUE**
- User: "Subscribe" → create invoice via WayForPay API → send payment URL button
- `POST /payments/wayforpay`: verify HMAC signature, store `raw_callback`, respond with the signed "accept" payload WayForPay expects
- On `Approved`: activate or extend subscription, send invite link, store recurring token
- Recurring charges: handle subsequent callbacks by extending `expires_at`; handle `Declined` / failed renewals with a grace period (e.g. 3 days) and a message to the user
- User: "Cancel subscription" → stop recurring via API, keep access until `expires_at`
- Admin stats: successful charges this month

**Exit gate** (plus standing gate)
- [ ] Unit test: signature verification accepts valid, rejects tampered callbacks
- [ ] Test: **duplicate callback** with the same `order_reference` extends access only once
- [ ] Test: callback for an unknown user / order is logged and answered, not crashed on
- [ ] Test: out-of-order callbacks (renewal arrives before initial) end in a correct state
- [ ] Sandbox: full flow — pay, receive invite, join channel
- [ ] Sandbox: successful renewal extends by exactly one period
- [ ] Sandbox: failed renewal → grace period → expiry → removal
- [ ] Sandbox: user cancel → no further charges, access until period end
- [ ] Every callback's raw body is stored and findable by `order_reference`
- [ ] One real payment with a real card on production, then refunded

---

### Phase 6: Networking profiles (2–3 days)

**Goal:** members can present themselves and find each other.

Tasks:
- `profiles` table + migration
- FSM questionnaire: name, occupation, offers, looking for, contacts; editable later
- Catalog for active subscribers only; paginated; simple text search
- Visibility toggle (hide my profile)
- Admin user lookup shows the profile card

**Exit gate** (plus standing gate)
- [ ] Test: non-subscribers and expired users cannot open the catalog
- [ ] Test: hidden profiles never appear in catalog or search
- [ ] Manual: fill, edit, hide, unhide a profile; search finds it by occupation keyword
- [ ] Input length limits enforced (no 4,000-character "name")

---

### Phase 7: Broadcasts and statistics (2–3 days)

**Goal:** admins can message segments and see how the club is doing.

Tasks:
- Admin: compose (text / photo / video / link button) → preview → choose segment (all / active / expired) → confirm
- Send with ~20 messages/second max (`asyncio.sleep(0.05)`), honour `RetryAfter`
- Mark users who blocked the bot; record `sent_count` / `failed_count`
- Run sending as a background task so the admin gets "Broadcast started" immediately and a summary when done
- Statistics screen: total users, active, expired, blocked bot, payments this month, profiles filled, material views

**Exit gate** (plus standing gate)
- [ ] Unit tests for segment selection queries
- [ ] Test: a `RetryAfter` error pauses and resumes without skipping users
- [ ] Test: broadcast with 0 recipients reports that instead of "sent"
- [ ] Manual: preview matches what users receive
- [ ] Staging: broadcast to ~50 seeded test users completes with correct counts
- [ ] Confirmation step makes it impossible to broadcast by a single mis-tap

---

### Phase 8: Launch (2–3 days)

Tasks:
- Point the production bot to production environment; run migrations
- Import existing subscribers (manual grants with `source=manual`)
- Write short admin guide (link to `telegram-bot-admin-panel.md`) and a user FAQ
- Soft launch to 10–20 trusted members for a week, then open to all

**Launch gate**
- [ ] All previous phase gates green
- [ ] Production env vars double-checked; staging and production use different bots, DBs, and WayForPay keys
- [ ] Sentry, UptimeRobot, backups active on production
- [ ] Rollback procedure written in README and tested once
- [ ] Soft-launch week: no unhandled errors in Sentry, no payment discrepancies between WayForPay dashboard and `payments` table
- [ ] You know how to: grant access by hand, revoke, find a payment by order reference, restore a backup

---

## 4. Timeline summary

| Phase | Content | Estimate | Cumulative |
|---|---|---|---|
| 0 | Setup | 1–2 days | ~2 days |
| 1 | Skeleton + admin gate | 2–3 days | ~1 week |
| 2 | Knowledge base | 3–5 days | ~2 weeks |
| 3 | Manual subscriptions | 3–4 days | ~2.5 weeks (early launch possible) |
| 4 | Production infra | 2–4 days | ~3 weeks |
| 5 | WayForPay | 5–8 days | ~4.5 weeks |
| 6 | Networking | 2–3 days | ~5 weeks |
| 7 | Broadcasts + stats | 2–3 days | ~5.5 weeks |
| 8 | Launch | 2–3 days | ~6 weeks |

---

## 5. Running costs

| Item | Cost |
|---|---|
| Railway / Render + managed Postgres | ~$10/month |
| Sentry, UptimeRobot | free tiers |
| Backup bucket | < $1/month |
| Domain (optional) | ~$10/year |
| WayForPay | percentage per transaction |

---

## 6. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Duplicate / out-of-order payment callbacks | Double access or lost payments | UNIQUE `order_reference`, idempotent handler, raw callback storage (Phase 5 gate) |
| Expiry job fails silently | Non-payers keep access | Idempotent job, Sentry alerts, daily count in stats |
| Token leak | Full takeover of bot | Secrets only in env vars; revoke via BotFather `/revoke` if leaked |
| Two processes on one token | Erratic behaviour | Separate test bot; never poll the production token locally |
| SQLite file wiped on redeploy | Data loss | Postgres from Phase 4; restore drill in gate |
| Telegram rate limits during broadcast | Messages dropped, temporary ban | ≤20 msg/s, handle `RetryAfter` |
| Time zone bugs in expiry | Early / late kicks | UTC everywhere, unit tests for date math |
| Scope creep before launch | Never launching | Phase 3 is a valid launch point; web dashboard deferred |

---

## 7. Deliberately deferred

- **Web admin dashboard** (tables, charts in a browser). Useful once data outgrows chat screens; more expensive to build. Revisit after 2–3 months in production.
- **Docker / VPS**. Only if hosting cost or multiple workers become a real need.
- **Redis / task queue**. Only if broadcasts reach tens of thousands of users.
- **Multi-language UI**. Add i18n only if the audience needs it.

---

## 8. Learning resources

- aiogram 3 docs: https://docs.aiogram.dev/
- Telegram Bot API reference: https://core.telegram.org/bots/api
- FastAPI tutorial: https://fastapi.tiangolo.com/tutorial/
- SQLAlchemy 2 ORM quickstart: https://docs.sqlalchemy.org/en/20/orm/quickstart.html
- WayForPay API docs: https://wiki.wayforpay.com/
