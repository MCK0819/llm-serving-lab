import json
from uuid import uuid4

import pytest

from app.rag.prompt import PromptBuilder
from app.rag.retrieval import SourceChunk
from app.rag.tokenization import Tokenizer
from tests.integration.test_tei import verified_cache

pytestmark = pytest.mark.integration


def test_real_qwen_template_trims_korean_context_and_reports_exact_sources():
    tokenizer = Tokenizer.from_pretrained(
        verified_cache(
            "LLM_LAB_TEST_QWEN_TOKENIZER",
            "Qwen/Qwen3-4B-Instruct-2507",
            "cdbee75f17c01a7cc42f958dc650907174af0554",
        )
    )
    chunks = [
        SourceChunk(
            uuid4(),
            index,
            "한국어 규정.pdf",
            index + 1,
            "연차 휴가는 사전에 신청하고 담당자의 승인을 받아야 합니다. " * 100,
        )
        for index in range(5)
    ]
    result = PromptBuilder(tokenizer).build("연차 신청 절차는 무엇인가요?", chunks)
    assert 0 < len(result.sources) < len(chunks)
    assert result.input_tokens == tokenizer.count_messages(result.messages) <= 3584
    payload = json.loads(result.messages[1]["content"])
    assert [entry["document_id"] for entry in payload["context"]] == [
        str(chunk.document_id) for chunk in chunks[: len(result.sources)]
    ]
    assert [entry["source_id"] for entry in payload["context"]] == [
        entry["source_id"] for entry in result.sources
    ]
