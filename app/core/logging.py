import asyncio
import logging
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class _ServerCodes(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn can otherwise print an exception's body, URL or credentials.
        record.msg = "server_error" if record.levelno >= logging.ERROR else "server_lifecycle"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_logging() -> None:
    """Explicit process startup policy: emit application codes, never request bodies."""
    for name in ("uvicorn.access", "httpx", "httpcore", "sqlalchemy", "python_multipart", "pypdf"):
        dependency = logging.getLogger(name)
        dependency.handlers = [logging.NullHandler()]
        dependency.propagate = False
    server = logging.getLogger("uvicorn.error")
    if not any(isinstance(item, _ServerCodes) for item in server.filters):
        server.addFilter(_ServerCodes())


class SafeLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False
        finished = False

        async def tracked_send(message: Message) -> None:
            nonlocal started, finished
            await send(message)
            if message["type"] == "http.response.start":
                started = True
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True

        try:
            await self.app(scope, receive, tracked_send)
        except Exception:
            request_id = scope.get("state", {}).get("request_id", str(uuid4()))
            logger.error("request_failed", extra={"request_id": request_id})
            if finished:
                return
            try:
                async with asyncio.timeout(1):
                    if started:
                        # A stream without a terminal SSE event remains incomplete to the client.
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
                    else:
                        await JSONResponse(
                            {
                                "error": {
                                    "code": "internal_error",
                                    "message": "일시적인 오류가 발생했습니다.",
                                },
                                "request_id": request_id,
                            },
                            status_code=500,
                            headers={"X-Request-ID": request_id},
                        )(scope, receive, send)
            except Exception:
                # The transport itself may already have failed; never log its raw exception.
                return
