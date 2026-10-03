# Telegram Community Bot: Quality Gates

Companion to `telegram-bot-implementation-plan.md`. The plan says **what** to build in each phase; this document says **how you prove each phase is done** before moving on.

---

## 1. How gates work

- Every phase ends with a **gate**: a checklist of pass/fail items.
- A phase is closed only when **every** item passes. No "mostly done".
- Each item says **how it's verified** (automated test, CI, manual check, or drill) and what **evidence** you keep.
- If an item fails: fix it, re-run the whole gate, then move on. Don't carry failures into the next phase, least of all into payments.
- Record each gate result in `docs/gates/phase-N.md` (date, commit hash, checked items, notes). This becomes your release history.

### Verification types

| Code | Meaning | Evidence |
|---|---|---|
| **AUTO** | Automated test in `tests/`, runs in CI | Green CI run linked in the gate record |
| **CI** | Static check in the pipeline (lint, format, secrets scan) | Green CI run |
| **MAN** | Manual check on the test bot from a real phone | Short note or screenshot in the gate record |
| **DRILL** | Operational rehearsal (restore, rollback, alert) | Date + what happened + time taken |
| **REV** | Self-review against a checklist | Ticked checklist in the gate record |

### Severity

- **Blocker**: must pass. Every item in a gate is a blocker unless marked *(advisory)*.
- **Advisory**: should pass; if it doesn't, write down why and when it will be fixed.

---

## 2. Standing gate (applies to every phase from Phase 1)

These are checked on every pull request and again at each phase gate.

| # | Check | How | Type |
|---|---|---|---|
| S1 | `ruff check .` passes | GitHub Actions | CI |
| S2 | `ruff format --check .` passes | GitHub Actions | CI |
| S3 | `pytest` passes, no skipped tests without a reason comment | GitHub Actions | CI |
| S4 | No secrets committed | `gitleaks` step in CI; `.env` in `.gitignore` | CI |
| S5 | App refuses to start with a missing required env var, with a clear message | Test that clears one var and expects a startup error | AUTO |
| S6 | Every data-changing admin action writes an `audit_log` row (actor, action, target) | Test per admin action | AUTO |
| S7 | New DB changes come with an Alembic migration that upgrades from the previous head | CI runs `alembic upgrade head` on an empty DB | CI |
| S8 | No unhandled exception reaches the user as silence; user gets a friendly error | Global error handler test | AUTO |
| S9 | Smoke test on the test bot: `/start`, `/admin`, and every feature built so far | Phone | MAN |
| S10 | README updated: run instructions, new env vars, new commands | Self-review | REV |
| S11 | Test coverage of `services/` ≥ 80% *(advisory)* | `pytest --cov=services` | CI |

### Pull request checklist (paste into the PR template)

```
- [ ] CI green (lint, format, tests, secrets scan, migrations)
- [ ] New logic in services/ has unit tests
- [ ] Admin actions write audit_log
- [ ] Tried it on the test bot from my phone
- [ ] README / .env.example updated if needed
```

---

## 3. Phase gates

### Gate 0: Setup

| # | Check | Type |
|---|---|---|
| G0.1 | Test bot replies to `/start` from a phone | MAN |
| G0.2 | Two separate bots exist: production and test | REV |
| G0.3 | CI workflow runs and is green on a trivial test | CI |
| G0.4 | `.env` not tracked by git; `.env.example` is | CI (S4) |
| G0.5 | Test bot is admin of the test channel with "Invite users via link" and "Ban users" | MAN |
| G0.6 | `CHANNEL_ID` recorded in `.env` and verified by a script that calls `get_chat` | MAN |

**Exit condition:** you can change code, push, and see CI run; the bot answers you.

---

### Gate 1: Skeleton, database, admin gate

| # | Check | Type |
|---|---|---|
| G1.1 | `/start` twice creates exactly one user row | AUTO |
| G1.2 | `/start` after a username change updates the stored username | AUTO |
| G1.3 | Non-admin `/admin` gets no menu | AUTO |
| G1.4 | Admin `/admin` gets the menu with all buttons | AUTO |
| G1.5 | Non-admin sending a forged admin callback (`callback_data`) is rejected | AUTO |
| G1.6 | `alembic upgrade head` works from an empty DB | CI (S7) |
| G1.7 | Standing gate S1–S10 | — |

**Exit condition:** access control is proven by tests, not just by the menu being hidden.

---

### Gate 2: Knowledge base

| # | Check | Type |
|---|---|---|
| G2.1 | Create / rename / delete category | AUTO |
| G2.2 | Create / edit / delete material (text, file, link) | AUTO |
| G2.3 | `/cancel` from every FSM state returns to idle with no partial DB writes | AUTO |
| G2.4 | Deleting a category with materials is either blocked or asks for confirmation (pick one, test it) | AUTO |
| G2.5 | Lists over 8 items paginate correctly (first, middle, last page) | AUTO |
| G2.6 | Add a PDF, a text, a link; open each as a regular user | MAN |
| G2.7 | Sending a sticker / voice mid-dialog gets a polite retry, not a crash | MAN |
| G2.8 | Regular users cannot reach admin edit actions | AUTO |
| G2.9 | Standing gate | — |

**Exit condition:** an admin who isn't you could manage content without help.

---

### Gate 3: Manual subscriptions and channel access

| # | Check | Type |
|---|---|---|
| G3.1 | Date math: grant N days, grant forever, extend active, extend expired | AUTO |
| G3.2 | All dates stored in UTC; display converts to Europe/Kyiv (or chosen zone) | AUTO |
| G3.3 | Invite link is single-use (`member_limit=1`) and time-limited | AUTO + MAN |
| G3.4 | Expiry job is idempotent: second run changes nothing | AUTO |
| G3.5 | Expiry job continues past a user who blocked the bot and marks them | AUTO |
| G3.6 | Removal uses ban + unban, so the user can rejoin after paying again | AUTO + MAN |
| G3.7 | Reminder sent once, 3 days before expiry, not repeatedly | AUTO |
| G3.8 | End-to-end with a second account: grant 1 day → join → set `expires_at` in the past → run job → removed → notified | MAN |
| G3.9 | Every grant / extend / revoke in `audit_log` with admin id | AUTO (S6) |
| G3.10 | Standing gate | — |

**Exit condition:** you would trust this to run the club with manual payments. This is a valid early launch point.

---

### Gate 4: Production infrastructure

| # | Check | Type |
|---|---|---|
| G4.1 | Staging and production are separate: different bots, DBs, secrets | REV |
| G4.2 | Migrations run automatically on deploy | MAN |
| G4.3 | Webhook rejects requests with wrong / missing `X-Telegram-Bot-Api-Secret-Token` | AUTO |
| G4.4 | `/jobs/*` rejects requests without the jobs secret | AUTO |
| G4.5 | `/health` returns 200 only when the DB is reachable | AUTO |
| G4.6 | Scheduled jobs fire on staging at the expected time | MAN |
| G4.7 | A deliberate exception appears in Sentry within 1 minute | DRILL |
| G4.8 | UptimeRobot alerts when the service is stopped and clears when restarted | DRILL |
| G4.9 | **Restore drill:** last backup restored into a scratch DB and queried | DRILL |
| G4.10 | Redeploy keeps all data | MAN |
| G4.11 | **Rollback drill:** previous commit redeployed in under 5 minutes | DRILL |
| G4.12 | All Phase 1–3 features work on staging from a phone | MAN |
| G4.13 | Standing gate | — |

**Exit condition:** if something breaks at 2 a.m., you'll be told, and you know how to roll back and restore.

---

### Gate 5: WayForPay payments (strictest gate)

| # | Check | Type |
|---|---|---|
| G5.1 | Signature verification accepts valid callbacks | AUTO |
| G5.2 | Signature verification rejects tampered amount / order / status | AUTO |
| G5.3 | **Duplicate callback** (same `order_reference`) extends access only once | AUTO |
| G5.4 | Out-of-order callbacks (renewal before initial) end in a correct state | AUTO |
| G5.5 | Unknown user / unknown order: logged, answered to WayForPay, no crash | AUTO |
| G5.6 | Callback handler always replies with the signed "accept" response WayForPay expects | AUTO |
| G5.7 | Raw callback body stored for every request, searchable by `order_reference` | AUTO |
| G5.8 | Amount and currency in callback checked against expected price | AUTO |
| G5.9 | Sandbox: pay → subscription active → invite received → joined channel | MAN |
| G5.10 | Sandbox: renewal extends by exactly one period | MAN |
| G5.11 | Sandbox: failed renewal → grace period → expiry → removal → message | MAN |
| G5.12 | Sandbox: user cancels → no further charges, access until period end | MAN |
| G5.13 | Reconciliation: WayForPay dashboard totals match `payments` table for the sandbox period | REV |
| G5.14 | Production: one real payment with a real card, then refunded; state correct after refund | MAN |
| G5.15 | Payment secrets exist only in platform env vars, not in logs | REV |
| G5.16 | Standing gate | — |

**Exit condition:** every money path has an automated test and a sandbox run; you've seen one real payment go through.

---

### Gate 6: Networking profiles

| # | Check | Type |
|---|---|---|
| G6.1 | Non-subscribers and expired users cannot open the catalog | AUTO |
| G6.2 | Hidden profiles never appear in catalog or search | AUTO |
| G6.3 | Field length limits enforced | AUTO |
| G6.4 | Search by occupation keyword finds the right profiles | AUTO |
| G6.5 | Fill, edit, hide, unhide a profile from a phone | MAN |
| G6.6 | Admin lookup shows the profile card | MAN |
| G6.7 | Standing gate | — |

---

### Gate 7: Broadcasts and statistics

| # | Check | Type |
|---|---|---|
| G7.1 | Segment queries return the right users (all / active / expired) | AUTO |
| G7.2 | `RetryAfter` pauses and resumes without skipping or duplicating users | AUTO |
| G7.3 | Blocked users are marked and counted as failed, sending continues | AUTO |
| G7.4 | Zero-recipient broadcast reports "no recipients", not "sent" | AUTO |
| G7.5 | Sending rate stays ≤ 20 messages/second | AUTO |
| G7.6 | Confirmation step prevents sending with a single tap | MAN |
| G7.7 | Preview matches what users receive (text, media, button) | MAN |
| G7.8 | Staging broadcast to ~50 seeded users completes with correct counts | MAN |
| G7.9 | Stats numbers match direct SQL counts | AUTO |
| G7.10 | Standing gate | — |

---

### Gate 8: Launch

| # | Check | Type |
|---|---|---|
| G8.1 | Gates 0–7 all recorded as passed | REV |
| G8.2 | Production env vars double-checked against `.env.example` | REV |
| G8.3 | Sentry, UptimeRobot and backups active on **production** | MAN |
| G8.4 | Existing subscribers imported; count matches your source list | MAN |
| G8.5 | Rollback and restore procedures written in README | REV |
| G8.6 | Admin guide and user FAQ published | REV |
| G8.7 | Soft launch (10–20 members, 1 week): zero unhandled errors in Sentry | MAN |
| G8.8 | Soft launch: WayForPay totals reconcile with `payments` table | REV |
| G8.9 | Operator readiness: you can grant, revoke, find a payment by order reference, and restore a backup without looking things up | DRILL |

**Exit condition:** open to everyone.

---

## 4. After launch: ongoing gates

| Cadence | Check |
|---|---|
| Every PR | Standing gate S1–S11 |
| Every deploy | Staging first; smoke test S9 on staging before promoting |
| Weekly | Sentry reviewed, no unresolved errors older than 7 days |
| Monthly | Reconcile WayForPay with `payments`; restore drill; dependency updates |
| Quarterly | Rotate `WEBHOOK_SECRET` and `JOBS_SECRET`; review admin list |

---

## 5. CI pipeline reference

`.github/workflows/ci.yml` should run, in order:

1. Install dependencies (cached)
2. `ruff check .`
3. `ruff format --check .`
4. `gitleaks detect`
5. Start a throwaway Postgres service; `alembic upgrade head`
6. `pytest --cov=services --cov-report=term-missing`

Merge to `main` is allowed only when all steps are green (enable branch protection on GitHub).

---

## 6. Gate record template

Save as `docs/gates/phase-N.md`:

```
# Gate N: <name>

Date:
Commit:
CI run:

| # | Result | Notes / evidence |
|---|---|---|
| GN.1 | pass / fail | |
| GN.2 | | |

Advisory items not met (with reason and fix date):

Decision: PASSED / NOT PASSED
```
