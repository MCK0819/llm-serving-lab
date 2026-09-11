from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import AppError
from app.users.service import AuthService, parse_key
from app.users.types import Identity


def identity_dependency(auth: AuthService | None) -> Callable[[Request], Awaitable[Identity]]:
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)] = None,
    ) -> Identity:
        headers = request.headers.getlist("authorization")
        try:
            if len(headers) != 1 or credentials is None:
                raise AppError(401, "unauthorized", "API Key가 필요합니다.")
            scheme, _, raw_key = headers[0].partition(" ")
            if scheme.lower() != "bearer":
                raise AppError(401, "unauthorized", "API Key가 필요합니다.")
            parse_key(raw_key)
            if auth is None:
                raise AppError(
                    503, "authentication_unavailable", "인증 저장소가 준비되지 않았습니다."
                )
            return await auth.authenticate(raw_key)
        except AppError as exc:
            if exc.status == 401:
                raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"}) from None
            raise

    return authenticate
