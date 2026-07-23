from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class TimestampSchema(BaseModel):
    created_at: datetime
    updated_at: datetime


class UUIDSchema(BaseModel):
    id: UUID


class MessageResponse(BaseModel):
    message: str
