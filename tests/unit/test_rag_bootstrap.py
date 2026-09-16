from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.settings import Settings


def test_rag_requires_both_local_tokenizers_and_database(monkeypatch):
    monkeypatch.delenv("LLM_LAB_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(rag_embedding_tokenizer_path=Path("e5"))
    with pytest.raises(ValidationError):
        Settings(
            rag_embedding_tokenizer_path=Path("e5"), rag_generation_tokenizer_path=Path("qwen")
        )
    settings = Settings(
        database_url="postgresql+psycopg://localhost/test",
        rag_embedding_tokenizer_path=Path("e5"),
        rag_generation_tokenizer_path=Path("qwen"),
    )
    assert settings.rag_generation_tokenizer_path == Path("qwen")


async def test_rag_resources_are_loaded_once_and_closed_by_lifespan(monkeypatch):
    from fastapi import FastAPI

    from app.bootstrap import application_lifespan, build_services
    from app.rag.tokenization import Tokenizer

    loaded = []

    def load(path):
        loaded.append(path)
        return object()

    monkeypatch.setattr(Tokenizer, "from_pretrained", load)
    settings = Settings(
        database_url="postgresql+psycopg://localhost/test",
        rag_embedding_tokenizer_path=Path("e5"),
        rag_generation_tokenizer_path=Path("qwen"),
    )
    services = build_services(settings)
    app = FastAPI()
    assert services.rag is None
    async with application_lifespan(settings, services)(app):
        assert loaded == [Path("e5"), Path("qwen")]
        rag = services.rag
        assert rag is not None
        assert not rag.embedding.http.is_closed
        assert not rag.inference.http.is_closed
    assert rag.embedding.http.is_closed
    assert rag.inference.http.is_closed
    assert services.rag is None


async def test_failed_tokenizer_startup_closes_inference_and_disposes_database(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import FastAPI

    from app.bootstrap import application_lifespan, build_services
    from app.rag.tokenization import Tokenizer

    def failed_load(path):
        raise OSError("PRIVATE_CACHE_PATH")

    monkeypatch.setattr(Tokenizer, "from_pretrained", failed_load)
    settings = Settings(
        database_url="postgresql+psycopg://localhost/test",
        rag_embedding_tokenizer_path=Path("e5"),
        rag_generation_tokenizer_path=Path("qwen"),
    )
    services = build_services(settings)
    dispose = AsyncMock()
    monkeypatch.setattr(type(services.engine), "dispose", dispose)
    app = FastAPI()
    with pytest.raises(RuntimeError, match="Local RAG tokenizers could not be loaded") as error:
        async with application_lifespan(settings, services)(app):
            pytest.fail("startup must fail")
    assert "PRIVATE_CACHE_PATH" not in str(error.value)
    assert services.rag is None
    assert app.state.inference.http.is_closed
    dispose.assert_awaited_once()
