import hashlib
import hmac
import re
import secrets
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError

from app.core.errors import AppError
from app.users.types import Identity

if TYPE_CHECKING:
    from app.users.repository import UserRepository


class AuthService:
    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    async def authenticate(self, raw_key: str) -> Identity:
        key_id, secret = parse_key(raw_key)
        try:
            stored = await self.repository.find_active_key(key_id)
        except SQLAlchemyError:
            raise AppError(
                503, "authentication_unavailable", "인증 저장소에 연결할 수 없습니다."
            ) from None
        digest = hashlib.sha256(secret.encode("ascii")).hexdigest()
        expected = stored.secret_hash if stored is not None else "0" * 64
        matches = hmac.compare_digest(digest, expected)
        if stored is None or not matches:
            raise AppError(401, "unauthorized", "유효한 API Key가 필요합니다.")
        return stored.identity

    async def issue_key(self, user_id: UUID) -> str:
        key_id, secret = uuid4(), secrets.token_urlsafe(32)
        await self.repository.add_key(
            key_id, user_id, hashlib.sha256(secret.encode("ascii")).hexdigest()
        )
        return f"{key_id}.{secret}"

    async def revoke_key(self, key_id: UUID) -> None:
        await self.repository.revoke_key(key_id)


def parse_key(raw_key: str) -> tuple[UUID, str]:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}\.[A-Za-z0-9_-]{43}", raw_key):
        raise AppError(401, "unauthorized", "유효한 API Key가 필요합니다.")
    key_id, secret = raw_key.split(".")
    try:
        return UUID(key_id), secret
    except ValueError:
        raise AppError(401, "unauthorized", "유효한 API Key가 필요합니다.") from None
