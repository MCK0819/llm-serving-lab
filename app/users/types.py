from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class Identity:
    user_id: UUID
    organization_id: UUID


@dataclass(frozen=True)
class StoredCredential:
    identity: Identity
    secret_hash: str = field(repr=False)
