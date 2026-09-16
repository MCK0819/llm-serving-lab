import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app.core.settings import Settings
from app.documents.models import Chunk, Document
from tests.fakes.streams import ControlledStream
from tests.integration.test_auth import accounts  # noqa: F401

pytestmark = pytest.mark.integration


async def test_default_rag_route_is_tenant_scoped_and_deletion_affects_new_questions(
    accounts,  # noqa: F811
    monkeypatch,
    caplog,
):
    from app.main import create_app
    from app.rag.tokenization import Tokenizer
    from app.users.service import AuthService

    repository, orgs, users = accounts
    revision = Settings().embedding_revision
    document_ids = [uuid4(), uuid4()]
    async with repository.sessions.begin() as session:
        for org, user, identifier, content in zip(
            orgs,
            [users[0], users[2]],
            document_ids,
            ["연차 휴가는 사전에 신청합니다.", "OTHER_TENANT_SECRET"],
            strict=True,
        ):
            session.add(
                Document(
                    id=identifier,
                    organization_id=org,
                    uploader_id=user,
                    filename="규정.pdf",
                    storage_path=f"{identifier}.pdf",
                    status="ready",
                )
            )
            await session.flush()
            session.add(
                Chunk(
                    document_id=identifier,
                    organization_id=org,
                    ordinal=0,
                    page=2,
                    text=content,
                    embedding=[1.0] + [0.0] * 383,
                    embedding_revision=revision,
                )
            )
    key = await AuthService(repository).issue_key(users[0])

    class TokenCounter:
        def count(self, value):
            return len(value)

        def count_messages(self, messages):
            return sum(len(message["content"]) for message in messages)

    monkeypatch.setattr(Tokenizer, "from_pretrained", lambda path: TokenCounter())
    generation_requests = []
    streams = []

    async def upstream(request):
        payload = json.loads(request.content)
        if request.url.path == "/embed":
            assert payload["inputs"] == ["query: 휴가 규정"]
            stream = ControlledStream([json.dumps([[1.0] + [0.0] * 383]).encode()])
            streams.append(stream)
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)
        assert request.url.path == "/v1/chat/completions"
        generation_requests.append(payload)
        assert "OTHER_TENANT_SECRET" not in request.content.decode()
        assert "연차 휴가는 사전에 신청합니다." in request.content.decode()
        # Deletion after authorized context acquisition permits this response only.
        async with repository.sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE documents SET status='deleted', deleted_at=clock_timestamp() "
                    "WHERE id=:id"
                ),
                {"id": document_ids[0]},
            )
        wire = (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "사전에 신청하세요. [S1]"},
                            "finish_reason": "stop",
                        }
                    ]
                },
                ensure_ascii=False,
            )
            + "\n\ndata: [DONE]\n\n"
        ).encode()
        stream = ControlledStream([wire])
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    original_client = httpx.AsyncClient

    def internal_client(*args, **kwargs):
        if str(kwargs.get("base_url", "")).startswith("http://mock-"):
            kwargs["transport"] = httpx.MockTransport(upstream)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", internal_client)
    app = create_app(
        Settings(
            database_url=os.environ["LLM_LAB_TEST_DATABASE_URL"],
            inference_url="http://mock-inference",
            embedding_url="http://mock-embedding",
            rag_embedding_tokenizer_path=Path("e5"),
            rag_generation_tokenizer_path=Path("qwen"),
        )
    )
    async with app.router.lifespan_context(app):
        async with original_client(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {key}"}
            first = await client.post("/questions", json={"question": "휴가 규정"}, headers=headers)
            assert first.status_code == 200
            assert first.text.count("event: sources") == 1
            assert str(document_ids[0]) in first.text
            assert str(document_ids[1]) not in first.text
            assert '"finish_reason": "stop"' in first.text
            second = await client.post(
                "/questions", json={"question": "휴가 규정"}, headers=headers
            )
            assert second.status_code == 200
            assert '"sources": []' in second.text
            assert '"finish_reason": "no_context"' in second.text
            rejected = await client.post(
                "/questions", json={"question": "휴가 규정", "model": "override"}, headers=headers
            )
            assert rejected.status_code == 422
    assert len(generation_requests) == 1
    assert generation_requests[0]["model"] == "Qwen/Qwen3-4B-Instruct-2507"
    assert generation_requests[0]["max_tokens"] == 512
    assert all(stream.closed for stream in streams)
    assert key not in caplog.text
    assert "OTHER_TENANT_SECRET" not in caplog.text
    assert "연차 휴가는 사전에 신청합니다." not in caplog.text
