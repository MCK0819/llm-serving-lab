"""Container-owned SIGTERM regression; opt in with LLM_LAB_SHUTDOWN_TEST_IMAGE."""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.integration


async def docker(*args: str) -> str:
    result = await asyncio.to_thread(
        subprocess.run, ["docker", *args], check=True, capture_output=True, text=True, timeout=60
    )
    return result.stdout.strip()


async def wait_http(client: httpx.AsyncClient, path: str) -> httpx.Response:
    async with asyncio.timeout(20):
        while True:
            try:
                response = await client.get(path)
                if response.status_code == 200:
                    return response
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.05)


@pytest.mark.parametrize("finish", ["complete", "deadline"])
async def test_sigterm_keeps_listener_for_503_then_closes_upstream_and_exits(
    finish, record_property
):
    image = os.environ.get("LLM_LAB_SHUTDOWN_TEST_IMAGE")
    if not image:
        pytest.skip("Set LLM_LAB_SHUTDOWN_TEST_IMAGE to a local image containing test dependencies")
    project = "llm-shutdown-" + uuid4().hex
    root = str(Path(__file__).resolve().parents[2])
    containers = []
    await docker("network", "create", project)
    try:

        async def start(role):
            container = await docker(
                "run",
                "-d",
                "--name",
                project + "-" + role,
                "--network",
                project,
                "--network-alias",
                role,
                "-p",
                "127.0.0.1::8000",
                "--mount",
                f"type=bind,src={root},dst=/srv/llm-serving-lab,readonly",
                "--workdir",
                "/srv/llm-serving-lab",
                "--entrypoint",
                "python",
                image,
                "-m",
                "tests.fixtures.shutdown_app",
                role,
            )
            containers.append(container)
            bindings = json.loads(await docker("inspect", container))[0]["NetworkSettings"]["Ports"]
            return container, "http://127.0.0.1:" + bindings["8000/tcp"][0]["HostPort"]

        _, upstream_url = await start("upstream")
        api_id, api_url = await start("api")
        async with (
            httpx.AsyncClient(base_url=api_url, timeout=40, trust_env=False) as api,
            httpx.AsyncClient(base_url=upstream_url, timeout=5, trust_env=False) as upstream,
        ):
            await wait_http(upstream, "/test/state")
            await wait_http(api, "/health/live")
            async with api.stream("POST", "/test/stream") as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                async for line in lines:
                    if line == "event: delta":
                        break
                assert (await upstream.get("/test/state")).json()["active"] == 1
                started = time.monotonic()
                await docker("kill", "--signal", "TERM", api_id)
                for route in ("/documents", "/questions"):
                    rejected = await api.post(route, content=b"new work")
                    assert rejected.status_code == 503
                    assert rejected.json()["error"]["code"] == "draining"
                assert (await api.get("/health/ready")).status_code == 503
                assert (await api.get("/health/live")).status_code == 200
                if finish == "complete":
                    await upstream.post("/test/release")
                remaining = []
                try:
                    async for line in lines:
                        remaining.append(line)
                except httpx.RemoteProtocolError:
                    assert finish == "deadline"
                assert ("event: done" in remaining) == (finish == "complete")
            await docker("wait", api_id)
            elapsed = time.monotonic() - started
            state = json.loads(await docker("inspect", api_id))[0]["State"]
            assert state["ExitCode"] in (0, 143), state
            assert not state["OOMKilled"]
            assert elapsed < 39
            if finish == "deadline":
                assert elapsed >= 29
            else:
                assert elapsed < 10
            assert (await upstream.get("/test/state")).json() == {"active": 0, "closed": 1}
            record_property("shutdown_seconds", round(elapsed, 3))
    finally:
        for container in reversed(containers):
            await docker("rm", "-f", container)
        await docker("network", "rm", project)
