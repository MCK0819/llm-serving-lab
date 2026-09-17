from contextlib import AsyncExitStack

from fastapi import FastAPI
from starlette.responses import JSONResponse

from app.bootstrap import application_lifespan, build_services
from app.core.body_limit import BodyLimitMiddleware
from app.core.errors import AppError, register_error_handlers
from app.core.lifecycle import LifecycleMiddleware
from app.core.logging import SafeLoggingMiddleware, configure_logging
from app.core.request_context import RequestContextMiddleware
from app.core.settings import Settings
from app.documents.router import build_document_router
from app.inference.response import PreparedStream
from app.rag.router import QuestionPreparer, build_question_router
from app.users.types import Identity


def create_app(
    settings: Settings | None = None, *, prepare_question: QuestionPreparer | None = None
) -> FastAPI:
    # Resolve environment configuration at startup, never during module import.
    settings = settings if settings is not None else Settings()
    configure_logging()
    services = build_services(settings)
    app = FastAPI(title="LLM Serving Lab", lifespan=application_lifespan(settings, services))
    app.state.settings = settings
    app.state.lifecycle = services.lifecycle
    app.add_middleware(BodyLimitMiddleware, maximum_bytes=settings.document_request_bytes)
    app.add_middleware(LifecycleMiddleware, lifecycle=services.lifecycle)
    app.add_middleware(SafeLoggingMiddleware)
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(build_document_router(services.auth, services.documents, services.limiter))

    async def prepare_rag(
        identity: Identity, question: str, stack: AsyncExitStack
    ) -> PreparedStream:
        if services.rag is None:
            raise AppError(503, "rag_unavailable", "문서 검색 기능이 아직 준비되지 않았습니다.")
        return await services.rag.prepare(identity, question, stack)

    app.include_router(
        build_question_router(
            services.auth,
            prepare_question if prepare_question is not None else prepare_rag,
            services.admission,
            settings,
            services.limiter,
        )
    )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        available = await services.health.ready()
        return JSONResponse(
            {"status": "ok" if available else "unavailable"},
            status_code=200 if available else 503,
        )

    return app
