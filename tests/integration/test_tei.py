import hashlib
import json
import math
import os
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration
E5_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"


def verified_cache(variable, model, revision):
    configured = os.environ.get(variable)
    if not configured:
        pytest.skip("Provide a local pinned tokenizer cache")
    root = Path(configured)
    manifest = json.loads((root / "tokenizer-manifest.json").read_text(encoding="utf-8"))
    assert manifest["model"] == model
    assert manifest["revision"] == revision
    assert "tokenizer.json" in manifest["files"]
    for filename, entry in manifest["files"].items():
        with (root / filename).open("rb") as handle:
            assert hashlib.file_digest(handle, "sha256").hexdigest() == entry["sha256"]
    return root


@pytest.fixture
def e5_tokenizer():
    from app.rag.tokenization import Tokenizer

    return Tokenizer.from_pretrained(
        verified_cache("LLM_LAB_TEST_E5_TOKENIZER", "intfloat/multilingual-e5-small", E5_REVISION)
    )


@pytest.fixture
async def tei_http():
    url = os.environ.get("LLM_LAB_TEST_TEI_URL")
    if not url:
        pytest.skip("Start the pinned CPU TEI container explicitly")
    async with httpx.AsyncClient(
        base_url=url, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(30, connect=5)
    ) as http:
        yield http


async def test_authored_korean_pdf_to_real_cpu_vectors(e5_tokenizer, tei_http):
    from app.documents.chunking import chunk_pages
    from app.documents.parsing import parse_pdf
    from app.rag.embedding import EmbeddingClient

    response = await tei_http.get("/info")
    response.raise_for_status()
    info = response.json()
    assert info["model_id"] == "intfloat/multilingual-e5-small"
    assert info["model_sha"] == E5_REVISION
    assert info["max_input_length"] == 512
    pages = parse_pdf(Path(__file__).parents[1] / "fixtures/pdfs/authored_korean.pdf")
    assert pages and any("휴가" in page.text for page in pages)
    chunks = chunk_pages(pages, e5_tokenizer)
    assert chunks
    assert all(e5_tokenizer.count(chunk.text) <= 300 for chunk in chunks)
    assert all(e5_tokenizer.count("passage: " + chunk.text) <= 512 for chunk in chunks)
    client = EmbeddingClient(tei_http, e5_tokenizer, E5_REVISION)
    vectors = await client.embed_passages([chunk.text for chunk in chunks])
    query = await client.embed_query("휴가를 어떻게 신청하나요?")
    assert len(vectors) == len(chunks)
    for vector in [query, *vectors]:
        assert len(vector) == 384
        assert all(math.isfinite(value) for value in vector)
        assert math.isclose(math.hypot(*vector), 1, abs_tol=1e-6)


async def test_local_e5_token_budget_agrees_with_server_rejection(e5_tokenizer, tei_http):
    from app.core.errors import AppError
    from app.rag.embedding import EmbeddingClient

    text = "가나다라마바사 " * 200
    assert e5_tokenizer.count("query: " + text) > 512
    with pytest.raises(AppError) as error:
        await EmbeddingClient(tei_http, e5_tokenizer, E5_REVISION).embed_query(text)
    assert error.value.status == 422
    response = await tei_http.post(
        "/embed",
        json={
            "inputs": ["query: " + text],
            "normalize": True,
            "truncate": False,
        },
    )
    assert response.status_code == 422


def test_actual_e5_korean_chunk_boundaries_cover_text(e5_tokenizer):
    from app.documents.chunking import chunk_pages
    from app.documents.parsing import PageText

    text = "".join(f"{index}번째 항목은 연차 신청 방법을 설명합니다. " for index in range(150))
    chunks = chunk_pages([PageText(1, text)], e5_tokenizer)
    assert len(chunks) > 1
    end = 0
    for chunk in chunks:
        start = text.find(chunk.text, max(0, end - 500))
        assert start >= 0
        assert not text[end:start].strip()
        assert start + len(chunk.text) > end
        assert e5_tokenizer.count(chunk.text) <= 300
        if start < end:
            assert len(e5_tokenizer.offsets(text[start:end])) <= 40
        end = start + len(chunk.text)
    assert not text[end:].strip()


def test_actual_qwen_chat_template_counts_wrapping_and_generation_prompt():
    from app.rag.tokenization import Tokenizer

    tokenizer = Tokenizer.from_pretrained(
        verified_cache(
            "LLM_LAB_TEST_QWEN_TOKENIZER",
            "Qwen/Qwen3-4B-Instruct-2507",
            "cdbee75f17c01a7cc42f958dc650907174af0554",
        )
    )
    messages = [
        {"role": "system", "content": "문서에 근거하여 답하세요."},
        {"role": "user", "content": "휴가 신청 방법은?"},
    ]
    count = tokenizer.count_messages(messages)
    assert count > sum(tokenizer.count(message["content"]) for message in messages)
    assert tokenizer.count_messages(messages) == count


@pytest.mark.parametrize("suffix", ["\u200b", "\ufeff", "\u200d"])
def test_actual_e5_normalized_away_suffix_is_preserved(e5_tokenizer, suffix):
    from app.documents.chunking import chunk_pages
    from app.documents.parsing import PageText

    chunks = chunk_pages([PageText(1, "abc" + suffix)], e5_tokenizer)
    assert len(chunks) == 1
    assert chunks[0].text == "abc" + suffix
