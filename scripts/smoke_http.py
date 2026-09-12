import argparse
import asyncio
import ipaddress
import json
import math
import os
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

import httpx

from app.core.errors import AppError

type ProbeResult = dict[str, str | int | float | bool | None]


def run_command(
    probe: Callable[[httpx.AsyncClient, str], Awaitable[ProbeResult]],
    default_model: str,
    endpoint_variable: str,
    argv: Sequence[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Private model endpoint smoke check; JSON summary only"
    )
    parser.add_argument("--endpoint", default=os.environ.get(endpoint_variable, ""))
    parser.add_argument("--model", default=default_model)
    parser.add_argument("--deadline", type=float, default=120)
    args = parser.parse_args(argv)

    async def run() -> ProbeResult:
        endpoint = validate_endpoint(args.endpoint)
        if not math.isfinite(args.deadline) or not 0 < args.deadline <= 1200:
            raise ValueError("Invalid deadline")
        token = os.environ.get("LLM_LAB_SMOKE_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with asyncio.timeout(args.deadline):
            async with httpx.AsyncClient(
                base_url=endpoint,
                headers=headers,
                timeout=httpx.Timeout(30, connect=5),
                follow_redirects=False,
                trust_env=False,
            ) as http:
                return await probe(http, args.model)

    try:
        result = asyncio.run(run())
    except AppError as exc:
        result = {"status": "failed", "code": exc.code}
    except TimeoutError, httpx.TimeoutException:
        result = {"status": "failed", "code": "probe_timeout"}
    except ValueError, httpx.HTTPError, OSError:
        result = {"status": "failed", "code": "probe_failed"}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if result["status"] == "failed" else 0


def validate_endpoint(value: str) -> str:
    url = urlsplit(value)
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Invalid endpoint")
    loopback = url.hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        pass
    if url.scheme != "https" and not (url.scheme == "http" and loopback):
        raise ValueError("Use HTTPS or a loopback SSH tunnel")
    return value


async def read_json(
    http: httpx.AsyncClient, method: str, path: str, payload: object = None
) -> object:
    async with http.stream(
        method, path, json=payload, headers={"Accept-Encoding": "identity"}
    ) as response:
        response.raise_for_status()
        if response.headers.get("content-encoding", "identity") != "identity":
            raise AppError(502, "invalid_probe_response", "Unexpected encoding")
        body = bytearray()
        async for chunk in response.aiter_raw():
            if len(body) + len(chunk) > 131072:
                raise AppError(502, "probe_response_too_large", "Response exceeds smoke limit")
            body.extend(chunk)
    return json.loads(body)
