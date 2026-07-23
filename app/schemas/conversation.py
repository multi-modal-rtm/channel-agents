from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class MessageOut(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    role: str
    content: str
    agent_id: UUID | None
    human_user_id: UUID | None
    cost_usd: float | None
    created_at: datetime


class ConversationOut(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    channel: str
    customer_handle: str
    customer_name: str | None
    status: str
    last_message_at: datetime | None
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut]


class ConversationPage(BaseModel):
    items: list[ConversationOut]
    total: int
    page: int
    page_size: int


class SendMessageRequest(BaseModel):
    content: str


class EscalateRequest(BaseModel):
    reason: str = ""
