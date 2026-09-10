import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.main import create_app


@pytest.mark.parametrize(
    "path,status,code",
    [
        ("/missing", 404, "not_found"),
        ("/private", 403, "forbidden"),
        ("/crash", 500, "internal_error"),
    ],
)
async def test_errors_hide_exception_details(path: str, status: int, code: str) -> None:
    app = create_app()

    @app.get("/private")
    async def private() -> None:
        raise HTTPException(403, detail="secret-marker")

    @app.get("/crash")
    async def crash() -> None:
        raise RuntimeError("secret-marker")

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get(path)
    assert response.status_code == status
    assert "secret-marker" not in response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    assert body["request_id"] == response.headers["x-request-id"]


class Input(BaseModel):
    count: int


async def test_method_error_preserves_allow_header() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://test"
    ) as client:
        response = await client.post("/health/live")
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]


async def test_validation_does_not_echo_rejected_input() -> None:
    app = create_app()

    @app.post("/validate")
    async def validate(body: Input) -> dict[str, int]:
        return {"count": body.count}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/validate", json={"count": "secret-marker"})
    assert response.status_code == 422
    assert "secret-marker" not in response.text
    assert response.json()["error"]["code"] == "invalid_request"


async def test_expected_application_error_has_safe_contract() -> None:
    from app.core.errors import AppError

    app = create_app()

    @app.get("/busy")
    async def busy() -> None:
        raise AppError(503, "overloaded", "잠시 후 다시 요청해 주세요.")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/busy")
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "overloaded",
        "message": "잠시 후 다시 요청해 주세요.",
    }
