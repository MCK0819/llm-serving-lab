from uuid import UUID

from httpx import ASGITransport, AsyncClient


async def test_liveness_without_database_or_model() -> None:
    from app.main import create_app

    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        first = await client.get("/health/live", headers={"X-Request-ID": "untrusted"})
        second = await client.get("/health/live")

    assert first.status_code == 200
    assert first.json() == {"status": "ok"}
    assert UUID(first.headers["x-request-id"]).version == 4
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
