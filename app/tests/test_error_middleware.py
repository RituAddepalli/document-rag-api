import pytest
from sqlalchemy import select

from app.tests.conftest import requires_pg

pytestmark = [pytest.mark.asyncio, requires_pg]


async def test_unhandled_exception_returns_clean_json_and_is_logged(client):
    resp = await client.get("/debug/error")

    assert resp.status_code == 500
    body = resp.json()
    assert body["error"] == "internal_server_error"
    assert "request_id" in body
    # Raw exception details must never leak to the client.
    assert "Traceback" not in resp.text

    # Verify the error was actually persisted to the error_logs table.
    from app.models.error_log import ErrorLog
    from app.tests.conftest import TEST_DATABASE_URL
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    engine = create_async_engine(TEST_DATABASE_URL)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with session_factory() as session:
        result = await session.execute(
            select(ErrorLog).where(ErrorLog.endpoint == "/debug/error")
        )
        rows = result.scalars().all()
    await engine.dispose()

    assert len(rows) >= 1
    assert rows[-1].method == "GET"
    assert "deliberate test error" in rows[-1].error_message
