from fastapi import FastAPI

from app.bootstrap import inference_lifespan
from app.core.errors import register_error_handlers
from app.core.request_context import RequestContextMiddleware
from app.core.settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    # Resolve environment configuration at startup, never during module import.
    settings = settings if settings is not None else Settings()
    app = FastAPI(title="LLM Serving Lab", lifespan=inference_lifespan(settings))
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app
