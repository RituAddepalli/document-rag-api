"""
API-level test fixtures.

These tests exercise the real HTTP layer (auth, ingestion, chat) against a
real PostgreSQL + pgvector database, since the Chunk model's Vector column
can't be faithfully emulated by SQLite. They are skipped automatically if
no test database is reachable, so `pytest` still runs cleanly in
environments without Docker/Postgres available (e.g. `test_chunking.py`
and `test_security.py` will still run).

To run the full suite:
    docker compose up -d db
    export TEST_DATABASE_URL=postgresql+asyncpg://rag_user:rag_password@localhost:5432/rag_test_db
    pytest
"""
import asyncio
import os

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://rag_user:rag_password@localhost:5432/rag_test_db",
)

# IMPORTANT: BaseHTTPMiddleware (used by ErrorLoggingMiddleware) sits outside
# FastAPI's dependency-injection graph, so it can't pick up the get_db
# override used by the `client` fixture below - it always opens sessions via
# app.db.session.AsyncSessionLocal, which is bound to settings.DATABASE_URL
# at import time. To exercise the error-logging middleware against the same
# throwaway test database, we point DATABASE_URL at it too, *before* any
# `app.*` module gets imported anywhere in the test session.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _pg_available() -> bool:
    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return False

    async def _check():
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.get_event_loop().run_until_complete(_check())
    except Exception:
        return False


requires_pg = pytest.mark.skipif(
    not _pg_available(), reason="No reachable Postgres test database (set TEST_DATABASE_URL)."
)


@pytest_asyncio.fixture
async def client():
    from app.api.deps import get_db
    from app.db.base_class import Base
    from app.main import app

    engine = create_async_engine(TEST_DATABASE_URL)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
    app.dependency_overrides.clear()
