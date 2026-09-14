import asyncio
import json
import math
from typing import TYPE_CHECKING

import httpx

from app.core.errors import AppError

if TYPE_CHECKING:
    from app.rag.tokenization import TextTokenizer


class EmbeddingClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        tokenizer: TextTokenizer,
        revision: str,
        *,
        request_timeout: float = 30,
    ) -> None:
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("Embedding request timeout must be finite and positive")
        self.http = http
        self.tokenizer = tokenizer
        self.revision = revision
        self.request_timeout = request_timeout

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        inputs = [self._input("passage: ", text) for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(inputs), 16):
            vectors.extend(await self._embed(inputs[start : start + 16]))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([self._input("query: ", text)]))[0]

    def _input(self, prefix: str, text: str) -> str:
        value = prefix + text
        if not text.strip() or self.tokenizer.count(value) > 512:
            raise AppError(422, "embedding_input_too_long", "검색할 텍스트의 길이를 확인해 주세요.")
        return value

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        try:
            async with asyncio.timeout(self.request_timeout):
                async with self.http.stream(
                    "POST",
                    "/embed",
                    json={
                        "inputs": inputs,
                        "normalize": True,
                        "truncate": False,
                    },
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code != 200:
                        raise self._invalid()
                    if (
                        response.headers.get("content-type", "").split(";")[0].strip().lower()
                        != "application/json"
                    ):
                        raise self._invalid()
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise self._invalid()
                    body = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(body) + len(chunk) > 256 * 1024:
                            raise self._invalid()
                        body.extend(chunk)
                    try:
                        payload = json.loads(body)
                    except ValueError, RecursionError:
                        raise self._invalid() from None
        except httpx.TimeoutException, TimeoutError:
            raise AppError(
                504, "embedding_timeout", "문서 검색 서버의 응답 시간이 초과되었습니다."
            ) from None
        except httpx.ConnectError:
            raise AppError(
                503, "embedding_unavailable", "문서 검색 서버에 연결할 수 없습니다."
            ) from None
        except httpx.HTTPError:
            raise self._invalid() from None
        if not isinstance(payload, list) or len(payload) != len(inputs):
            raise self._invalid()
        return [self._normalize(vector) for vector in payload]

    @classmethod
    def _normalize(cls, vector: object) -> list[float]:
        if not isinstance(vector, list) or len(vector) != 384:
            raise cls._invalid()
        values: list[float] = []
        for item in vector:
            if type(item) not in (int, float):
                raise cls._invalid()
            try:
                number = float(item)
            except OverflowError:
                raise cls._invalid() from None
            if not math.isfinite(number):
                raise cls._invalid()
            values.append(number)
        scale = max(abs(value) for value in values)
        if scale == 0:
            raise cls._invalid()
        scaled = [value / scale for value in values]
        norm = math.hypot(*scaled)
        return [value / norm for value in scaled]

    @staticmethod
    def _invalid() -> AppError:
        return AppError(502, "invalid_embedding", "문서 검색용 응답을 처리하지 못했습니다.")
