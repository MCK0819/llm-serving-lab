from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.errors import AppError
from app.rag.tokenization import TextTokenizer

if TYPE_CHECKING:
    from app.documents.parsing import PageText


DEFAULT_MAX_TOKENS = 300
DEFAULT_OVERLAP_TOKENS = 40
DEFAULT_MAX_CHUNKS = 5_000

_PARAGRAPH_BREAK = re.compile(r"\r?\n[ \t]*\r?\n+")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?。！？])(?=\s|$)")


@dataclass(frozen=True, slots=True)
class Chunk:
    ordinal: int
    page: int
    text: str


def chunk_pages(
    pages: Iterable[PageText],
    tokenizer: TextTokenizer,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[Chunk]:
    """Split page text into bounded chunks without carrying overlap across pages."""

    _validate_limits(max_tokens, overlap_tokens, max_chunks)
    chunks: list[Chunk] = []
    for page in pages:
        _append_page_chunks(
            chunks,
            page.page,
            page.text,
            tokenizer,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            max_chunks=max_chunks,
        )
    return chunks


def _append_page_chunks(
    chunks: list[Chunk],
    page: int,
    text: str,
    tokenizer: TextTokenizer,
    *,
    max_tokens: int,
    overlap_tokens: int,
    max_chunks: int,
) -> None:
    page_start, page_end = _content_bounds(text)
    if page_start == page_end:
        return

    offsets = _checked_offsets(text, tokenizer)
    token_offsets = [(left, right) for left, right in offsets if right > left]
    token_starts = [left for left, _ in token_offsets]
    token_ends = sorted(end for _, end in token_offsets if page_start < end <= page_end)
    start = page_start
    consumed_end = page_start

    while consumed_end < page_end:
        start = _skip_whitespace(text, start, page_end)
        hard_end = _bounded_end(text, start, page_end, token_ends, tokenizer, max_tokens)
        end = _preferred_end(text, start, hard_end, consumed_end)
        end = _trim_trailing_whitespace(text, start, end)
        if end <= consumed_end or tokenizer.count(text[start:end]) > max_tokens:
            end = hard_end
        if end <= consumed_end:
            raise _invalid_text()

        chunk_text = text[start:end]
        if tokenizer.count(f"passage: {chunk_text}") > 512:
            raise _invalid_text()

        if len(chunks) >= max_chunks:
            raise AppError(422, "document_too_large", "문서 처리 한도를 초과했습니다.")
        chunks.append(Chunk(ordinal=len(chunks), page=page, text=chunk_text))
        consumed_end = end
        if consumed_end >= page_end:
            return
        start = _overlap_start(
            text, token_offsets, token_starts, start, end, overlap_tokens, tokenizer
        )


def _bounded_end(
    text: str,
    start: int,
    page_end: int,
    token_ends: list[int],
    tokenizer: TextTokenizer,
    max_tokens: int,
) -> int:
    index = bisect_right(token_ends, start)
    candidates = sorted(set(token_ends[index : index + max_tokens]))
    if len(token_ends) - index <= max_tokens:
        candidates.append(page_end)
        candidates = sorted(set(candidates))
    candidates = [candidate for candidate in candidates if candidate <= page_end]
    if not candidates:
        raise _invalid_text()

    low = 0
    high = len(candidates)
    bounded_end: int | None = None
    while low < high:
        middle = (low + high) // 2
        candidate = candidates[middle]
        if tokenizer.count(text[start:candidate]) <= max_tokens:
            bounded_end = candidate
            low = middle + 1
        else:
            high = middle
    if bounded_end is None:
        raise _invalid_text()
    return bounded_end


def _preferred_end(text: str, start: int, hard_end: int, consumed_end: int) -> int:
    window = text[start:hard_end]
    paragraphs = [
        start + match.start()
        for match in _PARAGRAPH_BREAK.finditer(window)
        if start + match.start() > consumed_end
    ]
    if paragraphs:
        return paragraphs[-1]
    sentences = [
        start + match.start()
        for match in _SENTENCE_BREAK.finditer(window)
        if start + match.start() > consumed_end
    ]
    return sentences[-1] if sentences else hard_end


def _overlap_start(
    text: str,
    offsets: list[tuple[int, int]],
    token_starts: list[int],
    start: int,
    end: int,
    overlap_tokens: int,
    tokenizer: TextTokenizer,
) -> int:
    if overlap_tokens == 0:
        return end

    first = bisect_right(token_starts, start)
    after_last = bisect_left(token_starts, end)
    candidates = sorted(
        {left for left, right in offsets[first:after_last] if right <= end and right > left}
    )
    low = 0
    high = len(candidates)
    while low < high:
        middle = (low + high) // 2
        if tokenizer.count(text[candidates[middle] : end]) <= overlap_tokens:
            high = middle
        else:
            low = middle + 1
    if low == len(candidates):
        return end
    candidate = candidates[low]
    return candidate if tokenizer.count(text[candidate:end]) <= overlap_tokens else end


def _checked_offsets(text: str, tokenizer: TextTokenizer) -> list[tuple[int, int]]:
    try:
        offsets = tokenizer.offsets(text)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _invalid_text() from exc
    if any(
        not isinstance(left, int)
        or not isinstance(right, int)
        or left < 0
        or right < left
        or right > len(text)
        for left, right in offsets
    ):
        raise _invalid_text()
    return sorted(offsets)


def _content_bounds(text: str) -> tuple[int, int]:
    start = _skip_whitespace(text, 0, len(text))
    end = _trim_trailing_whitespace(text, start, len(text))
    return start, end


def _skip_whitespace(text: str, start: int, end: int) -> int:
    while start < end and text[start].isspace():
        start += 1
    return start


def _trim_trailing_whitespace(text: str, start: int, end: int) -> int:
    while end > start and text[end - 1].isspace():
        end -= 1
    return end


def _validate_limits(max_tokens: int, overlap_tokens: int, max_chunks: int) -> None:
    if max_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= max_tokens or max_chunks <= 0:
        raise ValueError("Chunk limits must be positive and overlap must be below the token limit")


def _invalid_text() -> AppError:
    return AppError(422, "invalid_document_text", "문서 텍스트를 처리할 수 없습니다.")
