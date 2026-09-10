import asyncio
from collections.abc import AsyncIterator

import httpx


class ControlledStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], failure: Exception | None = None, block: bool = False):
        self.chunks = chunks
        self.failure = failure
        self.block = block
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.failure:
            raise self.failure
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True
