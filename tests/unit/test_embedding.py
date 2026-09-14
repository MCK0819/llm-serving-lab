import httpx
import pytest

from app.core.errors import AppError


class BodyStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body
        self.closed = False

    async def __aiter__(self):
        yield self.body

    async def aclose(self):
        self.closed = True


def json_response(payload):
    import json

    return httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        stream=BodyStream(json.dumps(payload).encode()),
    )


class CharacterTokenizer:
    def count(self, text):
        return len(text) + 2

    def offsets(self, text):
        return [(index, index + 1) for index in range(len(text))]


@pytest.fixture
def recorded_requests():
    return []


@pytest.fixture
async def embedding_client(recorded_requests):
    import json

    from app.rag.embedding import EmbeddingClient

    def handle(request):
        payload = json.loads(request.content)
        recorded_requests.append(payload)
        return json_response([[1.0] + [0.0] * 383 for _ in payload["inputs"]])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://tei"
    ) as http:
        yield EmbeddingClient(
            http, CharacterTokenizer(), "614241f622f53c4eeff9890bdc4f31cfecc418b3"
        )


async def test_passage_prefix_is_added_once(embedding_client, recorded_requests):
    vectors = await embedding_client.embed_passages(["휴가 규정"])
    assert recorded_requests[0]["inputs"] == ["passage: 휴가 규정"]
    assert recorded_requests[0]["truncate"] is False
    assert len(vectors) == 1
    assert vectors[0] == [1.0] + [0.0] * 383


async def test_query_and_user_prefixes_are_not_stripped(embedding_client, recorded_requests):
    vector = await embedding_client.embed_query("query: 휴가 규정")
    await embedding_client.embed_passages(["passage: 안내"])
    assert recorded_requests[0]["inputs"] == ["query: query: 휴가 규정"]
    assert recorded_requests[1]["inputs"] == ["passage: passage: 안내"]
    assert len(vector) == 384


async def test_batches_are_at_most_sixteen_without_losing_order(
    embedding_client, recorded_requests
):
    vectors = await embedding_client.embed_passages([str(index) for index in range(33)])
    assert [len(request["inputs"]) for request in recorded_requests] == [16, 16, 1]
    assert [text for request in recorded_requests for text in request["inputs"]] == [
        "passage: " + str(index) for index in range(33)
    ]
    assert len(vectors) == 33


async def test_special_tokens_and_prefix_are_included_in_input_limit(
    embedding_client, recorded_requests
):
    await embedding_client.embed_passages(["x" * 501])  # 9 prefix + 501 + 2 specials = 512
    with pytest.raises(AppError) as error:
        await embedding_client.embed_passages(["ok", "x" * 502])
    assert error.value.status == 422
    assert len(recorded_requests) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [[1.0] * 383],
        [[float("nan")] + [0.0] * 383],
        [[0.0] * 384],
        [[float("inf")] + [0.0] * 383],
        [[True] + [0.0] * 383],
        [[10**400] + [0] * 383],
        [["private data"] + [0] * 383],
        [],
        [[1.0] * 384] * 2,
        {"error": "private service response"},
    ],
)
async def test_invalid_vectors_are_rejected_without_exposing_upstream(payload):
    from app.rag.embedding import EmbeddingClient

    response = json_response(payload)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response), base_url="http://tei"
    ) as http:
        client = EmbeddingClient(http, CharacterTokenizer(), "test-revision")
        with pytest.raises(AppError) as error:
            await client.embed_query("질문")
    assert error.value.status == 502
    assert "private" not in str(error.value)
    assert response.is_closed


async def test_non_unit_finite_vector_is_normalized():
    import math

    from app.rag.embedding import EmbeddingClient

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: json_response([[3.0, 4.0] + [0] * 382])),
        base_url="http://tei",
    ) as http:
        vector = await EmbeddingClient(http, CharacterTokenizer(), "test-revision").embed_query(
            "질문"
        )
    assert vector[:2] == [0.6, 0.8]
    assert math.isclose(math.hypot(*vector), 1)


@pytest.mark.parametrize(
    "failure,status",
    [
        (httpx.ConnectError("private hostname"), 503),
        (httpx.ReadTimeout("private hostname"), 504),
        (httpx.RemoteProtocolError("private response"), 502),
    ],
)
async def test_transport_errors_are_safe(failure, status):
    from app.rag.embedding import EmbeddingClient

    def fail(_):
        raise failure

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fail), base_url="http://tei"
    ) as http:
        with pytest.raises(AppError) as error:
            await EmbeddingClient(http, CharacterTokenizer(), "test-revision").embed_query("질문")
    assert error.value.status == status
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "status,body,headers",
    [
        (500, b"private error", {"Content-Type": "application/json"}),
        (200, b"bad JSON", {"Content-Type": "application/json"}),
        (200, b"[]", {"Content-Type": "text/html"}),
        (200, b"x" * (256 * 1024 + 1), {"Content-Type": "application/json"}),
        (200, b"[]", {"Content-Type": "application/json", "Content-Encoding": "gzip"}),
    ],
    ids=["upstream500", "malformed_json", "wrong_media", "oversized_body", "compressed"],
)
async def test_invalid_http_response_closes_connection(status, body, headers):
    from app.rag.embedding import EmbeddingClient

    stream = BodyStream(body)
    response = httpx.Response(status, headers=headers, stream=stream)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response), base_url="http://tei"
    ) as http:
        with pytest.raises(AppError) as error:
            await EmbeddingClient(http, CharacterTokenizer(), "test-revision").embed_query("질문")
    assert error.value.status == 502
    assert stream.closed


async def test_timeout_while_reading_body_closes_response():
    import asyncio

    from app.rag.embedding import EmbeddingClient

    class WaitingStream(BodyStream):
        async def __aiter__(self):
            yield b"["
            await asyncio.Event().wait()

    stream = WaitingStream(b"")
    response = httpx.Response(200, headers={"Content-Type": "application/json"}, stream=stream)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response), base_url="http://tei"
    ) as http:
        client = EmbeddingClient(http, CharacterTokenizer(), "test-revision", request_timeout=0.02)
        with pytest.raises(AppError) as error:
            await client.embed_query("질문")
    assert error.value.status == 504
    assert stream.closed


async def test_cancellation_closes_embedding_connection():
    import asyncio

    from app.rag.embedding import EmbeddingClient

    started = asyncio.Event()

    class WaitingStream(BodyStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

    stream = WaitingStream(b"")
    response = httpx.Response(200, headers={"Content-Type": "application/json"}, stream=stream)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response), base_url="http://tei"
    ) as http:
        task = asyncio.create_task(
            EmbeddingClient(http, CharacterTokenizer(), "test-revision").embed_query("질문")
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


async def test_empty_batch_does_not_contact_server(embedding_client, recorded_requests):
    assert await embedding_client.embed_passages([]) == []
    assert recorded_requests == []


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_request_timeout_is_rejected(timeout):
    from app.rag.embedding import EmbeddingClient

    with pytest.raises(ValueError):
        EmbeddingClient(None, CharacterTokenizer(), "test-revision", request_timeout=timeout)
