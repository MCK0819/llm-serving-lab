import logging

import httpx
import pytest
from fastapi import FastAPI, Request

from app.core.request_context import RequestContextMiddleware


async def test_raw_exception_and_request_credentials_never_reach_logs_or_error(caplog):
    from app.core.logging import SafeLoggingMiddleware

    app = FastAPI()
    app.add_middleware(SafeLoggingMiddleware)
    app.add_middleware(RequestContextMiddleware)

    @app.post("/failure")
    async def failure(request: Request):
        body = await request.body()
        raise RuntimeError(body.decode() + request.headers["authorization"])

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with caplog.at_level(logging.ERROR):
            response = await client.post(
                "/failure",
                content="PRIVATE_QUESTION_PDF",
                headers={"Authorization": "Bearer PRIVATE_KEY"},
            )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert "request_failed" in caplog.text
    assert "PRIVATE_" not in response.text + caplog.text


@pytest.mark.parametrize("process", ["api", "worker"])
def test_configured_dependency_and_server_logging_cannot_emit_raw_canaries(caplog, process):
    from app.core.logging import configure_logging
    from app.documents.worker import configure_worker_logging

    names = [
        "uvicorn.access",
        "uvicorn.error",
        "httpx",
        "httpcore",
        "sqlalchemy",
        "python_multipart",
        "pypdf",
    ]
    originals = {
        name: (
            logging.getLogger(name).handlers[:],
            logging.getLogger(name).propagate,
            logging.getLogger(name).filters[:],
        )
        for name in names
    }
    try:
        (configure_logging if process == "api" else configure_worker_logging)()
        with caplog.at_level(logging.DEBUG):
            for name in [
                "httpx",
                "httpcore.connection",
                "sqlalchemy.engine",
                "python_multipart.multipart",
                "pypdf._page",
                "uvicorn.access",
            ]:
                logging.getLogger(name).warning("PRIVATE_QUERY_TOKEN_PDF")
            try:
                raise ValueError("PRIVATE_UPSTREAM_RESPONSE")
            except ValueError:
                logging.getLogger("uvicorn.error").exception("PRIVATE_UPSTREAM_RESPONSE")
        assert "PRIVATE_" not in caplog.text
    finally:
        for name, (handlers, propagate, filters) in originals.items():
            logger = logging.getLogger(name)
            logger.handlers, logger.propagate, logger.filters = handlers, propagate, filters


async def test_cancelled_request_is_not_rewritten_as_server_error():
    import asyncio

    from app.core.logging import SafeLoggingMiddleware

    async def cancelled(scope, receive, send):
        raise asyncio.CancelledError

    async def forbidden(*args):
        pytest.fail("cancelled request must not write a replacement response")

    with pytest.raises(asyncio.CancelledError):
        await SafeLoggingMiddleware(cancelled)({"type": "http", "state": {}}, forbidden, forbidden)


async def test_failure_after_headers_finishes_transport_without_raw_exception(caplog):
    from app.core.logging import SafeLoggingMiddleware

    async def broken_stream(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"event: sources\ndata: {}\n\n",
                "more_body": True,
            }
        )
        raise RuntimeError("PRIVATE_UPSTREAM_RESPONSE")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=SafeLoggingMiddleware(broken_stream)),
        base_url="http://test",
    ) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert "event: done" not in response.text
    assert "PRIVATE_" not in response.text + caplog.text
