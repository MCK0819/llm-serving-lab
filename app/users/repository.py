from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.users.models import ApiKey, Organization, User
from app.users.types import Identity, StoredCredential


class UserRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def create_organization(self, name: str) -> UUID:
        async with self.sessions.begin() as session:
            organization = Organization(name=name)
            session.add(organization)
            await session.flush()
            return organization.id

    async def create_user(self, organization_id: UUID, name: str) -> UUID:
        async with self.sessions.begin() as session:
            user = User(organization_id=organization_id, name=name)
            session.add(user)
            await session.flush()
            return user.id

    async def find_active_key(self, key_id: UUID) -> StoredCredential | None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(User.id, User.organization_id, ApiKey.secret_hash)
                    .join(ApiKey, ApiKey.user_id == User.id)
                    .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
                )
            ).one_or_none()
            if row is None:
                return None
            return StoredCredential(Identity(row.id, row.organization_id), row.secret_hash)

    async def add_key(self, key_id: UUID, user_id: UUID, secret_hash: str) -> None:
        async with self.sessions.begin() as session:
            session.add(ApiKey(id=key_id, user_id=user_id, secret_hash=secret_hash))

    async def revoke_key(self, key_id: UUID) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(ApiKey)
                .where(ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
                .values(revoked_at=func.now())
            )
