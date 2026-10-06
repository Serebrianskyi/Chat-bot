# Gate records

One file per closed phase: `phase-0.md`, `phase-1.md`, … Copy `TEMPLATE.md` to start one.
Together these are the project's release history — they are the evidence that a phase
actually passed, not just that it felt finished.

| Gate | Phase | Record | Status |
| ---- | ----- | ------ | ------ |
| 0 | Setup | — | **done.** One live bot (not the plan's prod/test pair), channel configured, repo pushed, CI running |
| 1 | Skeleton, database, admin gate | [phase-1.md](phase-1.md) | automated items green; S9 (phone test) never formally recorded |
| 2A | Onboarding + subscription start | [phase-2a-onboarding-payments.md](phase-2a-onboarding-payments.md) | automated items green; **A.21 open** — no real payment has been made |
| 2 | Knowledge base | — | **deferred**, reordered after 2A |
| 3 | Manual subscriptions and access | — | partly absorbed into 2A. Removal and manual grants outstanding |
| 4 | Production infrastructure | — | **partly done**: deployed on Railway with managed Postgres and migrate-on-deploy. No webhook, Sentry, uptime monitoring or backups |
| 5 | WayForPay payments | — | **partly done**: invoicing and confirmation live. Renewals (`CHARGE`) not built |
| 6 | Networking profiles | — | not started |
| 7 | Broadcasts and statistics | — | not started |
| 8 | Launch | — | not started |

Update this table when a gate closes, and link the record.

## Remaining Gate 0 items

The repository side of Gate 0 is in place (layout, CI, `README.md`, `.env.example`, a green
trivial test). These items need a human and cannot be done from the repo:

- **G0.1 / G0.5 / G0.6** — **done, with a deviation.** One bot exists, `@yourstoryclub_bot`,
  not the plan's production/test pair: the owner chose to run the live bot. It is an
  administrator of `Create Your Story | Club` (supergroup, id `-1003980549671`) with
  "Invite users via link" and "Ban users" both granted, verified via `getChatMember`.
  `CHANNEL_ID` is set in `.env`.
- **G0.3** — `git init`, then `git config core.hooksPath .githooks` **before the first
  commit**, then push to a private GitHub repo and confirm the CI workflow runs green.
- **G0.4** — confirm `.env` is untracked and `.env.example` is committed. Verified in a
  throwaway repo: with this `.gitignore`, `git add -A` stages neither `.env` nor
  `chatbot.db` nor `.idea/`.

Phase order was changed on 2026-09-30: the knowledge base (plan Phase 2) is deferred, and
**Phase 2A** — onboarding plus the start of the subscription mechanism — was built instead,
drawing G3.1/G3.2 and G5.1/G5.2/G5.3/G5.5/G5.8 from the plan's Phases 3 and 5. It has its own
item list (A.1–A.22) in `docs/phase-2a-scope.md`. Removal from the community is deliberately
**not** built: an overdue member is reported to an admin instead.

Phase 1's code and tests are complete but its gate is **not closed**:
the standing gate's manual smoke test (S9) needs a real bot token, which depends on the
@BotFather items above. See [phase-1.md](phase-1.md) for exactly what remains.

`main.py` runs for real in polling mode once `.env` has a test-bot token. Webhook mode still
raises `NotImplementedError` — that is Phase 4.

## Local run, not Phase 4

The owner chose to run the bot on this laptop — from PyCharm, against SQLite — rather than
deploy to a host. That is a test-server arrangement, not the production infrastructure phase,
and **no `phase-4.md` record should be opened for it**.

There is no local PostgreSQL, so the Postgres side of **G1.6** stays unverified until the first
CI run. (Docker files were written for this and then removed at the owner's request, after
Docker Desktop on this machine could not pull the base images.)

Every Phase 4 item is still open:

- **G4.1** staging and production separation (one environment, one bot)
- **G4.2** migrations on deploy — there is no deploy; `alembic upgrade head` is run by hand
- **G4.10** redeploy keeps data — not meaningful without a deploy step
- **G4.3 / G4.4** webhook and `/jobs/*` secret rejection — no webhook, no job endpoints
- **G4.5** `/health` — no HTTP server; polling has no endpoint
- **G4.6** scheduled jobs
- **G4.7 / G4.8** Sentry and UptimeRobot
- **G4.9** restore drill, **G4.11** rollback drill
- **G4.12** all features working on staging
