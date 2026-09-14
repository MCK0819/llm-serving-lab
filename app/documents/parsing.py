"""Bounded PDF text extraction for the document worker."""

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from app.core.errors import AppError

MAX_PAGES = 100
MAX_EXTRACTED_CHARACTERS = 1_000_000


@dataclass(frozen=True)
class PageText:
    page: int
    text: str


def parse_pdf(path: Path) -> list[PageText]:
    """Extract text, preserving PDF page numbers and rejecting unsafe inputs."""
    try:
        with path.open("rb") as source:
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                raise AppError(422, "encrypted_pdf", "암호화된 PDF는 처리할 수 없습니다.")
            if len(reader.pages) > MAX_PAGES:
                raise AppError(422, "pdf_page_limit", "PDF 페이지 수 제한을 초과했습니다.")

            pages: list[PageText] = []
            total_characters = 0
            for page_number, page in enumerate(reader.pages, start=1):
                text = page.extract_text() or ""
                total_characters += len(text)
                if total_characters > MAX_EXTRACTED_CHARACTERS:
                    raise AppError(422, "pdf_text_limit", "PDF 텍스트 크기 제한을 초과했습니다.")
                if text.strip():
                    pages.append(PageText(page=page_number, text=text))
            if not pages:
                raise AppError(422, "pdf_no_text", "PDF에서 텍스트를 찾을 수 없습니다.")
            return pages
    except AppError:
        raise
    except MemoryError:
        raise
    except OSError:
        raise AppError(503, "storage_unavailable", "PDF 파일을 읽을 수 없습니다.") from None
    except Exception:
        raise AppError(422, "invalid_pdf", "PDF를 처리할 수 없습니다.") from None
