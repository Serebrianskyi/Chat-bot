## What this changes

<!-- One or two sentences. Which phase does this advance? -->

Phase:

## Standing gate

- [ ] CI green (lint, format, tests, secrets scan, migrations)
- [ ] New logic in services/ has unit tests
- [ ] Admin actions write audit_log
- [ ] Tried it on the test bot from my phone
- [ ] README / .env.example updated if needed

## Invariants touched

- [ ] Timestamps stored in UTC (display conversion only at the edge)
- [ ] No secret in code, logs, or committed files
- [ ] Alembic migration included if `db/models.py` changed
- [ ] Money / expiry paths remain idempotent
