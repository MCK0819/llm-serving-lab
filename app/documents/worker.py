import argparse
import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
from celery import Celery  # type: ignore[import-untyped]
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.database import create_database
from app.core.errors import AppError
from app.core.settings import Settings
from app.documents.chunking import Chunk, chunk_pages
from app.documents.parsing import parse_pdf
from app.rag.embedding import EmbeddingClient
from app.rag.tokenization import TextTokenizer

if TYPE_CHECKING:
    from app.documents.jobs import JobRepository, Lease

logger = logging.getLogger(__name__)


def prepare_chunks(path: Path, tokenizer: TextTokenizer) -> list[Chunk]:
    return chunk_pages(parse_pdf(path), tokenizer)


async def process_document(
    document_id: UUID,
    jobs: JobRepository,
    embedding: EmbeddingClient,
    tokenizer: TextTokenizer,
    upload_root: Path,
    *,
    heartbeat_interval: float = 20,
) -> None:
    async def compute(lease: Lease) -> tuple[list[Chunk], list[list[float]]]:
        path = upload_root / str(lease.organization_id) / f"{lease.document_id}.pdf"
        chunks = await asyncio.to_thread(prepare_chunks, path, tokenizer)
        vectors = await embedding.embed_passages([chunk.text for chunk in chunks])
        return chunks, vectors

    await run_attempt(document_id, jobs, compute, embedding.revision, heartbeat_interval)


async def run_attempt(
    document_id: UUID,
    jobs: JobRepository,
    compute: Callable[[Lease], Awaitable[tuple[list[Chunk], list[list[float]]]]],
    revision: str,
    heartbeat_interval: float = 20,
) -> None:
    if not math.isfinite(heartbeat_interval) or heartbeat_interval <= 0:
        raise ValueError("Heartbeat interval must be finite and positive")
    try:
        lease = await jobs.claim(document_id)
    except SQLAlchemyError:
        logger.warning("job_database_unavailable")
        return
    if lease is None:
        return

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_interval)
            if not await jobs.renew(lease):
                return

    async def execute() -> tuple[list[Chunk], list[list[float]]]:
        return await compute(lease)

    computation = asyncio.create_task(execute())
    pulse = asyncio.create_task(heartbeat())
    try:
        async with asyncio.timeout(300):
            done, _ = await asyncio.wait({computation, pulse}, return_when=asyncio.FIRST_COMPLETED)
            if pulse in done:
                pulse.result()
                logger.warning("job_lease_lost")
                return
            chunks, vectors = computation.result()
            await jobs.publish(lease, chunks, vectors, revision)
    except AppError as exc:
        await record_failure(jobs, lease, exc.code, exc.status >= 500)
    except TimeoutError:
        await record_failure(jobs, lease, "processing_timeout", True)
    except SQLAlchemyError:
        # An unavailable DB cannot safely authorize publication or a new attempt.
        logger.warning("job_database_unavailable")
    except Exception:
        logger.warning("job_processing_failed")
        await record_failure(jobs, lease, "processing_failed", True)
    finally:
        computation.cancel()
        pulse.cancel()
        await asyncio.gather(computation, pulse, return_exceptions=True)


async def record_failure(jobs: JobRepository, lease: Lease, code: str, retryable: bool) -> None:
    try:
        await jobs.fail(lease, code, retryable)
    except SQLAlchemyError:
        logger.warning("job_database_unavailable")


def create_worker_app(settings: Settings) -> Celery:
    if (
        settings.database_url is None
        or settings.broker_url is None
        or settings.worker_tokenizer_path is None
    ):
        raise ValueError("Worker requires database, broker and local tokenizer settings")
    app = Celery("documents", broker=settings.broker_url.get_secret_value(), set_as_current=False)
    app.conf.update(
        worker_concurrency=1,
        worker_prefetch_multiplier=1,
        task_time_limit=300,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_ignore_result=True,
        task_serializer="json",
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=5,
        broker_transport_options={"visibility_timeout": 360},
        worker_hijack_root_logger=False,
    )

    def run(document_id: str) -> None:
        if not isinstance(document_id, str):
            logger.warning("invalid_document_job")
            return
        try:
            identifier = UUID(document_id)
        except ValueError:
            logger.warning("invalid_document_job")
            return
        try:
            asyncio.run(run_document(identifier, settings))
        except Exception:
            # The DB lease expires after unexpected process/dependency failures.
            logger.error("job_execution_failed")

    app.task(name="documents.process", max_retries=0)(run)
    return app


async def run_document(document_id: UUID, settings: Settings) -> None:
    from app.documents.jobs import JobRepository
    from app.rag.tokenization import Tokenizer

    if settings.database_url is None or settings.worker_tokenizer_path is None:
        raise ValueError("Worker requires database and tokenizer settings")
    tokenizer_path = settings.worker_tokenizer_path
    engine = create_database(settings.database_url.get_secret_value())
    try:
        jobs = JobRepository(async_sessionmaker(engine, expire_on_commit=False))

        async def compute(lease: Lease) -> tuple[list[Chunk], list[list[float]]]:
            tokenizer = await asyncio.to_thread(Tokenizer.from_pretrained, tokenizer_path)
            path = settings.upload_root / str(lease.organization_id) / f"{lease.document_id}.pdf"
            chunks = await asyncio.to_thread(prepare_chunks, path, tokenizer)
            async with httpx.AsyncClient(
                base_url=str(settings.embedding_url),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(30, connect=5),
                limits=httpx.Limits(max_connections=1),
            ) as http:
                embedding = EmbeddingClient(http, tokenizer, settings.embedding_revision)
                return chunks, await embedding.embed_passages([chunk.text for chunk in chunks])

        await run_attempt(document_id, jobs, compute, settings.embedding_revision)
    finally:
        await engine.dispose()


async def run_recovery(settings: Settings) -> None:
    from app.documents.jobs import JobRepository
    from app.documents.notifications import JobNotifier
    from app.documents.recovery import DocumentRecovery
    from app.documents.storage import FileStorage

    if settings.database_url is None or settings.broker_url is None:
        raise ValueError("Recovery requires database and broker settings")
    engine = create_database(settings.database_url.get_secret_value())
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        jobs = JobRepository(sessions)
        notifier = JobNotifier(settings.broker_url.get_secret_value())
        cleanup = DocumentRecovery(sessions, FileStorage(settings.upload_root))
        while True:
            try:
                for document_id in await jobs.recover_due():
                    await notifier.notify(document_id)
                await cleanup.run_once()
            except Exception:
                logger.warning("job_recovery_unavailable")
            await asyncio.sleep(30)
    finally:
        await engine.dispose()


def configure_worker_logging() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    # Malformed PDF warnings may contain document text. Keep only our safe failure codes.
    pdf_logger = logging.getLogger("pypdf")
    pdf_logger.handlers = [logging.NullHandler()]
    pdf_logger.propagate = False


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the document worker or recovery scheduler")
    parser.add_argument("mode", choices=["worker", "recover"])
    args = parser.parse_args()
    configure_worker_logging()
    settings = Settings()
    if args.mode == "worker":
        create_worker_app(settings).worker_main(
            ["worker", "--pool=prefork", "--concurrency=1", "--loglevel=WARNING"]
        )
    else:
        asyncio.run(run_recovery(settings))


if __name__ == "__main__":
    main()
