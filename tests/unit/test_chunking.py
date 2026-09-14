from dataclasses import dataclass

import pytest

from app.core.errors import AppError


@dataclass(frozen=True)
class Page:
    page: int
    text: str


class CharacterTokenizer:
    """Each character is one content token; count also includes two specials."""

    def count(self, text: str) -> int:
        return len(text) + 2

    def offsets(self, text: str) -> list[tuple[int, int]]:
        return [(index, index + 1) for index in range(len(text))]


def test_chunks_stay_on_their_page_and_ordinals_span_all_pages() -> None:
    from app.documents.chunking import chunk_pages

    chunks = chunk_pages(
        [Page(1, "abcdefghij"), Page(2, "KLMNOP")],
        CharacterTokenizer(),
        max_tokens=7,
        overlap_tokens=4,
    )

    assert [(chunk.ordinal, chunk.page, chunk.text) for chunk in chunks] == [
        (0, 1, "abcde"),
        (1, 1, "defgh"),
        (2, 1, "ghij"),
        (3, 2, "KLMNO"),
        (4, 2, "NOP"),
    ]


def test_paragraph_then_sentence_boundaries_are_preferred() -> None:
    from app.documents.chunking import chunk_pages

    text = "첫 문장. 둘째 문장.\n\n새 문단은 길다"
    chunks = chunk_pages([Page(1, text)], CharacterTokenizer(), max_tokens=18, overlap_tokens=0)

    assert [chunk.text for chunk in chunks] == ["첫 문장. 둘째 문장.", "새 문단은 길다"]


def test_long_korean_text_has_no_missing_non_whitespace_and_respects_budget() -> None:
    from app.documents.chunking import chunk_pages

    text = "가나다라마바사아자차카타파하라마바사아자차카타파하"
    tokenizer = CharacterTokenizer()
    chunks = chunk_pages([Page(1, text)], tokenizer, max_tokens=10, overlap_tokens=3)

    assert all(tokenizer.count(chunk.text) <= 10 for chunk in chunks)
    assert all(tokenizer.count(chunks[index + 1].text[:1]) <= 3 for index in range(len(chunks) - 1))
    reconstructed = chunks[0].text + "".join(chunk.text[1:] for chunk in chunks[1:])
    assert reconstructed == text


def test_repeated_zero_width_offsets_still_advance() -> None:
    from app.documents.chunking import chunk_pages

    class ByteFallbackTokenizer(CharacterTokenizer):
        def count(self, text: str) -> int:
            return len(text) * 2 + 2

        def offsets(self, text: str) -> list[tuple[int, int]]:
            offsets: list[tuple[int, int]] = []
            for index in range(len(text)):
                offsets.extend([(index, index), (index, index + 1), (index, index + 1)])
            return offsets

    chunks = chunk_pages(
        [Page(1, "가나다라마바사")],
        ByteFallbackTokenizer(),
        max_tokens=10,
        overlap_tokens=4,
    )

    assert [chunk.text for chunk in chunks] == ["가나다라", "라마바사"]


def test_prefixed_embedding_input_must_fit_the_512_token_limit() -> None:
    from app.documents.chunking import chunk_pages

    class PrefixSensitiveTokenizer(CharacterTokenizer):
        def count(self, text: str) -> int:
            return 513 if text.startswith("passage: ") else super().count(text)

    with pytest.raises(AppError) as error:
        chunk_pages([Page(1, "내용")], PrefixSensitiveTokenizer())

    assert error.value.status == 422


def test_whitespace_only_pages_do_not_create_chunks() -> None:
    from app.documents.chunking import chunk_pages

    assert chunk_pages([Page(1, " \n\t ")], CharacterTokenizer()) == []


@pytest.mark.parametrize(
    ("max_tokens", "overlap_tokens", "max_chunks"),
    [(0, 0, 1), (3, -1, 1), (3, 3, 1), (3, 0, 0)],
)
def test_invalid_chunk_limits_are_rejected(
    max_tokens: int, overlap_tokens: int, max_chunks: int
) -> None:
    from app.documents.chunking import chunk_pages

    with pytest.raises(ValueError):
        chunk_pages(
            [Page(1, "text")],
            CharacterTokenizer(),
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
            max_chunks=max_chunks,
        )


def test_tokenizer_that_cannot_expose_a_progressing_boundary_is_rejected() -> None:
    from app.documents.chunking import chunk_pages

    class UnusableTokenizer(CharacterTokenizer):
        def offsets(self, text: str) -> list[tuple[int, int]]:
            return [(0, 0)] * len(text)

    with pytest.raises(AppError) as error:
        chunk_pages(
            [Page(1, "oversized")],
            UnusableTokenizer(),
            max_tokens=4,
            overlap_tokens=0,
        )

    assert error.value.status == 422


def test_raw_unicode_suffix_ignored_by_tokenizer_offsets_is_preserved() -> None:
    from app.documents.chunking import chunk_pages

    class NormalizingTokenizer(CharacterTokenizer):
        def count(self, text: str) -> int:
            return len(text.replace("\u200b", "")) + 2

        def offsets(self, text: str) -> list[tuple[int, int]]:
            return [
                (index, index + 1) for index, character in enumerate(text) if character != "\u200b"
            ]

    chunks = chunk_pages(
        [Page(1, "abc\u200b")], NormalizingTokenizer(), max_tokens=5, overlap_tokens=0
    )

    assert [chunk.text for chunk in chunks] == ["abc\u200b"]


def test_chunk_cap_rejects_documents_instead_of_returning_partial_content() -> None:
    from app.documents.chunking import chunk_pages

    with pytest.raises(AppError) as error:
        chunk_pages(
            [Page(1, "abcdefghij")],
            CharacterTokenizer(),
            max_tokens=4,
            overlap_tokens=0,
            max_chunks=4,
        )

    assert error.value.status == 422


def test_large_page_token_counting_is_limited_to_local_chunk_windows() -> None:
    from app.documents.chunking import chunk_pages

    class WorkRecordingTokenizer(CharacterTokenizer):
        def __init__(self) -> None:
            self.counted_lengths: list[int] = []

        def count(self, text: str) -> int:
            self.counted_lengths.append(len(text))
            return super().count(text)

    tokenizer = WorkRecordingTokenizer()
    chunks = chunk_pages([Page(1, "가" * 10_000)], tokenizer)

    assert len(chunks) < 50
    assert max(tokenizer.counted_lengths) <= 512
