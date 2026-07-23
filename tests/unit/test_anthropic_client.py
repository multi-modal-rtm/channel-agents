"""
Unit tests for TenantAwareAnthropicClient.

Anthropic SDK and DB sessions are fully mocked — no network, no DB required.
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, call, patch

import anthropic
import httpx
import pytest
from tenacity import wait_none

from app.integrations.anthropic_client import (
    MODEL_HAIKU,
    MODEL_OPUS,
    MODEL_SONNET,
    BudgetExceededError,
    TenantAwareAnthropicClient,
    _compute_cost,
)

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_response(*, input_tokens: int = 100, output_tokens: int = 50) -> MagicMock:
    r = MagicMock(spec=anthropic.types.Message)
    r.usage = MagicMock()
    r.usage.input_tokens  = input_tokens
    r.usage.output_tokens = output_tokens
    return r


def _make_session(*, spent: float = 0.0) -> AsyncMock:
    """Mock DB session. execute().scalar() returns ``spent``."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    # AsyncMock makes sub-attributes async by default; scalar() must be sync.
    session.execute.return_value.scalar = MagicMock(return_value=spent)
    return session


def _make_tenant_session_ctx(session: AsyncMock):
    """Return a drop-in replacement for get_tenant_session()."""
    @asynccontextmanager
    async def _ctx():
        yield session
    return _ctx


def _make_client(
    *,
    mock_anthropic: AsyncMock,
    budget: float | None = None,
    session: AsyncMock | None = None,
) -> tuple[TenantAwareAnthropicClient, AsyncMock]:
    """Build a client with injected mocks; return (client, session)."""
    if session is None:
        session = _make_session()
    client = TenantAwareAnthropicClient(
        tenant_id=TENANT_ID,
        _anthropic_client=mock_anthropic,
        _daily_budget_usd=budget,
        _retry_wait=wait_none(),
    )
    return client, session


# ── 1. Cost calculation ────────────────────────────────────────────────────────

def test_cost_opus_input_only():
    cost = _compute_cost(MODEL_OPUS, 1_000_000, 0)
    assert cost == pytest.approx(15.0)


def test_cost_opus_full():
    cost = _compute_cost(MODEL_OPUS, 1_000_000, 1_000_000)
    assert cost == pytest.approx(90.0)  # 15 + 75


def test_cost_sonnet():
    cost = _compute_cost(MODEL_SONNET, 1_000_000, 1_000_000)
    assert cost == pytest.approx(18.0)  # 3 + 15


def test_cost_haiku_short_id():
    cost = _compute_cost("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(4.80)  # 0.8 + 4.0


def test_cost_haiku_versioned_id():
    # The real model ID includes a date suffix; prefix match must still work.
    cost = _compute_cost(MODEL_HAIKU, 1_000_000, 1_000_000)
    assert cost == pytest.approx(4.80)


def test_cost_small_call():
    # 200 in + 100 out on sonnet
    cost = _compute_cost(MODEL_SONNET, 200, 100)
    expected = 200 * 3.0 / 1_000_000 + 100 * 15.0 / 1_000_000
    assert cost == pytest.approx(expected)


def test_cost_unknown_model_returns_zero():
    cost = _compute_cost("claude-unknown-99", 1_000, 1_000)
    assert cost == 0.0


# ── 2. llm_calls row created with correct tenant_id ──────────────────────────

async def test_chat_creates_llm_call_row():
    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(return_value=_make_response())
    session = _make_session()
    client, _ = _make_client(mock_anthropic=mock_anthropic, session=session)

    with patch(
        "app.integrations.anthropic_client.get_tenant_session",
        _make_tenant_session_ctx(session),
    ):
        await client.chat([{"role": "user", "content": "hi"}])

    assert session.add.called
    llm_call_obj = session.add.call_args[0][0]
    from app.db.models.llm_call import LLMCall
    assert isinstance(llm_call_obj, LLMCall)
    assert llm_call_obj.tenant_id == TENANT_ID


async def test_chat_llm_call_has_correct_cost():
    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(
        return_value=_make_response(input_tokens=1_000_000, output_tokens=1_000_000)
    )
    session = _make_session()
    client, _ = _make_client(mock_anthropic=mock_anthropic, session=session)

    with patch(
        "app.integrations.anthropic_client.get_tenant_session",
        _make_tenant_session_ctx(session),
    ):
        await client.chat([{"role": "user", "content": "hi"}], model=MODEL_SONNET)

    llm_call_obj = session.add.call_args[0][0]
    assert llm_call_obj.cost_usd == pytest.approx(18.0)  # $3 in + $15 out
    assert llm_call_obj.tokens_in  == 1_000_000
    assert llm_call_obj.tokens_out == 1_000_000
    assert llm_call_obj.success is True


async def test_chat_marks_failed_on_exception():
    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(
        side_effect=anthropic.InternalServerError(
            "boom",
            response=httpx.Response(500, request=httpx.Request("POST", "https://api.anthropic.com")),
            body={},
        )
    )
    session = _make_session()
    client = TenantAwareAnthropicClient(
        tenant_id=TENANT_ID,
        _anthropic_client=mock_anthropic,
        _retry_wait=wait_none(),
    )

    with (
        patch("app.integrations.anthropic_client.get_tenant_session", _make_tenant_session_ctx(session)),
        pytest.raises(anthropic.InternalServerError),
    ):
        await client.chat([{"role": "user", "content": "hi"}])

    llm_call_obj = session.add.call_args[0][0]
    assert llm_call_obj.success is False


# ── 3. BYOK vs managed routing ───────────────────────────────────────────────

async def test_create_byok_uses_decrypted_key():
    from app.core.security import encrypt_api_key

    raw_key = "sk-ant-byok-test-key-xyz"
    tenant = MagicMock(spec=["id", "billing_mode", "anthropic_key_encrypted", "daily_budget_usd"])
    tenant.id = TENANT_ID
    tenant.billing_mode = "byok"
    tenant.anthropic_key_encrypted = encrypt_api_key(raw_key)
    tenant.daily_budget_usd = None

    mock_session = AsyncMock()
    mock_session.execute.return_value.scalar_one_or_none = MagicMock(return_value=tenant)

    @asynccontextmanager
    async def _mock_admin():
        yield mock_session

    with (
        patch("app.integrations.anthropic_client.get_admin_session", _mock_admin),
        patch("anthropic.AsyncAnthropic") as MockSDK,
    ):
        await TenantAwareAnthropicClient.create(TENANT_ID)

    MockSDK.assert_called_once_with(api_key=raw_key)


async def test_create_managed_uses_platform_key():
    tenant = MagicMock(spec=["id", "billing_mode", "anthropic_key_encrypted", "daily_budget_usd"])
    tenant.id = TENANT_ID
    tenant.billing_mode = "managed"
    tenant.daily_budget_usd = None

    mock_session = AsyncMock()
    mock_session.execute.return_value.scalar_one_or_none = MagicMock(return_value=tenant)

    @asynccontextmanager
    async def _mock_admin():
        yield mock_session

    with (
        patch("app.integrations.anthropic_client.get_admin_session", _mock_admin),
        patch("app.integrations.anthropic_client.settings") as mock_settings,
        patch("anthropic.AsyncAnthropic") as MockSDK,
    ):
        mock_settings.anthropic_api_key_managed = "sk-ant-platform-key"
        await TenantAwareAnthropicClient.create(TENANT_ID)

    MockSDK.assert_called_once_with(api_key="sk-ant-platform-key")


async def test_create_managed_no_platform_key_raises():
    tenant = MagicMock(spec=["id", "billing_mode", "anthropic_key_encrypted", "daily_budget_usd"])
    tenant.id = TENANT_ID
    tenant.billing_mode = "managed"
    tenant.daily_budget_usd = None

    mock_session = AsyncMock()
    mock_session.execute.return_value.scalar_one_or_none = MagicMock(return_value=tenant)

    @asynccontextmanager
    async def _mock_admin():
        yield mock_session

    with (
        patch("app.integrations.anthropic_client.get_admin_session", _mock_admin),
        patch("app.integrations.anthropic_client.settings") as mock_settings,
        pytest.raises(ValueError, match="ANTHROPIC_API_KEY_MANAGED"),
    ):
        mock_settings.anthropic_api_key_managed = None
        await TenantAwareAnthropicClient.create(TENANT_ID)


# ── 4. Budget guard ────────────────────────────────────────────────────────────

async def test_budget_exceeded_blocks_call():
    mock_anthropic = AsyncMock()
    session = _make_session(spent=12.50)  # over 10.00 limit
    client, _ = _make_client(mock_anthropic=mock_anthropic, budget=10.0, session=session)

    with (
        patch("app.integrations.anthropic_client.get_tenant_session", _make_tenant_session_ctx(session)),
        pytest.raises(BudgetExceededError) as exc_info,
    ):
        await client.chat([{"role": "user", "content": "hi"}])

    mock_anthropic.messages.create.assert_not_called()
    assert exc_info.value.spent == pytest.approx(12.50)
    assert exc_info.value.limit == pytest.approx(10.0)


async def test_budget_not_set_allows_call():
    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(return_value=_make_response())
    session = _make_session(spent=9999.0)  # massive spend, but no limit set
    client, _ = _make_client(mock_anthropic=mock_anthropic, budget=None, session=session)

    with patch(
        "app.integrations.anthropic_client.get_tenant_session",
        _make_tenant_session_ctx(session),
    ):
        result = await client.chat([{"role": "user", "content": "hi"}])

    assert result is not None
    mock_anthropic.messages.create.assert_called_once()


async def test_budget_at_80_pct_warns_but_does_not_block():
    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(return_value=_make_response())
    session = _make_session(spent=8.5)  # 85% of $10 limit → warning, not block
    client, _ = _make_client(mock_anthropic=mock_anthropic, budget=10.0, session=session)

    with (
        patch("app.integrations.anthropic_client.get_tenant_session", _make_tenant_session_ctx(session)),
        patch.object(client, "_write_llm_call", new=AsyncMock()),  # skip DB write
    ):
        result = await client.chat([{"role": "user", "content": "hi"}])

    assert result is not None
    mock_anthropic.messages.create.assert_called_once()


# ── 5. Retry logic ────────────────────────────────────────────────────────────

async def test_retries_on_429_succeeds_third_attempt():
    _429 = anthropic.RateLimitError(
        "rate limited",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com")),
        body={},
    )
    good = _make_response(input_tokens=10, output_tokens=5)

    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(side_effect=[_429, _429, good])

    session = _make_session()
    client, _ = _make_client(mock_anthropic=mock_anthropic, session=session)

    with patch(
        "app.integrations.anthropic_client.get_tenant_session",
        _make_tenant_session_ctx(session),
    ):
        result = await client.chat([{"role": "user", "content": "hi"}])

    assert mock_anthropic.messages.create.call_count == 3
    assert result is good


async def test_retries_on_500():
    _500 = anthropic.InternalServerError(
        "server error",
        response=httpx.Response(500, request=httpx.Request("POST", "https://api.anthropic.com")),
        body={},
    )
    good = _make_response()

    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(side_effect=[_500, good])

    session = _make_session()
    client, _ = _make_client(mock_anthropic=mock_anthropic, session=session)

    with patch(
        "app.integrations.anthropic_client.get_tenant_session",
        _make_tenant_session_ctx(session),
    ):
        result = await client.chat([{"role": "user", "content": "hi"}])

    assert mock_anthropic.messages.create.call_count == 2
    assert result is good


async def test_no_retry_on_400():
    _400 = anthropic.BadRequestError(
        "bad request",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")),
        body={},
    )
    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(side_effect=_400)

    session = _make_session()
    client, _ = _make_client(mock_anthropic=mock_anthropic, session=session)

    with (
        patch("app.integrations.anthropic_client.get_tenant_session", _make_tenant_session_ctx(session)),
        pytest.raises(anthropic.BadRequestError),
    ):
        await client.chat([{"role": "user", "content": "hi"}])

    # Only 1 attempt — 400 is not retryable
    assert mock_anthropic.messages.create.call_count == 1


async def test_raises_after_max_retries():
    _429 = anthropic.RateLimitError(
        "still rate limited",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com")),
        body={},
    )
    mock_anthropic = AsyncMock()
    mock_anthropic.messages.create = AsyncMock(side_effect=_429)  # always fails

    session = _make_session()
    client, _ = _make_client(mock_anthropic=mock_anthropic, session=session)

    with (
        patch("app.integrations.anthropic_client.get_tenant_session", _make_tenant_session_ctx(session)),
        pytest.raises(anthropic.RateLimitError),
    ):
        await client.chat([{"role": "user", "content": "hi"}])

    assert mock_anthropic.messages.create.call_count == 3  # max attempts
