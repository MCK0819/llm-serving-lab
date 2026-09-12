from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class AppError(Exception):
    """An expected failure with a deliberately public message; never wrap raw exceptions."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(request: Request, status: int, code: str, message: str) -> JSONResponse:
    request_id = request.state.request_id
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}, "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def expected(request: Request, exc: AppError) -> JSONResponse:
        return error_response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(request, 422, "invalid_request", "요청 형식을 확인해 주세요.")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        if getattr(request.state, "body_limit_exceeded", False):
            return error_response(
                request, 413, "request_too_large", "요청 크기 제한을 초과했습니다."
            )
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(
            exc.status_code, "http_error"
        )
        response = error_response(request, exc.status_code, code, "요청을 처리할 수 없습니다.")
        for name, value in (exc.headers or {}).items():
            if name.lower() in {"allow", "www-authenticate", "retry-after"}:
                response.headers[name] = value
        return response

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        return error_response(request, 500, "internal_error", "일시적인 오류가 발생했습니다.")
