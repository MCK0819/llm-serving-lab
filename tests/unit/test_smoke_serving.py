import json

import httpx
import pytest

from app.core.errors import AppError
from tests.fakes.streams import ControlledStream


def json_response(payload: object) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        stream=ControlledStream([json.dumps(payload).encode()]),
    )


def inference_wire(text: str = "안녕하세요") -> bytes:
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    return (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    ).encode()


async def test_inference_smoke_checks_completion_without_printing_answer() -> None:
    from scripts.smoke_inference import probe

    stream = ControlledStream([inference_wire("private-answer")])
    async with httpx.AsyncClient(
        base_url="http://localhost",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
    ) as http:
        result = await probe(http, "test-model", cancel=False)
    assert result["status"] == "ok"
    assert result["input_tokens"] is None
    assert result["first_text_ms"] >= 0
    assert "private-answer" not in json.dumps(result)
    assert stream.closed


async def test_inference_smoke_rejects_empty_answer() -> None:
    from scripts.smoke_inference import probe

    async with httpx.AsyncClient(
        base_url="http://localhost",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=ControlledStream([inference_wire("")]),
            )
        ),
    ) as http:
        with pytest.raises(AppError):
            await probe(http, "test-model", cancel=False)


async def test_cancel_probe_closes_connection_without_claiming_gpu_cancellation() -> None:
    from scripts.smoke_inference import probe

    stream = ControlledStream([inference_wire()], block=True)
    async with httpx.AsyncClient(
        base_url="http://localhost",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=stream
            )
        ),
    ) as http:
        result = await probe(http, "test-model", cancel=True)
    assert stream.closed
    assert result["status"] == "client_cancelled"
    assert result["gpu_cancellation_verified"] is False


async def test_embedding_smoke_verifies_model_prefixes_and_normalized_dimensions() -> None:
    from scripts.smoke_embedding import probe

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return json_response({"model_id": "test-model"})
        body = json.loads(request.content)
        assert body["inputs"][0].startswith("query: ")
        assert body["inputs"][1].startswith("passage: ")
        assert body["normalize"] is True
        assert body["truncate"] is False
        return json_response([[1.0] + [0.0] * 383] * 2)

    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(upstream)
    ) as http:
        result = await probe(http, "test-model")
    assert result["status"] == "ok"
    assert result["dimensions"] == 384
    assert result["vectors"] == 2
    assert "휴가" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    "vector", [[0.0] * 384, [1.0], [float("nan")] * 384, [True] * 384, [10**200] + [0] * 383]
)
async def test_embedding_smoke_rejects_invalid_vectors(vector: list) -> None:
    from scripts.smoke_embedding import probe

    def upstream(request: httpx.Request) -> httpx.Response:
        payload = {"model_id": "test-model"} if request.url.path == "/info" else [vector, vector]
        return json_response(payload)

    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(upstream)
    ) as http:
        with pytest.raises(AppError):
            await probe(http, "test-model")


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://user:secret@example.com",
        "https://example.com?key=secret",
        "file:///tmp/model",
    ],
)
def test_smoke_rejects_unsafe_endpoint(url: str) -> None:
    from scripts.smoke_http import validate_endpoint

    with pytest.raises(ValueError):
        validate_endpoint(url)


@pytest.mark.parametrize("module", ["scripts.smoke_inference", "scripts.smoke_embedding"])
def test_cli_failure_is_nonzero_json_without_credentials(module: str) -> None:
    import subprocess
    import sys

    output = subprocess.run(
        [sys.executable, "-m", module, "--endpoint", "http://user:secret-marker@remote.example"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert output.returncode == 1
    assert json.loads(output.stdout)["status"] == "failed"
    assert "secret-marker" not in output.stdout + output.stderr


def test_cli_total_deadline_stops_stalled_probe(capsys: pytest.CaptureFixture[str]) -> None:
    import asyncio

    from scripts.smoke_http import run_command

    async def stalled(http: httpx.AsyncClient, model: str):
        await asyncio.Event().wait()

    assert (
        run_command(
            stalled,
            "test-model",
            "SMOKE_TEST_URL",
            ["--endpoint", "http://localhost", "--deadline", "0.01"],
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["code"] == "probe_timeout"
