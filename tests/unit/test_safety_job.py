"""
Unit tests for the safety background job.

All DB and time dependencies are mocked — no Postgres required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.tasks.safety_job import (
    BUDGET_WARN_PCT,
    CONSECUTIVE_FAILURE_LIMIT,
    ERROR_RATE_THRESHOLD,
    _check_budget,
    _check_consecutive_failures,
    _check_error_rate,
    _count_consecutive_failures,
    run_safety_checks,
)

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
AGENT_ID  = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_tenant(*, status: str = "active", budget: float | None = None) -> MagicMock:
    t = MagicMock()
    t.id = TENANT_ID
    t.slug = "test-tenant"
    t.status = status
    t.daily_budget_usd = budget
    t.autonomous_actions_enabled = status == "active"
    return t


def _mock_session(*, spent: float = 0.0, total_calls: int = 0, errors: int = 0) -> AsyncMock:
    session = AsyncMock()

    scalar_result = MagicMock()
    scalar_result.scalar = MagicMock(return_value=spent)

    one_result = MagicMock()
    one_result.one = MagicMock(return_value=MagicMock(total=total_calls, errors=errors))

    # First execute → budget query (scalar), second → error rate (one)
    session.execute = AsyncMock(side_effect=[scalar_result, one_result])
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


# ── Budget check ──────────────────────────────────────────────────────────────

async def test_budget_no_limit_never_pauses():
    tenant = _mock_tenant(budget=None)
    session = AsyncMock()
    paused = await _check_budget(tenant, session)
    assert paused is False
    session.execute.assert_not_called()


async def test_budget_under_threshold_no_pause():
    tenant = _mock_tenant(budget=10.0)
    session = AsyncMock()
    result = MagicMock()
    result.scalar = MagicMock(return_value=8.0)  # 80% — under 95%
    session.execute = AsyncMock(return_value=result)

    paused = await _check_budget(tenant, session)
    assert paused is False


async def test_budget_exactly_at_threshold_pauses():
    tenant = _mock_tenant(budget=10.0)
    session = AsyncMock()
    result = MagicMock()
    result.scalar = MagicMock(return_value=9.50)  # exactly 95%
    session.execute = AsyncMock(return_value=result)

    with (
        patch("app.workers.tasks.safety_job.get_admin_session") as mock_admin,
        patch("app.workers.tasks.safety_job._send_alert", new_callable=AsyncMock),
    ):
        mock_admin.return_value.__aenter__ = AsyncMock(return_value=AsyncMock(
            execute=AsyncMock(return_value=MagicMock(scalar_one=MagicMock(return_value=tenant))),
            commit=AsyncMock(),
        ))
        mock_admin.return_value.__aexit__ = AsyncMock(return_value=False)

        paused = await _check_budget(tenant, session)

    assert paused is True
    assert session.add.called
    logged = session.add.call_args[0][0]
    assert logged.action == "tenant.auto_pause"


async def test_budget_over_threshold_pauses():
    tenant = _mock_tenant(budget=10.0)
    session = AsyncMock()
    result = MagicMock()
    result.scalar = MagicMock(return_value=9.99)  # 99.9%
    session.execute = AsyncMock(return_value=result)

    with (
        patch("app.workers.tasks.safety_job.get_admin_session") as mock_admin,
        patch("app.workers.tasks.safety_job._send_alert", new_callable=AsyncMock),
    ):
        db_tenant = MagicMock()
        db_tenant.status = "active"
        admin_session = AsyncMock()
        admin_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one=MagicMock(return_value=db_tenant))
        )
        admin_session.commit = AsyncMock()
        mock_admin.return_value.__aenter__ = AsyncMock(return_value=admin_session)
        mock_admin.return_value.__aexit__ = AsyncMock(return_value=False)

        paused = await _check_budget(tenant, session)

    assert paused is True
    assert db_tenant.status == "paused"


# ── Error rate check ──────────────────────────────────────────────────────────

async def test_error_rate_too_few_calls_ignored():
    tenant = _mock_tenant()
    session = AsyncMock()
    result = MagicMock()
    result.one = MagicMock(return_value=MagicMock(total=3, errors=3))  # 100% but < 5 calls
    session.execute = AsyncMock(return_value=result)

    paused = await _check_error_rate(tenant, session)
    assert paused is False


async def test_error_rate_under_threshold_no_pause():
    tenant = _mock_tenant()
    session = AsyncMock()
    result = MagicMock()
    result.one = MagicMock(return_value=MagicMock(total=20, errors=2))  # 10%
    session.execute = AsyncMock(return_value=result)

    paused = await _check_error_rate(tenant, session)
    assert paused is False


async def test_error_rate_over_threshold_pauses():
    tenant = _mock_tenant()
    session = AsyncMock()
    result = MagicMock()
    result.one = MagicMock(return_value=MagicMock(total=10, errors=3))  # 30%
    session.execute = AsyncMock(return_value=result)

    with (
        patch("app.workers.tasks.safety_job.get_admin_session") as mock_admin,
        patch("app.workers.tasks.safety_job._send_alert", new_callable=AsyncMock),
    ):
        db_tenant = MagicMock()
        admin_session = AsyncMock()
        admin_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one=MagicMock(return_value=db_tenant))
        )
        admin_session.commit = AsyncMock()
        mock_admin.return_value.__aenter__ = AsyncMock(return_value=admin_session)
        mock_admin.return_value.__aexit__ = AsyncMock(return_value=False)

        paused = await _check_error_rate(tenant, session)

    assert paused is True


# ── Consecutive failure count ─────────────────────────────────────────────────

async def test_count_consecutive_zero_when_last_is_success():
    session = AsyncMock()
    # Most recent call succeeded
    session.execute = AsyncMock(
        return_value=MagicMock(all=MagicMock(return_value=[(True,), (False,), (False,)]))
    )
    count = await _count_consecutive_failures(AGENT_ID, TENANT_ID, session)
    assert count == 0


async def test_count_consecutive_counts_from_most_recent():
    session = AsyncMock()
    # 4 consecutive failures, then a success
    session.execute = AsyncMock(
        return_value=MagicMock(
            all=MagicMock(return_value=[(False,), (False,), (False,), (False,), (True,)])
        )
    )
    count = await _count_consecutive_failures(AGENT_ID, TENANT_ID, session)
    assert count == 4


async def test_count_consecutive_all_failures():
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(
            all=MagicMock(return_value=[(False,)] * 12)
        )
    )
    count = await _count_consecutive_failures(AGENT_ID, TENANT_ID, session)
    assert count == 12


# ── run_safety_checks skips paused tenants ────────────────────────────────────

async def test_run_safety_checks_only_active_tenants():
    tenant_active = _mock_tenant(status="active")

    with (
        patch("app.workers.tasks.safety_job.get_admin_session") as mock_admin,
        patch("app.workers.tasks.safety_job._check_tenant", new_callable=AsyncMock) as mock_check,
    ):
        admin_session = AsyncMock()
        admin_session.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(
                return_value=MagicMock(all=MagicMock(return_value=[tenant_active]))
            ))
        )
        mock_admin.return_value.__aenter__ = AsyncMock(return_value=admin_session)
        mock_admin.return_value.__aexit__ = AsyncMock(return_value=False)

        await run_safety_checks({})

    mock_check.assert_called_once_with(tenant_active)
