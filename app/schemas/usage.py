from datetime import date
from uuid import UUID

from pydantic import BaseModel


class UsageBreakdownItem(BaseModel):
    agent_id: UUID | None
    model: str
    call_count: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    error_count: int


class UsageResponse(BaseModel):
    from_date: date
    to_date: date
    total_cost_usd: float
    total_calls: int
    total_errors: int
    breakdown: list[UsageBreakdownItem]


class TodayUsageResponse(BaseModel):
    date: date
    cost_usd: float
    call_count: int
    budget_usd: float | None
    budget_pct: float | None  # None when no budget set
