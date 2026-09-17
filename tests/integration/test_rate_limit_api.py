from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.core.errors import register_error_handlers
from app.core.request_context import RequestContextMiddleware
from app.core.settings import Settings
from app.documents.router import build_document_router
from app.inference.admission import Admission
from app.rag.router import build_question_router
from app.users.types import Identity

pytestmark = pytest.mark.integration


class FakeAuth:
    def __init__(self, identity: Identity) -> None:
        self.identity = identity

    async def authenticate(self, raw_key: str) -> Identity:
        return self.identity


class RejectingLimiter:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, str]] = []

    def check(self, user_id: UUID, category: str) -> None:
        from app.core.errors import AppError

        self.calls.append((user_id, category))
        raise AppError(
            429,
            "rate_limited",
            "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            headers={"Retry-After": "1", "X-Internal-Path": "secret"},
        )


def api(limiter) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(
        build_question_router(
            FakeAuth(Identity(uuid4(), uuid4())),  # type: ignore[arg-type]
            None,
            Admission(1, 0, 1),
            Settings(),
            limiter,
        )
    )
    return app


def bearer_key() -> str:
    return f"{uuid4()}.{'a' * 43}"


async def test_authenticated_malformed_requests_consume_quota_and_return_retry_after() -> None:
    from app.core.rate_limit import RateLimiter

    app = api(RateLimiter(clock=lambda: 10.0))
    headers = {"Authorization": f"Bearer {bearer_key()}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(20):
            malformed = await client.post("/questions", content=b"not-json", headers=headers)
            assert malformed.status_code == 422
        rejected = await client.post("/questions", content=b"not-json", headers=headers)

    assert rejected.status_code == 429
    assert rejected.headers["retry-after"] == "60"
    assert rejected.json()["error"] == {
        "code": "rate_limited",
        "message": "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
    }
    assert UUID(rejected.json()["request_id"]) == UUID(rejected.headers["x-request-id"])


async def test_authentication_failure_does_not_consume_quota() -> None:
    from app.core.rate_limit import RateLimiter

    app = api(RateLimiter(clock=lambda: 10.0))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(25):
            unauthorized = await client.post("/questions", content=b"not-json")
            assert unauthorized.status_code == 401
        authenticated = await client.post(
            "/questions",
            content=b"not-json",
            headers={
                "Authorization": f"Bearer {bearer_key()}",
                "Content-Type": "application/json",
            },
        )

    assert authenticated.status_code == 422


async def test_document_routes_check_their_category_before_body_or_service_work() -> None:
    identity = Identity(uuid4(), uuid4())
    limiter = RejectingLimiter()
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(
        build_document_router(
            FakeAuth(identity),  # type: ignore[arg-type]
            None,
            limiter,  # type: ignore[arg-type]
        )
    )
    document_id = uuid4()
    headers = {"Authorization": f"Bearer {bearer_key()}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
    ) as client:
        responses = [
            await client.post("/documents", content=b"malformed"),
            await client.get("/documents"),
            await client.get(f"/documents/{document_id}"),
            await client.delete(f"/documents/{document_id}"),
            await client.get("/documents?limit=0"),
            await client.get("/documents/not-a-uuid"),
            await client.delete("/documents/not-a-uuid"),
        ]

    assert [response.status_code for response in responses] == [429] * 7
    assert all(response.headers["retry-after"] == "1" for response in responses)
    assert all("x-internal-path" not in response.headers for response in responses)
    assert limiter.calls == [
        (identity.user_id, "upload"),
        (identity.user_id, "read"),
        (identity.user_id, "read"),
        (identity.user_id, "read"),
        (identity.user_id, "read"),
        (identity.user_id, "read"),
        (identity.user_id, "read"),
    ]
