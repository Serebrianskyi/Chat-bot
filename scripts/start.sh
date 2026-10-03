#!/bin/sh
# Production start command: bring the schema up to date, then hand the process to the bot.
#
# Migrations run here, not in a separate deploy step, so a fresh database is usable on first boot
# and the founding-member seed is applied exactly once. `alembic upgrade head` is a no-op when
# there is nothing new, so running it on every deploy is correct rather than wasteful.
#
# `exec` replaces this shell with Python, so the platform's stop signal reaches the bot itself.
# Without it the signal goes to the shell and the container is killed on a timeout instead.
set -eu

echo "==> Applying database migrations"
alembic upgrade head

echo "==> Starting bot"
exec python main.py
