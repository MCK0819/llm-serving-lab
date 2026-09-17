from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from python_multipart.exceptions import MultipartParseError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from app.core.errors import AppError
from app.core.rate_limit import RateLimiter
from app.documents.service import DocumentService
from app.users.dependencies import identity_dependency
from app.users.service import AuthService
from app.users.types import Identity


def build_document_router(
    auth: AuthService | None,
    service: DocumentService | None,
    limiter: RateLimiter | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/documents", tags=["documents"])
    authenticate = identity_dependency(auth)

    async def read_identity(
        identity: Annotated[Identity, Depends(authenticate)],
    ) -> Identity:
        if limiter is not None:
            limiter.check(identity.user_id, "read")
        return identity

    def documents() -> DocumentService:
        if service is None:
            raise AppError(503, "documents_unavailable", "문서 저장소가 준비되지 않았습니다.")
        return service

    @router.post(
        "",
        status_code=202,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file"],
                            "additionalProperties": False,
                            "properties": {"file": {"type": "string", "format": "binary"}},
                        }
                    }
                },
            },
        },
    )
    async def upload(
        request: Request, identity: Annotated[Identity, Depends(authenticate)]
    ) -> JSONResponse:
        if limiter is not None:
            limiter.check(identity.user_id, "upload")
        document_service = documents()
        if (
            request.headers.get("content-type", "").split(";")[0].strip().lower()
            != "multipart/form-data"
        ):
            raise AppError(
                415, "unsupported_media_type", "PDF 파일을 multipart 형식으로 보내 주세요."
            )
        try:
            async with request.form(max_files=1, max_fields=0, max_part_size=1024) as form:
                parts = form.multi_items()
                if (
                    len(parts) != 1
                    or parts[0][0] != "file"
                    or not isinstance(parts[0][1], UploadFile)
                ):
                    raise AppError(422, "invalid_upload", "file 항목에 PDF 하나를 보내 주세요.")
                file = parts[0][1]
                if file.content_type != "application/pdf":
                    raise AppError(415, "unsupported_file", "PDF 파일을 업로드해 주세요.")

                async def body() -> AsyncIterator[bytes]:
                    while chunk := await file.read(64 * 1024):
                        yield chunk

                receipt = await document_service.accept(identity, file.filename or "", body())
        except HTTPException, MultipartParseError:
            if getattr(request.state, "body_limit_exceeded", False):
                raise
            raise AppError(422, "invalid_upload", "file 항목에 PDF 하나를 보내 주세요.") from None
        return JSONResponse(
            status_code=202,
            content={**receipt.model_dump(mode="json"), "request_id": request.state.request_id},
            headers={"Location": f"/documents/{receipt.document_id}"},
        )

    @router.get("")
    async def list_documents(
        request: Request,
        identity: Annotated[Identity, Depends(read_identity)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> JSONResponse:
        page = await documents().list(identity, limit, cursor)
        return JSONResponse(
            {**page.model_dump(mode="json"), "request_id": request.state.request_id}
        )

    @router.get("/{document_id}")
    async def get_document(
        document_id: UUID,
        request: Request,
        identity: Annotated[Identity, Depends(read_identity)],
    ) -> JSONResponse:
        detail = await documents().get(identity, document_id)
        return JSONResponse(
            {**detail.model_dump(mode="json"), "request_id": request.state.request_id}
        )

    @router.delete("/{document_id}", status_code=204)
    async def delete_document(
        document_id: UUID, identity: Annotated[Identity, Depends(read_identity)]
    ) -> Response:
        await documents().delete(identity, document_id)
        return Response(status_code=204)

    return router
