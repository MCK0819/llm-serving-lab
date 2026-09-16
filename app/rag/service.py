from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

from app.core.errors import AppError
from app.inference.response import PreparedStream
from app.inference.types import Completed, Delta
from app.users.types import Identity

if TYPE_CHECKING:
    from app.inference.client import InferenceClient
    from app.rag.embedding import EmbeddingClient
    from app.rag.prompt import PreparedPrompt, PromptBuilder
    from app.rag.retrieval import Retriever


class RagService:
    def __init__(
        self,
        embedding: EmbeddingClient,
        retriever: Retriever,
        prompt: PromptBuilder,
        inference: InferenceClient,
        *,
        prompt_limit: int = 2,
    ) -> None:
        if type(prompt_limit) is not int or prompt_limit < 1:
            raise ValueError("Prompt limit must be a positive integer")
        self.embedding = embedding
        self.retriever = retriever
        self.prompt = prompt
        self.inference = inference
        self.prompt_limit = prompt_limit
        self._prompt_jobs: set[asyncio.Task[PreparedPrompt]] = set()

    async def prepare(
        self, identity: Identity, question: str, stack: AsyncExitStack
    ) -> PreparedStream:
        vector = await self.embedding.embed_query(question)
        chunks = await self.retriever.search(
            identity.organization_id, vector, self.embedding.revision, limit=5
        )
        if not chunks:
            return PreparedStream([], self._no_context_events())

        if len(self._prompt_jobs) >= self.prompt_limit:
            raise AppError(503, "prompt_busy", "답변 준비 작업이 혼잡합니다.")
        prompt_task = asyncio.create_task(asyncio.to_thread(self.prompt.build, question, chunks))
        self._prompt_jobs.add(prompt_task)
        prompt_task.add_done_callback(self._prompt_done)
        prepared = await asyncio.shield(prompt_task)
        events = await stack.enter_async_context(self.inference.open(prepared.messages))
        return PreparedStream(prepared.sources, events)

    def _prompt_done(self, task: asyncio.Task[PreparedPrompt]) -> None:
        self._prompt_jobs.discard(task)
        if not task.cancelled():
            task.exception()

    @staticmethod
    async def _no_context_events() -> AsyncIterator[Delta | Completed]:
        yield Delta("관련 문서에서 답변 근거를 찾지 못했습니다.")
        yield Completed("no_context", None)
