from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AgentResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    type: str
    name: str
    system_prompt: str | None
    config_json: dict
    enabled: bool
    autonomy_level: int
    created_at: datetime
    updated_at: datetime


class AgentUpdateRequest(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    config_json: dict | None = None
    autonomy_level: int | None = Field(None, ge=0, le=2)
    enabled: bool | None = None


class AgentTestRequest(BaseModel):
    payload: str
    input_type: str = "message"
    channel: str = "internal"


class AgentTestResponse(BaseModel):
    response_text: str | None
    confidence: float
    escalate_to_human: bool
    cost_usd: float
    actions_taken: list[str]
