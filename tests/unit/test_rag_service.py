import asyncio
import json
import threading
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.core.errors import AppError
from app.inference.admission import Admission
from app.inference.response import PreparedStream, StreamResponse
from app.inference.types import Completed, Delta
from app.users.types import Identity


def _sse_events(messages: list[dict[str, object]]) -> list[tuple[str, dict[str, object]]]:
    body = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    ).decode()
    events = []
    for record in body.strip().split("\n\n"):
        lines = record.splitlines()
        if len(lines) == 2 and lines[0].startswith("event: ") and lines[1].startswith("data: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


async def _run_response(
    prepare,
    admission: Admission,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, str]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await StreamResponse(prepare, admission, "request-9")({"type": "http"}, receive, send)
    return messages


async def test_empty_retrieval_returns_grounded_no_context_without_generation() -> None:
    from app.rag.service import RagService

    organization_id = UUID("11111111-1111-1111-1111-111111111111")
    identity = Identity(UUID("22222222-2222-2222-2222-222222222222"), organization_id)
    calls: list[tuple[UUID, list[float], str, int]] = []

    class Embedding:
        revision = "revision-42"

        async def embed_query(self, question: str) -> list[float]:
            assert question == "휴가 규정"
            return [0.25, 0.75]

    class Retriever:
        async def search(
            self, tenant: UUID, vector: list[float], revision: str, limit: int = 5
        ) -> list[object]:
            calls.append((tenant, vector, revision, limit))
            return []

    class ForbiddenPrompt:
        def build(self, question: str, chunks: list[object]) -> object:
            pytest.fail("an empty retrieval must not build a prompt")

    class ForbiddenInference:
        def open(self, messages: list[dict[str, str]]) -> object:
            pytest.fail("an empty retrieval must not open inference")

    service = RagService(Embedding(), Retriever(), ForbiddenPrompt(), ForbiddenInference())
    gate = Admission(1, 0, 1)

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        return await service.prepare(identity, "휴가 규정", stack)

    messages = await _run_response(prepare, gate)

    assert messages[0]["status"] == 200
    assert calls == [(organization_id, [0.25, 0.75], "revision-42", 5)]
    assert _sse_events(messages) == [
        ("sources", {"request_id": "request-9", "sources": []}),
        (
            "delta",
            {"request_id": "request-9", "text": "관련 문서에서 답변 근거를 찾지 못했습니다."},
        ),
        (
            "done",
            {"request_id": "request-9", "finish_reason": "no_context", "usage": None},
        ),
    ]
    async with gate.slot():
        pass


async def test_normal_answer_uses_background_prompt_build_and_stack_owned_inference() -> None:
    from app.rag.retrieval import SourceChunk
    from app.rag.service import RagService

    identity = Identity(
        UUID("22222222-2222-2222-2222-222222222222"),
        UUID("11111111-1111-1111-1111-111111111111"),
    )
    chunk = SourceChunk(
        UUID("33333333-3333-3333-3333-333333333333"),
        7,
        "휴가규정.pdf",
        2,
        "연차 휴가는 15일입니다.",
    )
    sources = [
        {
            "source_id": "S1",
            "document_id": "33333333-3333-3333-3333-333333333333",
            "filename": "휴가규정.pdf",
            "page": 2,
        }
    ]
    messages = [
        {"role": "system", "content": "문서 근거만 사용하세요."},
        {"role": "user", "content": "휴가는 며칠인가요?"},
    ]
    event_loop_thread = threading.get_ident()
    prompt_call: tuple[int, str, list[SourceChunk]] | None = None
    inference_messages: list[dict[str, str]] | None = None
    inference_closed = asyncio.Event()

    class Embedding:
        revision = "revision-42"

        async def embed_query(self, question: str) -> list[float]:
            return [1.0, 0.0]

    class Retriever:
        async def search(
            self, organization_id: UUID, vector: list[float], revision: str, limit: int = 5
        ) -> list[SourceChunk]:
            return [chunk]

    class Prompt:
        def build(self, question: str, chunks: list[SourceChunk]) -> object:
            nonlocal prompt_call
            prompt_call = (threading.get_ident(), question, chunks)
            return SimpleNamespace(messages=messages, sources=sources, input_tokens=120)

    class Inference:
        @asynccontextmanager
        async def open(
            self, request_messages: list[dict[str, str]]
        ) -> AsyncIterator[AsyncIterator[Delta | Completed]]:
            nonlocal inference_messages
            inference_messages = request_messages

            async def events() -> AsyncIterator[Delta | Completed]:
                yield Delta("15일")
                yield Completed("stop", None)

            try:
                yield events()
            finally:
                inference_closed.set()

    service = RagService(Embedding(), Retriever(), Prompt(), Inference())
    gate = Admission(1, 0, 1)

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        return await service.prepare(identity, "휴가는 며칠인가요?", stack)

    sent = await _run_response(prepare, gate)

    assert sent[0]["status"] == 200
    assert prompt_call is not None
    assert prompt_call == (prompt_call[0], "휴가는 며칠인가요?", [chunk])
    assert prompt_call[0] != event_loop_thread
    assert inference_messages == messages
    assert _sse_events(sent) == [
        ("sources", {"request_id": "request-9", "sources": sources}),
        ("delta", {"request_id": "request-9", "text": "15일"}),
        (
            "done",
            {"request_id": "request-9", "finish_reason": "stop", "usage": None},
        ),
    ]
    assert inference_closed.is_set()
    async with gate.slot():
        pass


async def test_inference_open_failure_is_http_error_before_any_sse() -> None:
    from app.rag.service import RagService

    identity = Identity(
        UUID("22222222-2222-2222-2222-222222222222"),
        UUID("11111111-1111-1111-1111-111111111111"),
    )

    class Embedding:
        revision = "revision-42"

        async def embed_query(self, question: str) -> list[float]:
            return [1.0]

    class Retriever:
        async def search(
            self, organization_id: UUID, vector: list[float], revision: str, limit: int = 5
        ) -> list[object]:
            return [object()]

    class Prompt:
        def build(self, question: str, chunks: list[object]) -> object:
            return SimpleNamespace(
                messages=[{"role": "user", "content": question}],
                sources=[{"source_id": "S1"}],
                input_tokens=10,
            )

    class Inference:
        @asynccontextmanager
        async def open(
            self, messages: list[dict[str, str]]
        ) -> AsyncIterator[AsyncIterator[Delta | Completed]]:
            raise AppError(503, "inference_unavailable", "모델 서버에 연결할 수 없습니다.")
            yield

    service = RagService(Embedding(), Retriever(), Prompt(), Inference())
    gate = Admission(1, 0, 1)

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        return await service.prepare(identity, "질문", stack)

    sent = await _run_response(prepare, gate)
    body = b"".join(message.get("body", b"") for message in sent)

    assert sent[0]["status"] == 503
    assert json.loads(body)["error"]["code"] == "inference_unavailable"
    assert b"event:" not in body
    async with gate.slot():
        pass


async def test_disconnect_during_retrieval_cancels_work_and_returns_admission() -> None:
    from app.rag.service import RagService

    identity = Identity(
        UUID("22222222-2222-2222-2222-222222222222"),
        UUID("11111111-1111-1111-1111-111111111111"),
    )
    retrieval_started = asyncio.Event()
    retrieval_cancelled = asyncio.Event()
    disconnected = asyncio.Event()

    class Embedding:
        revision = "revision-42"

        async def embed_query(self, question: str) -> list[float]:
            return [1.0]

    class Retriever:
        async def search(
            self, organization_id: UUID, vector: list[float], revision: str, limit: int = 5
        ) -> list[object]:
            retrieval_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                retrieval_cancelled.set()
            return []

    service = RagService(Embedding(), Retriever(), object(), object())
    gate = Admission(1, 0, 1)

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        return await service.prepare(identity, "질문", stack)

    async def receive() -> dict[str, str]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        pytest.fail("retrieval cancellation must happen before a response starts")

    response = StreamResponse(prepare, gate, "request-9")
    task = asyncio.create_task(response({"type": "http"}, receive, send))
    await retrieval_started.wait()
    disconnected.set()
    await asyncio.wait_for(task, 1)

    assert retrieval_cancelled.is_set()
    async with gate.slot():
        pass


async def test_disconnect_releases_request_but_prompt_capacity_waits_for_thread() -> None:
    from app.rag.service import RagService

    identity = Identity(
        UUID("22222222-2222-2222-2222-222222222222"),
        UUID("11111111-1111-1111-1111-111111111111"),
    )
    prompt_started = threading.Event()
    prompt_release = threading.Event()
    prompt_finished = threading.Event()
    disconnected = asyncio.Event()

    class Embedding:
        revision = "revision-42"

        async def embed_query(self, question: str) -> list[float]:
            return [1.0]

    class Retriever:
        async def search(
            self, organization_id: UUID, vector: list[float], revision: str, limit: int = 5
        ) -> list[object]:
            return [object()]

    class Prompt:
        def build(self, question: str, chunks: list[object]) -> object:
            prompt_started.set()
            if not prompt_release.wait(1):
                raise RuntimeError("test prompt barrier timed out")
            prompt_finished.set()
            return SimpleNamespace(
                messages=[{"role": "user", "content": question}],
                sources=[],
                input_tokens=1,
            )

    class Inference:
        @asynccontextmanager
        async def open(
            self, messages: list[dict[str, str]]
        ) -> AsyncIterator[AsyncIterator[Delta | Completed]]:
            async def events() -> AsyncIterator[Delta | Completed]:
                yield Completed("stop", None)

            yield events()

    service = RagService(Embedding(), Retriever(), Prompt(), Inference(), prompt_limit=1)
    gate = Admission(1, 0, 1)

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        return await service.prepare(identity, "질문", stack)

    async def receive() -> dict[str, str]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        pytest.fail("a disconnected prompt build must not start a response")

    response = StreamResponse(prepare, gate, "request-9")
    response_task = asyncio.create_task(response({"type": "http"}, receive, send))
    while not prompt_started.is_set():
        await asyncio.sleep(0)

    disconnected.set()
    try:
        await asyncio.wait_for(response_task, 0.1)
        async with gate.slot():
            pass

        busy = await _run_response(prepare, gate)
        busy_body = b"".join(message.get("body", b"") for message in busy)
        assert busy[0]["status"] == 503
        assert json.loads(busy_body)["error"]["code"] == "prompt_busy"
        assert b"event:" not in busy_body
    finally:
        prompt_release.set()

    while not prompt_finished.is_set():
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    accepted = await _run_response(prepare, gate)
    assert accepted[0]["status"] == 200


async def test_prompt_thread_does_not_extend_execution_timeout_or_release_capacity() -> None:
    from app.rag.service import RagService

    identity = Identity(
        UUID("22222222-2222-2222-2222-222222222222"),
        UUID("11111111-1111-1111-1111-111111111111"),
    )
    prompt_started = threading.Event()
    prompt_release = threading.Event()
    prompt_finished = asyncio.Event()
    loop = asyncio.get_running_loop()

    class Embedding:
        revision = "revision-42"

        async def embed_query(self, question: str) -> list[float]:
            return [1.0]

    class Retriever:
        async def search(
            self, organization_id: UUID, vector: list[float], revision: str, limit: int = 5
        ) -> list[object]:
            return [object()]

    class Prompt:
        def build(self, question: str, chunks: list[object]) -> object:
            prompt_started.set()
            if not prompt_release.wait(1):
                raise RuntimeError("test prompt barrier timed out")
            loop.call_soon_threadsafe(prompt_finished.set)
            return SimpleNamespace(messages=[], sources=[], input_tokens=1)

    service = RagService(Embedding(), Retriever(), Prompt(), object(), prompt_limit=1)
    gate = Admission(1, 0, 1)

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        return await service.prepare(identity, "질문", stack)

    messages: list[dict[str, object]] = []

    async def receive() -> dict[str, str]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    response = StreamResponse(
        prepare,
        gate,
        "request-9",
        execution_seconds=0.1,
        send_seconds=1,
    )
    response_task = asyncio.create_task(response({"type": "http"}, receive, send))
    while not prompt_started.is_set():
        await asyncio.sleep(0)

    try:
        await asyncio.wait_for(response_task, 1)
        body = b"".join(message.get("body", b"") for message in messages)
        assert messages[0]["status"] == 504
        assert json.loads(body)["error"]["code"] == "execution_timeout"

        busy = await _run_response(prepare, gate)
        busy_body = b"".join(message.get("body", b"") for message in busy)
        assert busy[0]["status"] == 503
        assert json.loads(busy_body)["error"]["code"] == "prompt_busy"
    finally:
        prompt_release.set()

    await asyncio.wait_for(prompt_finished.wait(), 1)


async def test_disconnect_after_inference_open_closes_upstream_and_returns_admission() -> None:
    from app.rag.service import RagService

    identity = Identity(
        UUID("22222222-2222-2222-2222-222222222222"),
        UUID("11111111-1111-1111-1111-111111111111"),
    )
    inference_opened = asyncio.Event()
    inference_closed = asyncio.Event()
    disconnected = asyncio.Event()

    class Embedding:
        revision = "revision-42"

        async def embed_query(self, question: str) -> list[float]:
            return [1.0]

    class Retriever:
        async def search(
            self, organization_id: UUID, vector: list[float], revision: str, limit: int = 5
        ) -> list[object]:
            return [object()]

    class Prompt:
        def build(self, question: str, chunks: list[object]) -> object:
            return SimpleNamespace(
                messages=[{"role": "user", "content": question}],
                sources=[{"source_id": "S1"}],
                input_tokens=10,
            )

    class Inference:
        @asynccontextmanager
        async def open(
            self, messages: list[dict[str, str]]
        ) -> AsyncIterator[AsyncIterator[Delta | Completed]]:
            async def events() -> AsyncIterator[Delta | Completed]:
                await asyncio.Event().wait()
                yield Delta("never")

            try:
                inference_opened.set()
                yield events()
            finally:
                inference_closed.set()

    service = RagService(Embedding(), Retriever(), Prompt(), Inference())
    gate = Admission(1, 0, 1)

    async def prepare(stack: AsyncExitStack) -> PreparedStream:
        return await service.prepare(identity, "질문", stack)

    async def receive() -> dict[str, str]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    response = StreamResponse(prepare, gate, "request-9")
    task = asyncio.create_task(response({"type": "http"}, receive, send))
    await inference_opened.wait()
    disconnected.set()
    await asyncio.wait_for(task, 1)

    assert inference_closed.is_set()
    async with gate.slot():
        pass


@pytest.mark.parametrize("limit", [0, -1, True])
def test_prompt_capacity_must_be_a_positive_integer(limit: int) -> None:
    from app.rag.service import RagService

    with pytest.raises(ValueError):
        RagService(object(), object(), object(), object(), prompt_limit=limit)
