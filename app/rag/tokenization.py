from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, cast


class TextTokenizer(Protocol):
    """The tokenizer behavior needed to make safe text boundaries."""

    def count(self, text: str) -> int: ...

    def offsets(self, text: str) -> list[tuple[int, int]]: ...


class _TokenizerBackend(Protocol):
    def __call__(self, text: str, **kwargs: object) -> Mapping[str, object]: ...

    def apply_chat_template(
        self, messages: list[dict[str, str]], **kwargs: object
    ) -> list[int] | Mapping[str, object]: ...


class Tokenizer:
    """A no-truncation adapter around a locally available Hugging Face tokenizer."""

    def __init__(self, backend: _TokenizerBackend) -> None:
        self._backend = backend

    @classmethod
    def from_pretrained(cls, path: str | Path) -> Tokenizer:
        from transformers import AutoTokenizer

        backend = AutoTokenizer.from_pretrained(
            str(path),
            local_files_only=True,
            trust_remote_code=False,
            use_fast=True,
        )
        return cls(cast(_TokenizerBackend, backend))

    def count(self, text: str) -> int:
        encoded = self._backend(
            text,
            add_special_tokens=True,
            truncation=False,
            return_attention_mask=False,
            return_token_type_ids=False,
        )
        return len(_list_field(encoded, "input_ids"))

    def offsets(self, text: str) -> list[tuple[int, int]]:
        encoded = self._backend(
            text,
            add_special_tokens=False,
            truncation=False,
            return_attention_mask=False,
            return_token_type_ids=False,
            return_offsets_mapping=True,
        )
        raw_offsets = _list_field(encoded, "offset_mapping")
        offsets: list[tuple[int, int]] = []
        for item in raw_offsets:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or not isinstance(item[0], int)
                or not isinstance(item[1], int)
            ):
                raise TypeError("Tokenizer returned an invalid offset mapping")
            offsets.append((item[0], item[1]))
        return offsets

    def count_messages(self, messages: list[dict[str, str]]) -> int:
        tokens = self._backend.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
        )
        if isinstance(tokens, Mapping):
            return len(_list_field(tokens, "input_ids"))
        return len(tokens)


def _list_field(encoded: Mapping[str, object], name: str) -> list[object]:
    value = encoded.get(name)
    if not isinstance(value, list):
        raise TypeError(f"Tokenizer returned an invalid {name}")
    return value
