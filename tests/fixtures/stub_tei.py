"""Controllable TEI-compatible HTTP stub for worker process integration tests."""

import asyncio

from fastapi import FastAPI

app = FastAPI()
_release = asyncio.Event()
_requests = 0


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/test/reset")
async def reset() -> dict[str, int]:
    global _release, _requests
    _release = asyncio.Event()
    _requests = 0
    return {"requests": _requests}


@app.get("/test/state")
async def state() -> dict[str, int]:
    return {"requests": _requests}


@app.post("/test/release")
async def release() -> dict[str, int]:
    _release.set()
    return {"requests": _requests}


@app.post("/embed")
async def embed(payload: dict[str, object]) -> list[list[float]]:
    global _requests
    inputs = payload.get("inputs")
    if not isinstance(inputs, list) or not all(isinstance(item, str) for item in inputs):
        return []
    _requests += 1
    await _release.wait()
    vector = [1.0, *([0.0] * 383)]
    return [vector.copy() for _ in inputs]
