import asyncio
import logging
from uuid import UUID

import anyio
from celery import Celery  # type: ignore[import-untyped]
from kombu.exceptions import OperationalError  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class JobNotifier:
    """Best-effort wake-up; the committed PostgreSQL job remains authoritative."""

    def __init__(self, broker_url: str) -> None:
        self._client = Celery("documents", broker=broker_url, set_as_current=False)
        self._client.conf.update(
            broker_connection_timeout=0.5,
            broker_connection_retry=False,
            broker_pool_limit=0,
            task_publish_retry=False,
            broker_transport_options={
                "socket_connect_timeout": 0.5,
                "socket_timeout": 0.5,
                "retry_on_timeout": False,
                "max_retries": 0,
            },
        )
        self._pending: asyncio.Task[None] | None = None

    def _send(self, document_id: UUID) -> None:
        try:
            self._client.send_task(
                "documents.process",
                args=[str(document_id)],
                retry=False,
                ignore_result=True,
            )
        except OperationalError, OSError:
            logger.warning("document_notification_unavailable")

    async def notify(self, document_id: UUID) -> None:
        if self._pending is not None and not self._pending.done():
            return
        # The publisher owns its slot even after its caller times out or disconnects.
        # Missed wake-ups are recoverable from PostgreSQL; never enqueue more threads.
        self._pending = asyncio.create_task(anyio.to_thread.run_sync(self._send, document_id))
        self._pending.add_done_callback(self._finished)
        try:
            async with asyncio.timeout(1):
                await asyncio.shield(self._pending)
        except TimeoutError:
            logger.warning("document_notification_timeout")

    @staticmethod
    def _finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning("document_notification_unavailable")
