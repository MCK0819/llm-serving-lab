from pathlib import Path
from types import SimpleNamespace

import pytest


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.template_calls: list[tuple[list[dict[str, str]], dict[str, object]]] = []

    def __call__(self, text: str, **kwargs: object) -> dict[str, object]:
        self.calls.append((text, kwargs))
        if kwargs.get("return_offsets_mapping"):
            return {"input_ids": [10, 11], "offset_mapping": [(0, 1), (1, len(text))]}
        return {"input_ids": [101, 10, 11, 102]}

    def apply_chat_template(self, messages: list[dict[str, str]], **kwargs: object) -> list[int]:
        self.template_calls.append((messages, kwargs))
        return [1, 2, 3, 4, 5]


def test_count_includes_special_tokens_without_truncating() -> None:
    from app.rag.tokenization import Tokenizer

    backend = RecordingBackend()
    tokenizer = Tokenizer(backend)

    assert tokenizer.count("한글") == 4
    assert backend.calls == [
        (
            "한글",
            {
                "add_special_tokens": True,
                "truncation": False,
                "return_attention_mask": False,
                "return_token_type_ids": False,
            },
        )
    ]


def test_offsets_exclude_special_tokens_and_preserve_unicode_character_offsets() -> None:
    from app.rag.tokenization import Tokenizer

    backend = RecordingBackend()
    tokenizer = Tokenizer(backend)

    assert tokenizer.offsets("한글") == [(0, 1), (1, 2)]
    assert backend.calls == [
        (
            "한글",
            {
                "add_special_tokens": False,
                "truncation": False,
                "return_attention_mask": False,
                "return_token_type_ids": False,
                "return_offsets_mapping": True,
            },
        )
    ]


def test_count_messages_uses_generation_chat_template_exactly() -> None:
    from app.rag.tokenization import Tokenizer

    backend = RecordingBackend()
    tokenizer = Tokenizer(backend)
    messages = [{"role": "user", "content": "질문"}]

    assert tokenizer.count_messages(messages) == 5
    assert backend.template_calls == [(messages, {"add_generation_prompt": True, "tokenize": True})]


def test_count_messages_counts_input_ids_from_a_batch_encoding() -> None:
    from app.rag.tokenization import Tokenizer

    class MappingTemplateBackend(RecordingBackend):
        def apply_chat_template(
            self, messages: list[dict[str, str]], **kwargs: object
        ) -> dict[str, object]:
            return {"input_ids": [1, 2, 3, 4, 5], "attention_mask": [1, 1, 1, 1, 1]}

    tokenizer = Tokenizer(MappingTemplateBackend())

    assert tokenizer.count_messages([{"role": "user", "content": "질문"}]) == 5


def test_count_messages_does_not_approximate_when_template_is_missing() -> None:
    from app.rag.tokenization import Tokenizer

    class MissingTemplateBackend(RecordingBackend):
        def apply_chat_template(self, messages, **kwargs):
            raise ValueError("chat template is not set")

    tokenizer = Tokenizer(MissingTemplateBackend())

    with pytest.raises(ValueError, match="chat template"):
        tokenizer.count_messages([{"role": "user", "content": "질문"}])


def test_from_pretrained_loads_only_a_local_fast_tokenizer(monkeypatch, tmp_path: Path) -> None:
    from app.rag.tokenization import Tokenizer

    backend = RecordingBackend()
    recorded: list[tuple[str, dict[str, object]]] = []

    class AutoTokenizer:
        @staticmethod
        def from_pretrained(path: str, **kwargs: object) -> RecordingBackend:
            recorded.append((path, kwargs))
            return backend

    monkeypatch.setitem(
        __import__("sys").modules,
        "transformers",
        SimpleNamespace(AutoTokenizer=AutoTokenizer),
    )

    tokenizer = Tokenizer.from_pretrained(tmp_path)

    assert tokenizer.count("x") == 4
    assert recorded == [
        (
            str(tmp_path),
            {"local_files_only": True, "trust_remote_code": False, "use_fast": True},
        )
    ]
