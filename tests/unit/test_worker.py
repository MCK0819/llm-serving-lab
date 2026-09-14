from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.errors import AppError


@dataclass(frozen=True)
class Lease:
    document_id: UUID
    organization_id: UUID
    attempt_id: UUID
    attempt_count: int
    storage_path: str


class Tokenizer:
    def count(self, text):
        return len(text) + 2

    def offsets(self, text):
        return [(index, index + 1) for index in range(len(text))]


class Jobs:
    def __init__(self, lease):
        self.lease = lease
        self.published = []
        self.failures = []

    async def claim(self, document_id):
        return self.lease

    async def renew(self, lease):
        return True

    async def publish(self, lease, chunks, vectors, revision):
        self.published.append((chunks, vectors, revision))
        return True

    async def fail(self, lease, code, retryable):
        self.failures.append((code, retryable))


class Embedding:
    revision = "test-revision"

    async def embed_passages(self, texts):
        return [[1.0] + [0.0] * 383 for _ in texts]


def stored_pdf(tmp_path):
    org, doc = uuid4(), uuid4()
    path = tmp_path / str(org) / f"{doc}.pdf"
    path.parent.mkdir()
    source = Path(__file__).parents[1] / "fixtures/pdfs/authored_korean.pdf"
    path.write_bytes(source.read_bytes())
    return Lease(doc, org, uuid4(), 1, str(path))


async def test_worker_publishes_parsed_chunks_and_embeddings(tmp_path):
    from app.documents.worker import process_document

    lease = stored_pdf(tmp_path)
    jobs = Jobs(lease)
    await process_document(lease.document_id, jobs, Embedding(), Tokenizer(), tmp_path)
    assert jobs.failures == []
    assert len(jobs.published) == 1
    chunks, vectors, revision = jobs.published[0]
    assert chunks[0].text == "연차 휴가는 사전에 신청합니다."
    assert chunks[0].page == 1
    assert len(vectors) == len(chunks)
    assert revision == "test-revision"


async def test_duplicate_delivery_without_lease_does_no_work(tmp_path):
    from app.documents.worker import process_document

    jobs = Jobs(None)
    await process_document(uuid4(), jobs, Embedding(), Tokenizer(), tmp_path)
    assert jobs.published == jobs.failures == []


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (422, "invalid_pdf", False),
        (503, "embedding_unavailable", True),
        (504, "embedding_timeout", True),
        (502, "invalid_embedding", True),
    ],
)
async def test_processing_failures_are_classified(tmp_path, status, code, retryable):
    from app.documents.worker import process_document

    class FailedEmbedding(Embedding):
        async def embed_passages(self, texts):
            raise AppError(status, code, "private upstream text")

    lease = stored_pdf(tmp_path)
    jobs = Jobs(lease)
    await process_document(lease.document_id, jobs, FailedEmbedding(), Tokenizer(), tmp_path)
    assert jobs.published == []
    assert jobs.failures == [(code, retryable)]


def test_worker_logging_does_not_expose_malformed_pdf_contents(tmp_path, caplog):
    import logging

    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, StreamObject, TextStringObject

    from app.documents import worker
    from app.documents.parsing import parse_pdf

    sentinel = "PRIVATE_PDF_FONT_CANARY"
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
            NameObject("/Encoding"): DictionaryObject(
                {
                    NameObject("/Differences"): TextStringObject(sentinel),
                }
            ),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
        }
    )
    stream = StreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (Safe text) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "malformed-font.pdf"
    with path.open("wb") as output:
        writer.write(output)
    pdf_logger = logging.getLogger("pypdf")
    handlers, propagate = pdf_logger.handlers[:], pdf_logger.propagate
    try:
        with caplog.at_level(logging.WARNING):
            assert parse_pdf(path)[0].text == "Safe text"
        assert sentinel in caplog.text  # Prove the fixture reaches pypdf's unsafe warning.
        caplog.clear()
        worker.configure_worker_logging()
        with caplog.at_level(logging.WARNING):
            assert parse_pdf(path)[0].text == "Safe text"
            logging.getLogger("app.documents.worker").warning("job_processing_failed")
        assert sentinel not in caplog.text
        assert "job_processing_failed" in caplog.text
    finally:
        pdf_logger.handlers = handlers
        pdf_logger.propagate = propagate


async def test_heartbeat_loss_cancels_computation_without_publication(tmp_path):
    import asyncio

    from app.documents.worker import process_document

    began, cancelled = asyncio.Event(), asyncio.Event()

    class LostJobs(Jobs):
        async def renew(self, lease):
            await began.wait()
            return False

    class BlockedEmbedding(Embedding):
        async def embed_passages(self, texts):
            began.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    lease = stored_pdf(tmp_path)
    jobs = LostJobs(lease)
    async with asyncio.timeout(2):
        await process_document(
            lease.document_id,
            jobs,
            BlockedEmbedding(),
            Tokenizer(),
            tmp_path,
            heartbeat_interval=0.01,
        )
    assert cancelled.is_set()
    assert jobs.published == jobs.failures == []


async def test_cancellation_preserves_lease_for_recovery(tmp_path):
    import asyncio

    from app.documents.worker import process_document

    started = asyncio.Event()

    class BlockedEmbedding(Embedding):
        async def embed_passages(self, texts):
            started.set()
            await asyncio.Event().wait()

    lease = stored_pdf(tmp_path)
    jobs = Jobs(lease)
    task = asyncio.create_task(
        process_document(lease.document_id, jobs, BlockedEmbedding(), Tokenizer(), tmp_path)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert jobs.published == jobs.failures == []


def test_celery_worker_has_bounded_execution_and_delivery_configuration(tmp_path):
    from app.core.settings import Settings
    from app.documents.worker import create_worker_app

    app = create_worker_app(
        Settings(
            database_url="postgresql+psycopg://localhost/test",
            broker_url="redis://localhost:6379/0",
            worker_tokenizer_path=tmp_path,
        )
    )
    assert app.conf.worker_concurrency == 1
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_time_limit == 300
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.accept_content == ["json"]
    assert "documents.process" in app.tasks
    assert app.tasks["documents.process"].max_retries == 0  # DB owns the retry schedule.


async def test_service_keeps_upload_lock_until_database_commit(tmp_path):
    import asyncio
    from datetime import UTC, datetime

    from app.documents.schemas import DocumentReceipt
    from app.documents.service import DocumentService
    from app.documents.storage import FileStorage
    from app.users.types import Identity

    storage = FileStorage(tmp_path, minimum_free_bytes=0)
    held = []

    class Repository:
        async def create(self, identity, document_id, filename, path):
            async with storage.try_track_upload(identity.organization_id, document_id) as acquired:
                held.append(not acquired)
            return DocumentReceipt(
                document_id=document_id, status="queued", created_at=datetime.now(UTC)
            )

    async def body():
        yield b"%PDF-1.4\n"

    async with asyncio.timeout(2):
        await DocumentService(Repository(), storage).accept(
            Identity(uuid4(), uuid4()), "policy.pdf", body()
        )
    assert held == [True]


async def test_tokenizer_initialization_failure_consumes_claim_and_records_safe_retry(
    tmp_path, monkeypatch
):
    from app.core.settings import Settings
    from app.documents import jobs as job_module
    from app.documents import worker
    from app.rag.tokenization import Tokenizer as RealTokenizer

    lease = stored_pdf(tmp_path)
    jobs = Jobs(lease)
    claimed = []

    async def claim(document_id):
        claimed.append(document_id)
        return lease

    def broken_tokenizer(path):
        assert claimed == [lease.document_id]
        raise OSError("private cache path")

    jobs.claim = claim
    monkeypatch.setattr(job_module, "JobRepository", lambda sessions: jobs)
    monkeypatch.setattr(RealTokenizer, "from_pretrained", broken_tokenizer)
    await worker.run_document(
        lease.document_id,
        Settings(
            database_url="postgresql+psycopg://localhost/test", worker_tokenizer_path=tmp_path
        ),
    )
    assert jobs.failures == [("processing_failed", True)]
    assert jobs.published == []
