from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.errors import AppError
from app.core.rate_limit import RateLimiter
from app.core.settings import Settings
from app.inference.admission import Admission
from app.inference.response import PreparedStream, StreamResponse
from app.users.dependencies import identity_dependency
from app.users.service import AuthService
from app.users.types import Identity

type QuestionPreparer = Callable[[Identity, str, AsyncExitStack], Awaitable[PreparedStream]]


class QuestionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    question: str = Field(min_length=1)


def build_question_router(
    auth: AuthService | None,
    prepare_question: QuestionPreparer | None,
    admission: Admission,
    settings: Settings,
    limiter: RateLimiter | None = None,
) -> APIRouter:
    router = APIRouter()
    authenticate = identity_dependency(auth)

    @router.post(
        "/questions",
        response_class=StreamResponse,
        status_code=200,
        responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": QuestionInput.model_json_schema()}},
            }
        },
    )
    async def question(
        request: Request, identity: Annotated[Identity, Depends(authenticate)]
    ) -> StreamResponse:
        if limiter is not None:
            limiter.check(identity.user_id, "question")
        payload = await read_question(request)

        async def prepare(stack: AsyncExitStack) -> PreparedStream:
            if prepare_question is None:
                raise AppError(503, "rag_unavailable", "문서 검색 기능이 아직 준비되지 않았습니다.")
            return await prepare_question(identity, payload.question, stack)

        return StreamResponse(
            prepare,
            admission,
            request.state.request_id,
            execution_seconds=settings.execution_timeout,
            send_seconds=settings.send_timeout,
        )

    return router


async def read_question(request: Request) -> QuestionInput:
    # Authenticate first, then bound actual bytes before JSON parsing.
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise AppError(415, "unsupported_media_type", "JSON 형식으로 질문을 보내 주세요.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 16384:
            raise AppError(413, "request_too_large", "질문 요청이 너무 큽니다.")
        body.extend(chunk)
    try:
        return QuestionInput.model_validate_json(body)
    except ValidationError:
        raise AppError(422, "invalid_request", "요청 형식을 확인해 주세요.") from None
