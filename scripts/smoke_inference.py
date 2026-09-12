import sys
from collections.abc import Sequence
from functools import partial
from time import perf_counter

import httpx

from app.core.errors import AppError
from app.inference.client import InferenceClient
from app.inference.types import Completed, Delta
from scripts.smoke_http import ProbeResult, run_command


async def probe(http: httpx.AsyncClient, model: str, *, cancel: bool) -> ProbeResult:
    started = perf_counter()
    first_text_ms: float | None = None
    completion: Completed | None = None
    completions = 0
    client = InferenceClient(http, model, output_tokens=128)
    async with client.open(
        [{"role": "user", "content": "가상의 회사 안내입니다. 안녕하세요를 한 문장으로 답하세요."}]
    ) as events:
        async for event in events:
            if isinstance(event, Delta) and event.text.strip():
                if first_text_ms is None:
                    first_text_ms = (perf_counter() - started) * 1000
                if cancel:
                    break
            elif isinstance(event, Completed):
                completion = event
                completions += 1
    if first_text_ms is None or (not cancel and completions != 1):
        raise AppError(502, "smoke_incomplete", "Nonempty completed answer required")
    usage = completion.usage if completion else None
    return {
        "status": "client_cancelled" if cancel else "ok",
        "first_text_ms": first_text_ms,
        "total_ms": (perf_counter() - started) * 1000,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "gpu_cancellation_verified": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    cancel = "--cancel-after-first-text" in arguments
    if cancel:
        arguments.remove("--cancel-after-first-text")
    return run_command(
        partial(probe, cancel=cancel),
        "Qwen/Qwen3-4B-Instruct-2507",
        "LLM_LAB_SMOKE_INFERENCE_URL",
        arguments,
    )


if __name__ == "__main__":
    raise SystemExit(main())
