import asyncio
import json

import httpx
import pytest

from app.core.errors import AppError
from tests.fakes.streams import ControlledStream


def event(value: object) -> bytes:
    return f"data: {json.dumps(value, ensure_ascii=False)}\n\n".encode()


def chunk(content: str | None = None, reason: str | None = None) -> dict:
    return {
        "id": "test",
        "object": "chat.completion.chunk",
        "model": "test-model",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": reason}],
    }


@pytest.mark.parametrize("reason", ["stop", "length"])
async def test_complete_response_and_usage_close_connection(reason: str) -> None:
    from app.inference.client import InferenceClient
    from app.inference.types import Completed, Delta, Usage

    wire = (
        event({"choices": [{"index": 0, "delta": {"role": "assistant"}}]})
        + event(chunk("안녕하세요", reason))
        + event({"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}})
        + b"data: [DONE]\n\n"
    )
    stream = ControlledStream([bytes([b]) for b in wire])
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://model"
    ) as http:
        async with InferenceClient(http, "test-model").open(
            [{"role": "user", "content": "안녕"}]
        ) as response:
            received = [item async for item in response]
        assert not http.is_closed
    assert received == [Delta("안녕하세요"), Completed(reason, Usage(12, 3))]
    assert stream.closed
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    sent = json.loads(requests[0].content)
    assert sent["max_tokens"] == 512 and sent["n"] == 1 and sent["temperature"] == 0
    assert sent["stream"] is True


@pytest.mark.parametrize(
    "wire",
    [
        b"data: not-json\n\n",
        event([]),
        event({"choices": "wrong"}),
        event({"choices": []}),
        event({"choices": [{"index": 1, "delta": {"content": "wrong"}}]}),
        event({"choices": [{"index": 0, "delta": {"tool_calls": [{}]}}]}),
        event(chunk("text", "tool_calls")),
        event(chunk("text")),
        event(chunk("text", "stop")),
        b"data: [DONE]\n\n",
        event(chunk(None, "stop")) + event(chunk("late")) + b"data: [DONE]\n\n",
        event(chunk(None, "stop"))
        + event({"choices": [], "usage": {"prompt_tokens": -1, "completion_tokens": 1}}),
    ],
)
async def test_malformed_or_incomplete_response_fails_safely(wire: bytes) -> None:
    from app.inference.client import InferenceClient

    stream = ControlledStream([wire])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
        base_url="http://model",
    ) as http:
        with pytest.raises(AppError) as error:
            async with InferenceClient(http, "model").open([]) as response:
                _ = [item async for item in response]
    assert error.value.status == 502
    assert stream.closed


@pytest.mark.parametrize(
    "status,content_type",
    [(500, "text/event-stream"), (401, "application/json"), (200, "text/html")],
)
async def test_preflight_failure_before_iterator_is_returned(
    status: int, content_type: str
) -> None:
    from app.inference.client import InferenceClient

    stream = ControlledStream([b"secret-marker"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, headers={"content-type": content_type}, stream=stream
            )
        ),
        base_url="http://model",
    ) as http:
        with pytest.raises(AppError) as error:
            async with InferenceClient(http, "model").open([]):
                pytest.fail("invalid upstream passed preflight")
    assert error.value.status == 502
    assert "secret-marker" not in str(error.value)
    assert stream.closed


@pytest.mark.parametrize(
    "failure,status",
    [(httpx.ConnectError("secret-marker"), 503), (httpx.ConnectTimeout("secret-marker"), 504)],
)
async def test_connection_failure_is_not_retried(failure: Exception, status: int) -> None:
    from app.inference.client import InferenceClient

    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise failure

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://model"
    ) as http:
        with pytest.raises(AppError) as error:
            async with InferenceClient(http, "model").open([]):
                pytest.fail("unavailable upstream passed preflight")
    assert error.value.status == status
    assert "secret-marker" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "failure,status",
    [(httpx.ReadTimeout("secret-marker"), 504), (httpx.RemoteProtocolError("secret-marker"), 502)],
)
async def test_midstream_failure_closes_connection(failure: Exception, status: int) -> None:
    from app.inference.client import InferenceClient

    stream = ControlledStream([event(chunk("partial"))], failure=failure)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
        base_url="http://model",
    ) as http:
        with pytest.raises(AppError) as error:
            async with InferenceClient(http, "model").open([]) as response:
                _ = [item async for item in response]
    assert error.value.status == status
    assert stream.closed


async def test_cancellation_propagates_and_closes_upstream() -> None:
    from app.inference.client import InferenceClient

    stream = ControlledStream([], block=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
        base_url="http://model",
    ) as http:

        async def consume() -> None:
            async with InferenceClient(http, "model").open([]) as response:
                _ = [item async for item in response]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(stream.waiting.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


async def test_consumer_stops_early_without_closing_shared_client() -> None:
    from app.inference.client import InferenceClient
    from app.inference.types import Delta

    stream = ControlledStream([event(chunk("first"))], block=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
        base_url="http://model",
    ) as http:
        async with InferenceClient(http, "model").open([]) as response:
            assert await anext(response) == Delta("first")
        assert stream.closed
        assert not http.is_closed


async def test_missing_usage_stays_unknown() -> None:
    from app.inference.client import InferenceClient
    from app.inference.types import Completed

    stream = ControlledStream([event(chunk(None, "stop")), b"data: [DONE]\n\n"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
        base_url="http://model",
    ) as http:
        async with InferenceClient(http, "model").open([]) as response:
            assert [item async for item in response] == [Completed("stop", None)]
