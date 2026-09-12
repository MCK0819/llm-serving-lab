from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

DocumentStatus = Literal["queued", "processing", "ready", "failed"]


class DocumentReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    status: Literal["queued"]
    created_at: datetime


class DocumentItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    filename: str
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime


class DocumentFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str


class DocumentDetail(DocumentItem):
    failure: DocumentFailure | None


class DocumentPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: list[DocumentItem]
    next_cursor: str | None
