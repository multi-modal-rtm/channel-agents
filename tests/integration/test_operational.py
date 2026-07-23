"""
Integration tests: operational layer.

Tests:
1. Pause endpoint stops new agent invocations
2. Cost aggregation correct across multiple llm_calls rows
3. Safety job triggers auto-pause when budget exceeded
4. Logs do not contain raw API keys or passwords
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text

from tests.integration.conftest import TENANT_A_ID, USER_A_ID, USER_PW

TENANT_SLUG = "alpha-int"


# ── Auth helper ───────────────────────────────────────────────────────────────

async def _login(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@alpha-int.com", "password": USER_PW},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def authed_client(client: AsyncClient) -> AsyncClient:
    token = await _login(client)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest_asyncio.fixture(autouse=True)
async def reset_tenant_status(test_session_factory):
    """Ensure tenant A is active before each test."""
    async with test_session_factory() as session:
        await session.execute(
            text("UPDATE tenants SET status='active', autonomous_actions_enabled=true WHERE id=:tid"),
            {"tid": str(TENANT_A_ID)},
        )
        await session.commit()
    yield
    # Restore after test
    async with test_session_factory() as session:
        await session.execute(
            text("UPDATE tenants SET status='active', autonomous_actions_enabled=true WHERE id=:tid"),
            {"tid": str(TENANT_A_ID)},
        )
        await session.commit()


# ── 1. Pause endpoint stops new agent invocations ─────────────────────────────

async def test_pause_endpoint_sets_status_paused(
    authed_client: AsyncClient,
    test_session_factory,
):
    resp = await authed_client.post("/api/v1/tenants/me/pause")
    assert resp.status_code == 204

    from app.db.models.tenant import Tenant
    async with test_session_factory() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == TENANT_A_ID))
    assert tenant.status == "paused"
    assert tenant.autonomous_actions_enabled is False


async def test_paused_tenant_drops_background_task(
    authed_client: AsyncClient,
    test_session_factory,
):
    """Background task must exit early when tenant.status == 'paused'."""
    # Pause the tenant
    await authed_client.post("/api/v1/tenants/me/pause")

    # Seed a conversation so the task has something to work with
    from app.db.models.conversation import Conversation, Message
    conv_id = uuid.uuid4()
    async with test_session_factory() as session:
        session.add(Conversation(
            id=conv_id,
            tenant_id=TENANT_A_ID,
            channel="telegram",
            customer_handle="test_pause_user",
        ))
        session.add(Message(
            tenant_id=TENANT_A_ID,
            conversation_id=conv_id,
            role="user",
            content="test message while paused",
        ))
        await session.commit()

    mock_llm = AsyncMock()
    mock_llm.chat = AsyncMock()  # should never be called

    with patch(
        "app.workers.tasks.handle_incoming_message.TenantAwareAnthropicClient.create",
        new_callable=AsyncMock,
        return_value=mock_llm,
    ):
        from app.workers.tasks.handle_incoming_message import handle_incoming_message
        await handle_incoming_message(
            {},
            tenant_id=str(TENANT_A_ID),
            conversation_id=str(conv_id),
            message_text="test message while paused",
            channel="telegram",
            chat_id=12345,
        )

    # LLM client was created but chat() was never called (task dropped early)
    mock_llm.chat.assert_not_called()


async def test_resume_endpoint_restores_active(
    authed_client: AsyncClient,
    test_session_factory,
):
    await authed_client.post("/api/v1/tenants/me/pause")
    resp = await authed_client.post("/api/v1/tenants/me/resume")
    assert resp.status_code == 204

    from app.db.models.tenant import Tenant
    async with test_session_factory() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.id == TENANT_A_ID))
    assert tenant.status == "active"
    assert tenant.autonomous_actions_enabled is True


async def test_pause_writes_audit_log(authed_client: AsyncClient, test_session_factory):
    await authed_client.post("/api/v1/tenants/me/pause")

    from app.db.models.audit_log import AuditLog
    async with test_session_factory() as session:
        await session.execute(
            text(f"SET LOCAL app.tenant_id = '{TENANT_A_ID}'")
        )
        logs = await session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == TENANT_A_ID,
                AuditLog.action == "tenant.pause",
            )
        )
        rows = logs.scalars().all()
    assert len(rows) >= 1


# ── 2. Cost aggregation ───────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def seeded_llm_calls(test_session_factory):
    """Insert known llm_calls rows for TENANT_A for today."""
    from app.db.models.llm_call import LLMCall

    async with test_session_factory() as session:
        for i, (model, tin, tout, cost, success) in enumerate([
            ("claude-sonnet-4-6", 100, 50, 0.001650, True),
            ("claude-sonnet-4-6", 200, 80, 0.002900, True),
            ("claude-haiku-4-5",  500, 100, 0.000800, True),
            ("claude-haiku-4-5",  200, 50,  0.000360, False),
        ]):
            session.add(LLMCall(
                tenant_id=TENANT_A_ID,
                model=model,
                tokens_in=tin,
                tokens_out=tout,
                cost_usd=cost,
                latency_ms=100 + i * 10,
                success=success,
            ))
        await session.commit()
    yield
    # Cleanup
    async with test_session_factory() as session:
        await session.execute(
            text(f"DELETE FROM llm_calls WHERE tenant_id = '{TENANT_A_ID}'")
        )
        await session.commit()


async def test_usage_today_aggregates_cost(
    authed_client: AsyncClient,
    seeded_llm_calls,
):
    resp = await authed_client.get("/api/v1/tenants/me/usage/today")
    assert resp.status_code == 200
    data = resp.json()
    # 0.001650 + 0.002900 + 0.000800 + 0.000360 = 0.005710
    assert abs(data["cost_usd"] - 0.005710) < 0.0001
    assert data["call_count"] == 4


async def test_usage_period_breakdown(
    authed_client: AsyncClient,
    seeded_llm_calls,
):
    today = datetime.now(UTC).date().isoformat()
    resp = await authed_client.get(
        f"/api/v1/tenants/me/usage?from={today}&to={today}"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_calls"] == 4
    assert data["total_errors"] == 1
    assert abs(data["total_cost_usd"] - 0.005710) < 0.0001
    # Breakdown should have 2 distinct models
    models = {b["model"] for b in data["breakdown"]}
    assert "claude-sonnet-4-6" in models
    assert "claude-haiku-4-5" in models


async def test_usage_period_bad_date_range(authed_client: AsyncClient, seeded_llm_calls):
    resp = await authed_client.get("/api/v1/tenants/me/usage?from=2026-05-10&to=2026-05-01")
    assert resp.status_code == 400


# ── 3. Safety job triggers auto-pause when budget exceeded ────────────────────

async def test_safety_job_pauses_tenant_at_95pct_budget(
    test_session_factory,
):
    """Safety job should pause the tenant when spend ≥ 95% of daily budget."""
    from app.db.models.llm_call import LLMCall
    from app.db.models.tenant import Tenant

    budget = 1.0

    # Set daily budget on tenant A
    async with test_session_factory() as session:
        await session.execute(
            text(f"UPDATE tenants SET daily_budget_usd = {budget} WHERE id = '{TENANT_A_ID}'")
        )
        await session.commit()

    # Insert spend that exceeds 95%
    async with test_session_factory() as session:
        session.add(LLMCall(
            tenant_id=TENANT_A_ID,
            model="claude-sonnet-4-6",
            tokens_in=1000,
            tokens_out=500,
            cost_usd=0.96,   # 96% of $1.00
            latency_ms=200,
            success=True,
        ))
        await session.commit()

    try:
        with patch("app.workers.tasks.safety_job._send_alert", new_callable=AsyncMock):
            from app.workers.tasks.safety_job import run_safety_checks
            await run_safety_checks({})

        # Verify tenant is now paused
        async with test_session_factory() as session:
            tenant = await session.scalar(
                select(Tenant).where(Tenant.id == TENANT_A_ID)
            )
        assert tenant.status == "paused"
    finally:
        # Cleanup
        async with test_session_factory() as session:
            await session.execute(
                text(f"DELETE FROM llm_calls WHERE tenant_id = '{TENANT_A_ID}'")
            )
            await session.execute(
                text(f"UPDATE tenants SET daily_budget_usd = NULL, status = 'active',"
                     f" autonomous_actions_enabled = true WHERE id = '{TENANT_A_ID}'")
            )
            await session.commit()


# ── 4. Logs must not contain raw secrets ─────────────────────────────────────

def test_log_output_never_contains_raw_api_key(capsys):
    """Any log line that might contain an api_key must have it scrubbed."""
    import structlog
    from app.core.logging import configure_logging
    configure_logging("development")

    log = structlog.get_logger("security_test")

    # Simulate what anthropic_client.py might log by accident
    log.debug("llm_call", api_key="sk-ant-secret-key-value", model="claude-sonnet-4-6")
    log.warning("auth_failed", password="PlainTextPassword", user="attacker@evil.com")
    log.info("request", authorization="Bearer eyJ.secret.token")

    out = capsys.readouterr().out + capsys.readouterr().err

    for bad_value in ["sk-ant-secret-key-value", "PlainTextPassword", "Bearer eyJ.secret.token"]:
        assert bad_value not in out, f"Secret value leaked in logs: {bad_value!r}"
