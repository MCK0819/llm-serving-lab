import httpx
import pytest
from fastapi import FastAPI, Request

from app.core.errors import register_error_handlers
from app.core.request_context import RequestContextMiddleware


@pytest.mark.parametrize("headers", [{}, {"Content-Length": "1"}])
async def test_received_body_limit_applies_before_multipart_parser(headers):
    from app.core.body_limit import BodyLimitMiddleware

    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, maximum_bytes=32)
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)

    @app.post("/documents")
    async def upload(request: Request):
        async with request.form() as form:
            return {"parts": len(form)}

    async def body():
        yield b"--boundary\r\n"
        yield b"x" * 32
        raise AssertionError("The receiver must stop on the first excessive chunk")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/documents",
            content=body(),
            headers={
                **headers,
                "Content-Type": "multipart/form-data; boundary=boundary",
            },
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert response.json()["request_id"] == response.headers["x-request-id"]
