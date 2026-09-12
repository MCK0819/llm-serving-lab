import math
from collections.abc import Sequence
from time import perf_counter

import httpx

from app.core.errors import AppError
from scripts.smoke_http import ProbeResult, read_json, run_command


async def probe(http: httpx.AsyncClient, model: str) -> ProbeResult:
    info = await read_json(http, "GET", "/info")
    if not isinstance(info, dict) or info.get("model_id") != model:
        raise AppError(502, "model_mismatch", "Unexpected embedding model")
    started = perf_counter()
    vectors = await read_json(
        http,
        "POST",
        "/embed",
        {
            "inputs": [
                "query: 휴가 신청 방법은?",
                "passage: 가상의 회사에서는 휴가를 관리자에게 신청합니다.",
            ],
            "normalize": True,
            "truncate": False,
        },
    )
    if not isinstance(vectors, list) or len(vectors) != 2:
        raise AppError(502, "invalid_embedding", "Expected two vectors")
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != 384:
            raise AppError(502, "invalid_embedding", "Expected 384 dimensions")
        if any(
            type(value) not in (float, int)
            or not -1.001 <= value <= 1.001
            or not math.isfinite(value)
            for value in vector
        ):
            raise AppError(502, "invalid_embedding", "Expected finite numeric values")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isclose(norm, 1.0, abs_tol=0.001):
            raise AppError(502, "invalid_embedding", "Expected normalized vectors")
    return {
        "status": "ok",
        "dimensions": 384,
        "vectors": 2,
        "embedding_ms": (perf_counter() - started) * 1000,
    }


def main(argv: Sequence[str] | None = None) -> int:
    return run_command(probe, "intfloat/multilingual-e5-small", "LLM_LAB_SMOKE_EMBEDDING_URL", argv)


if __name__ == "__main__":
    raise SystemExit(main())
