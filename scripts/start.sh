#!/bin/sh
# Production start command: bring the schema up to date, then hand the process to the bot.
#
# `exec` replaces this shell with Python, so the platform's stop signal reaches the bot itself.
# Without it the signal goes to the shell and the container is killed on a timeout instead.
set -eu

# Refuse to run without an explicit DATABASE_URL.
#
# config.py falls back to a local SQLite file, which is correct for development and catastrophic
# here: a hosting container's filesystem is rebuilt on every deploy, so the bot would happily
# migrate, seed members and take payments into a database that disappears at the next push. The
# first deploy did exactly that — the log read "Context impl SQLiteImpl" and nothing looked wrong.
# A refusal to boot is far cheaper than discovering it after real money has moved.
if [ -z "${DATABASE_URL:-}" ]; then
    echo "FATAL: DATABASE_URL is not set." >&2
    echo "" >&2
    echo "Without it the bot would use a local SQLite file, and this container's disk is" >&2
    echo "rewritten on every deploy — every member, subscription and payment would be lost." >&2
    echo "" >&2
    echo "Add a PostgreSQL service and set, on THIS service's Variables tab:" >&2
    echo "    DATABASE_URL=\${{ Postgres.DATABASE_URL }}" >&2
    exit 1
fi

case "$DATABASE_URL" in
    sqlite*)
        echo "FATAL: DATABASE_URL points at SQLite: $DATABASE_URL" >&2
        echo "A container's filesystem does not survive a deploy. Use PostgreSQL." >&2
        exit 1
        ;;
esac

echo "==> Applying database migrations"
alembic upgrade head

echo "==> Starting bot"
exec python main.py
