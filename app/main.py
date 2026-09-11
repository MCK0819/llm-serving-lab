from fastapi import FastAPI

from app.bootstrap import application_lifespan, build_services
from app.core.errors import register_error_handlers
from app.core.request_context import RequestContextMiddleware
from app.core.settings import Settings
from app.rag.router import QuestionPreparer, build_question_router


def create_app(
    settings: Settings | None = None, *, prepare_question: QuestionPreparer | None = None
) -> FastAPI:
    # Resolve environment configuration at startup, never during module import.
    settings = settings if settings is not None else Settings()
    services = build_services(settings)
    app = FastAPI(title="LLM Serving Lab", lifespan=application_lifespan(settings, services))
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(
        build_question_router(services.auth, prepare_question, services.admission, settings)
    )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app
