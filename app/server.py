"""Single-process entrypoint with admission drain before closing listeners."""

import asyncio
import socket
from types import FrameType

import uvicorn
from fastapi import FastAPI

from app.core.lifecycle import LifecycleCoordinator
from app.core.logging import configure_logging


class DrainingServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, lifecycle: LifecycleCoordinator) -> None:
        super().__init__(config)
        self.lifecycle_coordinator = lifecycle

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self.lifecycle_coordinator.begin_draining()
        super().handle_exit(sig, frame)

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        # Uvicorn 0.52 normally closes listeners before ASGI lifespan shutdown.
        # Keep them available for explicit 503/readiness responses while draining.
        await self.lifecycle_coordinator.shutdown()
        async with asyncio.timeout(5):
            await super().shutdown(sockets)


def run(app: FastAPI | None = None) -> None:
    from app.main import create_app

    application = create_app() if app is None else app
    config = uvicorn.Config(
        application,
        host="0.0.0.0",
        port=8000,
        workers=1,
        access_log=False,
        timeout_graceful_shutdown=1,
    )
    # Config initializes Uvicorn logging; apply the safe policy afterwards.
    configure_logging()
    DrainingServer(config, application.state.lifecycle).run()


if __name__ == "__main__":
    run()
