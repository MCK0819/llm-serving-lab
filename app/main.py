from contextlib import AsyncExitStack

from fastapi import FastAPI

from app.bootstrap import application_lifespan, build_services
from app.core.body_limit import BodyLimitMiddleware
from app.core.errors import AppError, register_error_handlers
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
    services = build_services(settings)
    app = FastAPI(title="LLM Serving Lab", lifespan=application_lifespan(settings, services))
    app.state.settings = settings
    app.add_middleware(BodyLimitMiddleware, maximum_bytes=settings.document_request_bytes)
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(build_document_router(services.auth, services.documents))

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
        )
    )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    return app
