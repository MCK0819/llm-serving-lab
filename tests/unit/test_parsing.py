from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject

from app.core.errors import AppError
from app.documents import parsing
from app.documents.parsing import PageText, parse_pdf


def _write_pdf(path: Path, page_texts: list[str | None], *, password: str | None = None) -> None:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)

    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        if text is None:
            continue
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        page[NameObject("/Resources")] = resources
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content = StreamObject()
        content.set_data(f"BT\n/F1 12 Tf\n72 720 Td\n({escaped}) Tj\nET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)

    if password is not None:
        writer.encrypt(password)
    with path.open("wb") as file:
        writer.write(file)


def test_parse_pdf_returns_text_with_original_one_based_page_numbers(tmp_path: Path) -> None:
    pdf_path = tmp_path / "three-pages.pdf"
    _write_pdf(pdf_path, ["First page", None, "Third page"])

    pages = parse_pdf(pdf_path)

    assert pages == [PageText(page=1, text="First page"), PageText(page=3, text="Third page")]


def test_parse_pdf_extracts_authored_korean_fixture() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "pdfs" / "authored_korean.pdf"

    assert parse_pdf(fixture) == [PageText(page=1, text="연차 휴가는 사전에 신청합니다.")]


@pytest.mark.parametrize(
    ("contents", "expected_code"),
    [
        (b"%PDF-1.4\ncompletely broken", "invalid_pdf"),
        (b"not a PDF", "invalid_pdf"),
    ],
)
def test_parse_pdf_rejects_corrupt_content_safely(
    tmp_path: Path, contents: bytes, expected_code: str
) -> None:
    pdf_path = tmp_path / "sensitive-path.pdf"
    pdf_path.write_bytes(contents)

    with pytest.raises(AppError) as error:
        parse_pdf(pdf_path)

    assert error.value.status == 422
    assert error.value.code == expected_code
    assert "sensitive-path" not in error.value.message
    assert "sensitive-path" not in str(error.value)


@pytest.mark.parametrize("path_kind", ["missing", "directory"])
def test_parse_pdf_classifies_filesystem_failures_as_retryable_storage_errors(
    tmp_path: Path, path_kind: str
) -> None:
    pdf_path = tmp_path / "private-document-name.pdf"
    if path_kind == "directory":
        pdf_path.mkdir()

    with pytest.raises(AppError) as error:
        parse_pdf(pdf_path)

    assert (error.value.status, error.value.code) == (503, "storage_unavailable")
    assert "private-document-name" not in str(error.value)


def test_parse_pdf_does_not_convert_memory_exhaustion_to_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_path = tmp_path / "document.pdf"
    pdf_path.write_bytes(b"%PDF-1.4")

    def out_of_memory(source: object, *, strict: bool) -> None:
        raise MemoryError

    monkeypatch.setattr(parsing, "PdfReader", out_of_memory)

    with pytest.raises(MemoryError):
        parse_pdf(pdf_path)


@pytest.mark.parametrize("password", ["secret", ""])
def test_parse_pdf_rejects_encryption_even_with_empty_password(
    tmp_path: Path, password: str
) -> None:
    pdf_path = tmp_path / "encrypted.pdf"
    _write_pdf(pdf_path, ["Secret text"], password=password)

    with pytest.raises(AppError) as error:
        parse_pdf(pdf_path)

    assert (error.value.status, error.value.code) == (422, "encrypted_pdf")


def test_parse_pdf_rejects_no_extractable_text(tmp_path: Path) -> None:
    pdf_path = tmp_path / "blank.pdf"
    _write_pdf(pdf_path, [None, None])

    with pytest.raises(AppError) as error:
        parse_pdf(pdf_path)

    assert (error.value.status, error.value.code) == (422, "pdf_no_text")


def test_parse_pdf_accepts_exactly_100_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "100-pages.pdf"
    _write_pdf(pdf_path, [None] * 99 + ["Last page"])

    assert parse_pdf(pdf_path) == [PageText(page=100, text="Last page")]


def test_parse_pdf_rejects_101_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "101-pages.pdf"
    _write_pdf(pdf_path, ["First page"] + [None] * 100)

    with pytest.raises(AppError) as error:
        parse_pdf(pdf_path)

    assert (error.value.status, error.value.code) == (422, "pdf_page_limit")


@pytest.mark.parametrize(
    ("length", "allowed"),
    [(1_000_000, True), (1_000_001, False)],
)
def test_parse_pdf_enforces_actual_extracted_character_boundary(
    tmp_path: Path, length: int, allowed: bool
) -> None:
    pdf_path = tmp_path / "extracted-text-limit.pdf"
    text = "A" * length
    _write_pdf(pdf_path, [text])

    if allowed:
        assert parse_pdf(pdf_path) == [PageText(page=1, text=text)]
    else:
        with pytest.raises(AppError) as error:
            parse_pdf(pdf_path)
        assert (error.value.status, error.value.code) == (422, "pdf_text_limit")
