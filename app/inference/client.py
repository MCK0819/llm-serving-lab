from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager

import httpx
from pydantic import ValidationError

from app.core.errors import AppError
from app.inference.protocol import WireChunk
from app.inference.sse_parser import SSEParser
from app.inference.types import Completed, Delta, FinishReason, Usage


class InferenceClient:
    """Borrows a lifespan-owned HTTP client; owns each individual response."""

    def __init__(self, http: httpx.AsyncClient, model: str, output_tokens: int = 512) -> None:
        self.http = http
        self.model = model
        self.output_tokens = output_tokens

    @asynccontextmanager
    async def open(
        self, messages: list[dict[str, str]]
    ) -> AsyncIterator[AsyncIterator[Delta | Completed]]:
        try:
            async with self.http.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Accept": "text/event-stream", "Accept-Encoding": "identity"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": True,
                    "max_tokens": self.output_tokens,
                    "n": 1,
                    "temperature": 0,
                    "stream_options": {"include_usage": True},
                },
            ) as response:
                if response.status_code != 200:
                    raise self._invalid()
                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip().lower()
                )
                if content_type != "text/event-stream":
                    raise self._invalid()
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise self._invalid()
                async with aclosing(self._events(response)) as events:
                    yield events
        except httpx.TimeoutException:
            raise AppError(504, "inference_timeout", "모델 응답 시간이 초과되었습니다.") from None
        except httpx.ConnectError:
            raise AppError(
                503, "inference_unavailable", "모델 서버에 연결할 수 없습니다."
            ) from None
        except httpx.HTTPError:
            raise self._invalid() from None

    async def _events(self, response: httpx.Response) -> AsyncGenerator[Delta | Completed]:
        # Iteration errors must be translated here: AsyncExitStack closes the
        # upstream context normally and does not inject an iteration exception.
        try:
            async with aclosing(self._decode_events(response)) as decoded:
                async for event in decoded:
                    yield event
        except httpx.TimeoutException:
            raise AppError(504, "inference_timeout", "모델 응답 시간이 초과되었습니다.") from None
        except httpx.HTTPError:
            raise self._invalid() from None

    async def _decode_events(self, response: httpx.Response) -> AsyncGenerator[Delta | Completed]:
        parser = SSEParser()
        reason: FinishReason | None = None
        usage: Usage | None = None
        async for raw in response.aiter_raw():
            # Split already-arrived data; do not wait to fill a transport chunk,
            # which would delay small token deltas indefinitely on a quiet stream.
            for offset in range(0, len(raw), 4096):
                for payload in parser.feed(raw[offset : offset + 4096]):
                    if payload == "[DONE]":
                        if reason is None:
                            raise self._invalid()
                        yield Completed(reason, usage)
                        return
                    try:
                        chunk = WireChunk.model_validate_json(payload)
                    except ValidationError:
                        raise self._invalid() from None
                    if chunk.choices:
                        if reason is not None:
                            raise self._invalid()
                        choice = chunk.choices[0]
                        if choice.delta.content:
                            yield Delta(choice.delta.content)
                        reason = choice.finish_reason
                    elif chunk.usage is None:
                        raise self._invalid()
                    if chunk.usage is not None:
                        if reason is None or usage is not None:
                            raise self._invalid()
                        usage = Usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
        parser.finish()
        # A valid finish_reason AND protocol terminator are required; EOF alone
        # cannot distinguish a successful response from a cut connection.
        raise self._invalid()

    @staticmethod
    def _invalid() -> AppError:
        return AppError(502, "invalid_inference_response", "모델 응답을 처리하지 못했습니다.")
