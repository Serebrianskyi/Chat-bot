# Gate 2A: Onboarding and subscription start

Date: 2026-09-30
Commit: not under version control (git left uninitialised at the owner's request)
CI run: none — no GitHub remote exists (G0.3 open)

Scope and item list: `docs/phase-2a-scope.md`. Mechanics: `docs/telegram-bot-payments-design.md`.

Verified in a throwaway virtualenv built only from `requirements-dev.txt`, so results reflect the
pinned dependencies rather than a drifted working venv.

## Automated items

| # | Result | Evidence |
| --- | --- | --- |
| A.1 | pass | `test_start_twice_creates_one_user_and_one_subscription` |
| A.2 | pass | `test_custom_list_price_wins`, `test_custom_list_beats_community_membership`, and case-insensitive matching |
| A.3 | pass | `test_community_member_gets_a_free_period`; `test_left_member_is_not_treated_as_community` covers `left`/`kicked` |
| A.4 | pass | `test_new_joiner_pays_regular_price_immediately` — `expires_at == started_at`, so the daily job invoices them |
| A.5 | pass | `test_membership_check_failure_means_new_joiner`, `test_unset_channel_id_means_new_joiner`. Fails **closed** |
| A.6 | pass | `test_second_start_does_not_extend_the_free_period` |
| A.7 | pass | `test_after_the_deadline_community_membership_buys_nothing`, plus boundary case in `tests/test_pricing.py` |
| A.8 | pass | `test_custom_rule_is_pinned_to_the_user_id` |
| A.9 | pass | `test_invoice_signature_field_order`, `test_check_status_signature_is_two_fields` — expected HMAC computed independently in the test from the documented order, not by calling the code under test |
| A.10 | pass | `test_tampered_field_is_rejected` (5 fields incl. upgrading Declined → Approved), `test_missing_signature_is_rejected`, `test_check_status_rejects_a_tampered_body` |
| A.11 | pass | `test_duplicate_approved_extends_access_once` |
| A.12 | pass | `test_in_processing_neither_grants_nor_fails` |
| A.13 | pass | `test_declined_denies_without_granting` |
| A.14 | pass | `test_amount_mismatch_is_refused`, `test_currency_mismatch_is_refused`, `test_custom_price_is_what_gets_checked` (a discounted member, so the check is not comparing a value to itself) |
| A.15 | pass | `test_raw_response_is_stored` |
| A.16 | pass | `test_status_check_failure_is_retryable_not_fatal` — a signature mismatch leaves the row ERROR (non-terminal), never acting on untrusted data |
| A.17 | pass | `test_admin_notified_when_grace_expires`, `test_notification_writes_an_audit_row`, `test_member_is_not_removed_only_reported` |
| A.18 | pass | `test_unreachable_admin_does_not_break_the_job` |
| A.19 | pass | `tests/test_pricing.py` — 21 tests over expiry arithmetic, all timezone-aware |
| A.20 | pass | `test_due_job_does_not_double_invoice`, `test_admin_notified_only_once`, `test_paid_member_is_not_invoiced_again`, plus scheduler `coalesce`/`max_instances` |
| A.21 | **not done** | Sandbox run. Needs WayForPay merchant credentials, which do not exist yet |

## Standing gate

| # | Result | Evidence |
| --- | --- | --- |
| S1 | pass | `ruff check .` → All checks passed |
| S2 | pass | `ruff format --check .` → 50 files |
| S3 | pass | `pytest` → **169 passed**, no skips (wayforpay 29, billing 26, config 25, pricing 21, start 18, admin 15, audit 7, errors 7, main 7, models 7, scheduler 7) |
| S4 | **not done** | No git repository, so gitleaks has nothing to scan. Blocked on G0.3/G0.4 |
| S5 | pass | `tests/test_config.py` — 18 tests; blank and malformed values named individually |
| S6 | pass | `test_notification_writes_an_audit_row` — actor, action and target |
| S7 | pass | `alembic upgrade head` (2 revisions), `alembic check` → no drift, `downgrade base`, re-upgrade. PostgreSQL DDL checked offline: `NUMERIC(12,2)`, enum as `VARCHAR`, `order_reference` UNIQUE |
| S8 | pass | Unchanged from Phase 1; `tests/test_errors.py` |
| S9 | **not done** | Manual phone test. See below |
| S10 | pass | README status, env-var table (8 new rows), `.env.example`, `CLAUDE.md` rules 5–7 and layout note |
| S11 | pass (advisory) | `pytest --cov=services` → **95%** (pricing/audit/users/scheduler 100%, wayforpay 98%, billing 97%, subscriptions 86%) |

## Found while building

- **An invoice failure left an orphan `payments` row.** The row was flushed before
  `CREATE_INVOICE` was called, so a gateway error left a `pending_payment` with no `invoice_url`.
  Two consequences: the poller would `CHECK_STATUS` an order WayForPay never created, and
  `open_payment()` treated the member as already invoiced, so they were never retried until the
  2-hour timeout. The row is now removed on failure, and two tests cover it — one of which
  previously claimed to and did not.
- **`is_in_community` had its guard in the wrong place.** `member.status` sat outside the
  `try`, so an unexpected response shape raised instead of failing closed. Moved inside, which is
  what A.5 actually requires.
- **The jobs were built but never scheduled.** Caught before reporting; `services/scheduler.py`
  now registers both, and `tests/test_scheduler.py` asserts they are registered — a job that
  exists but never runs passes every unit test.
- **A blank `SUBSCRIPTION_PRICE` stopped the bot from starting.** `.env.example` ships it blank,
  and an empty string failed `Decimal` parsing rather than falling back to the default — so
  onboarding could not run while the merchant account was being set up. Blank now means "use the
  default" for the numeric and mode settings, and the scheduler separately refuses to bill at a
  price of 0, so a blank price is safe rather than silently free.
- **A `.env` rewrite silently did nothing.** The guard was `if "SUBSCRIPTION_PRICE" not in text`,
  and that string already appeared in an old commented-out block, so the substring matched a
  comment and the append was skipped. `.env` is now regenerated from the template with live
  values re-injected by anchored `^KEY=` matching, and every key verified present exactly once.
- **`revoke_for_refund` was completely untested** — the one path in this slice that *takes access
  away*, specified in `CLAUDE.md` rule 7. Now covered, including an audit row carrying the order
  reference and a payment whose subscription has gone.

## Not met

- **A.21** — sandbox payment. Needs `WAYFORPAY_MERCHANT_ACCOUNT`, `_MERCHANT_DOMAIN` and
  `_SECRET_KEY`. Until they exist, the scheduler logs one warning and does not start the billing
  jobs, so onboarding runs alone.
- **S9** — phone smoke test, inherited from Phase 1 and still open.
- **S4** — secrets scan, blocked on there being no repository.

## Unverified rather than failing

- **The community branch against real Telegram.** `CHANNEL_ID` is unset and the bot belongs to no
  chat, so A.3 and A.7 are proven only with a stubbed `getChatMember`. Adding the bot to the
  community as administrator is what settles them.
- **`SUBSCRIPTION_PRICE` is unset**, so no invoice has been built from a real price.
- **`alembic check` against PostgreSQL** — still SQLite only; the first CI run settles it.
- **Nothing automatically detects a refund.** `revoke_for_refund` is implemented, specified and
  tested, but `poll_open_payments` only re-checks payments that have not settled, so a payment
  that completes and is later refunded is never looked at again. The revoke path therefore has no
  automatic caller — it must be invoked by hand for now. A refund sweep is outside this slice's
  agreed scope; `test_settled_payments_are_not_re_polled` documents the limitation so
  `CLAUDE.md` rule 7 is not mistaken for fully wired.

## Decision: NOT PASSED — automated items green, blocked on A.21, S4, S9

Every item provable from the repository passes. The slice cannot be closed until a real invoice
has been created and confirmed, which needs merchant credentials.

To close: supply the three WayForPay values and `SUBSCRIPTION_PRICE` → run one sandbox payment
(A.21) → add the bot to the community as admin and set `CHANNEL_ID` (settles A.3/A.7 for real) →
`git init` + `git config core.hooksPath .githooks` + push (S4, G0.3, G0.4) → phone smoke test (S9).
