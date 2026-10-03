import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import TypeDecorator, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Make the project root importable so `db.models` resolves when alembic runs.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Read .env the same way config.py does, so `alembic upgrade head` and the application can
# never disagree about which database they are talking to. Existing environment variables win,
# which is what lets compose point the container at Postgres without a .env in the image.
load_dotenv()

from db.models import Base  # noqa: E402

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# The database URL comes from the environment, never from alembic.ini -- the same
# variable the app uses, so migrations can never target a different database.
database_url = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./club_bot.db")
config.set_main_option("sqlalchemy.url", database_url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Model metadata for 'autogenerate' support.
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def render_item(type_, obj, autogen_context):
    """Render custom column types as the standard type they wrap.

    Without this, autogenerate writes `db.models.UtcDateTime()` into the migration and does
    not import it, so the migration fails with NameError. Rendering the impl instead keeps
    migrations dependent on `sa` alone — which is what we want regardless: a migration is a
    frozen historical artifact, and it must keep running after the application model it was
    generated from has been renamed, moved or deleted.

    Returning False falls back to alembic's default rendering.
    """
    if type_ == "type" and isinstance(obj, TypeDecorator):
        return f"sa.{obj.impl!r}"
    return False


# Shared by offline and online runs so both render migrations identically.
CONTEXT_OPTS = {"target_metadata": target_metadata, "render_item": render_item}


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **CONTEXT_OPTS,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **CONTEXT_OPTS)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
