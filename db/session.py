"""Async engine and session factory.

SQLite locally, PostgreSQL in the Docker setup — both driven by ``DATABASE_URL``, so nothing
in this module changes when the database does.

Both functions take their inputs explicitly. There is deliberately no module-level cached
engine: a global would let callers ignore the settings handed to them, which is exactly the
bug that made ``run_polling`` untestable. ``main`` owns the engine for the life of the process
and disposes of it on shutdown.

Handlers never touch either of these. The session middleware in ``middlewares.session`` opens
one session per update and injects it.
"""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

# TODO(phase-4): the FastAPI app and the /jobs/* endpoints will need a factory shared across
#                requests. Build it once at app startup and put it in app.state — not in a
#                module-level cache.


def create_engine(database_url: str) -> AsyncEngine:
    """Build an engine for an explicit URL.

    ``pool_pre_ping`` costs one cheap round trip per checkout and avoids handing out a
    connection the database has already dropped — which managed Postgres does on idle.
    """
    return create_async_engine(database_url, echo=False, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    """Session factory bound to one engine.

    ``expire_on_commit=False`` so attributes stay readable after a handler commits — without
    it, rendering a reply from a just-saved object triggers a lazy reload on a closed session.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
