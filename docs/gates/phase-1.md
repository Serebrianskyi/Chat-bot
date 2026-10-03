# Gate 1: Skeleton, database, admin gate

Date: 2026-09-28
Commit: not yet under version control (git left uninitialised at the owner's request)
CI run: not yet — no GitHub remote exists (G0.3 still open)

Verified locally in a throwaway virtualenv built only from `requirements-dev.txt`, so the
results reflect the pinned dependencies rather than a drifted working venv.

## Automated items

| #     | Result | Notes / evidence |
| ----- | ------ | ---------------- |
| G1.1  | pass | `test_start_twice_creates_exactly_one_row` — two `/start` updates, one row |
| G1.2  | pass | `test_start_updates_a_changed_username`; `test_start_dropping_a_username_stores_none` covers removing it too |
| G1.3  | pass | `test_non_admin_gets_no_menu` — asserts the refusal *and* the absence of the panel |
| G1.4  | pass | `test_admin_gets_the_menu`, `test_every_menu_button_carries_a_gated_payload` — all five buttons, each payload parsing back to its action |
| G1.5  | pass | `test_forged_admin_callback_is_rejected`, parametrised over every payload read off the real keyboard, plus an action that does not exist yet. Both halves asserted: no menu response, and the refusal delivered |
| G1.6  | pass (SQLite) | `alembic upgrade head` from empty, `downgrade base`, re-upgrade, then `alembic check` → "No new upgrade operations detected". PostgreSQL DDL verified offline with `alembic upgrade head --sql`: `role VARCHAR(16)` with no type creation, `created_at TIMESTAMP WITH TIME ZONE`, no duplicate constraints |

Extra beyond the gate, because the code would otherwise have been wrong:

| Check | Result | Notes |
| ----- | ------ | ----- |
| Gate reads `ADMIN_IDS`, not `users.role` | pass | `test_admin_gate_reads_the_environment_not_the_stored_role` tampers with the column and still gets refused |
| Timestamps are UTC-aware on read | pass | `tests/test_models.py` — SQLite returns naive datetimes; `UtcDateTime` normalises, so Phase 3 date math (G3.2) behaves the same locally and on PostgreSQL |
| Naive datetimes refused on write | pass | `test_naive_input_is_refused` |

## Standing gate

| #   | Result | Notes / evidence |
| --- | ------ | ---------------- |
| S1  | pass | `ruff check .` → All checks passed |
| S2  | pass | `ruff format --check .` → 39 files already formatted |
| S3  | pass | `pytest` → 64 passed, no skips (test_admin 15, test_config 15, test_audit 7, test_errors 7, test_main 7, test_models 7, test_start 6) |
| S4  | **not done** | Blocked on G0.3/G0.4: there is no git repository, so gitleaks has no history to scan. A blank `.env` and a local `chatbot.db` now exist on disk; both are in `.gitignore`, but that is not the check. Satisfied by the first CI run after `git init` and a push |
| S5  | pass | `tests/test_config.py` — each required variable removed in turn, and separately blanked, `ConfigError` names it and says why. Hardened after running the bot for real: see "Found by running it" below |
| S6  | pass | `tests/test_audit.py` — a row carries actor, action and target; `record_action` does not commit, so a rolled-back action leaves no row |
| S7  | pass | See G1.6. CI also runs `alembic check` |
| S8  | pass | `tests/test_errors.py` — an exception injected into the real `/start` handler still sends the user a message; the exception text never reaches the chat; the traceback is logged; a failure to notify is swallowed rather than re-raised |
| S9  | **not done** | Manual phone smoke test. Needs a @BotFather token — see below |
| S10 | pass | `README.md` and `CLAUDE.md` updated in the same change: status, what works today, `alembic check`, the `psycopg` note, layout additions |
| S11 | pass (advisory) | `pytest --cov=services` → 100 % (`services/users.py` 13/13, `services/audit.py` 17/17) |

## Not met

- **S9 — manual smoke test from a phone.** Blocked on Gate 0: no bot has been created in
  @BotFather, so there is no token to run against.
- **S4 — secrets scan.** Blocked on there being no repository to scan.
- **G0.3 / G0.4** remain open for the same reason — no git repository and no GitHub remote,
  which the owner chose to set up themselves.

Both unmet items are blockers, not advisories. Neither is waived.

## Found by running it

Starting the bot from the IDE surfaced four defects that no test had caught, because every test
built `Settings` explicitly rather than reading a real `.env`:

1. **`.env.example` produced garbage, not blanks.** Comments sat on the same line as the value
   (`CHANNEL_ID=   # private channel id`), and python-dotenv does not strip trailing comments —
   so `CHANNEL_ID` loaded as the literal string `"# private channel id, e.g. -1001234567890"`
   and passed validation. Every comment now sits on its own line, with a note in the file
   saying why.
2. **Blank values passed S5.** `BOT_TOKEN=` validated as an empty string, so startup succeeded,
   `ADMIN_IDS` parsed to an empty set, and `/admin` would have refused *everybody* with no
   explanation — exactly the failure S5 exists to prevent. Required fields now carry
   `min_length=1`, and the error lists every offending variable with a reason.
3. **The bot's HTTP session leaked on a failed start.** `delete_webhook()` ran outside the
   `try`, so `finally: session.close()` never fired and the real error was buried under
   aiohttp's "Unclosed client session" warnings. Both first API calls are now inside the try.
4. **`run_polling(settings)` ignored its own argument.** It called a module-level cached
   `get_session_factory()`, which read the environment instead of the settings passed in. That
   made the function untestable and the parameter a lie. The engine is now injected, and the
   cached globals are gone.

Also hardened while there: a malformed `ADMIN_IDS` and a non-numeric `CHANNEL_ID` now fail at
startup rather than at first use, blank optional variables become `None` instead of `""`, and a
token Telegram rejects produces an actionable message and exit code 1 rather than a traceback.

`tests/test_main.py` (7 tests) and eight more in `tests/test_config.py` cover all of it.

## Unverified rather than failing

- **`alembic check` against PostgreSQL.** Drift detection compares the model metadata to a
  *reflected* schema, so it needs a live server; only the SQLite run has been done. The
  PostgreSQL DDL itself was checked offline (see G1.6). The first CI run settles this; there is
  no local PostgreSQL to check against.

## Decision: NOT PASSED — automated items all green, blocked on S4 and S9

Every item that can be proven from the repository passes. Phase 1 is not closed, because two
standing-gate blockers depend on infrastructure that does not exist yet. Closing it needs, in
order: create the two bots (Gate 0); `git init`, push to a private GitHub repo and confirm CI
green (satisfies S4, G0.3, G0.4, and the PostgreSQL `alembic check`); fill `.env` with the
**test** token; run `python main.py`; send `/start` and `/admin` from a phone as both an admin
and a non-admin (S9); then update this record and the table in `docs/gates/README.md`.

Do not start Phase 2 before that happens.
