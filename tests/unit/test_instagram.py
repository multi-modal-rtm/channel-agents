"""
Unit tests for Instagram integration.

Coverage:
  1. Webhook signature verification
  2. OAuth flow (happy path, mocked httpx)
  3. Token encryption / decryption round-trip
  4. Rate-limit retry logic in InstagramClient
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.meta.webhook_handler import verify_signature


# ── 1. Signature verification ─────────────────────────────────────────────────

_SECRET = "test_app_secret"


def _make_sig(body: bytes, secret: str = _SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_valid():
    body = b'{"object":"instagram","entry":[]}'
    assert verify_signature(body, _SECRET, _make_sig(body)) is True


def test_signature_invalid_value():
    body = b'{"object":"instagram","entry":[]}'
    assert verify_signature(body, _SECRET, "sha256=deadbeef") is False


def test_signature_missing_header():
    body = b'{"test":1}'
    assert verify_signature(body, _SECRET, None) is False


def test_signature_missing_prefix():
    body = b'{"test":1}'
    raw_hex = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    # Header without "sha256=" prefix must be rejected.
    assert verify_signature(body, _SECRET, raw_hex) is False


def test_signature_wrong_secret():
    body = b'{"test":1}'
    sig_with_different_secret = _make_sig(body, "other_secret")
    assert verify_signature(body, _SECRET, sig_with_different_secret) is False


def test_signature_tampered_body():
    original = b'{"object":"instagram"}'
    tampered = b'{"object":"instagram","extra":"injected"}'
    sig = _make_sig(original)
    assert verify_signature(tampered, _SECRET, sig) is False


# ── 2. OAuth — happy path ─────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_exchange_code_for_token_happy_path():
    from app.integrations.meta.oauth import exchange_code_for_token

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "short_tok_abc123", "token_type": "bearer"}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=mock_response)

    with (
        patch.object(type(mock_http), "__aenter__", return_value=mock_http),
        patch.object(type(mock_http), "__aexit__", return_value=False),
        patch("app.integrations.meta.oauth.settings") as mock_settings,
    ):
        mock_settings.meta_app_id = "123456"
        mock_settings.meta_app_secret = "appsecret"

        token = await exchange_code_for_token("auth_code_xyz", "https://app.example.com/cb", _http=mock_http)

    assert token == "short_tok_abc123"
    mock_http.get.assert_called_once()
    call_kwargs = mock_http.get.call_args[1]["params"]
    assert call_kwargs["code"] == "auth_code_xyz"
    assert call_kwargs["client_id"] == "123456"


@pytest.mark.anyio
async def test_exchange_for_long_lived_token_returns_tuple():
    from app.integrations.meta.oauth import exchange_for_long_lived_token

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "long_tok_EAA",
        "token_type": "bearer",
        "expires_in": 5183944,
    }

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=mock_response)

    with patch("app.integrations.meta.oauth.settings") as mock_settings:
        mock_settings.meta_app_id = "123456"
        mock_settings.meta_app_secret = "appsecret"

        token, expires = await exchange_for_long_lived_token("short_tok", _http=mock_http)

    assert token == "long_tok_EAA"
    assert expires == 5183944


@pytest.mark.anyio
async def test_get_page_access_tokens_returns_list():
    from app.integrations.meta.oauth import get_page_access_tokens

    pages = [
        {"id": "111", "name": "Test Page", "access_token": "page_tok_111",
         "instagram_business_account": {"id": "ig_111"}},
    ]
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": pages}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=mock_response)

    result = await get_page_access_tokens("user_tok", _http=mock_http)

    assert len(result) == 1
    assert result[0]["id"] == "111"


@pytest.mark.anyio
async def test_oauth_error_raises_meta_oauth_error():
    from app.integrations.meta.oauth import MetaOAuthError, exchange_code_for_token

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 400
    mock_response.json.return_value = {"error": {"message": "Invalid code", "code": 100}}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=mock_response)

    with (
        patch("app.integrations.meta.oauth.settings") as mock_settings,
        pytest.raises(MetaOAuthError),
    ):
        mock_settings.meta_app_id = "123456"
        mock_settings.meta_app_secret = "appsecret"
        await exchange_code_for_token("bad_code", "https://example.com/cb", _http=mock_http)


def test_get_authorization_url_raises_without_app_id():
    from app.integrations.meta.oauth import MetaOAuthError, get_authorization_url

    with (
        patch("app.integrations.meta.oauth.settings") as mock_settings,
        pytest.raises(MetaOAuthError, match="META_APP_ID"),
    ):
        mock_settings.meta_app_id = None
        get_authorization_url(uuid.uuid4(), "https://example.com/cb")


def test_get_authorization_url_contains_required_params():
    from app.integrations.meta.oauth import get_authorization_url

    tenant_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    with patch("app.integrations.meta.oauth.settings") as mock_settings:
        mock_settings.meta_app_id = "my_app_id"
        url = get_authorization_url(tenant_id, "https://example.com/cb")

    assert "my_app_id" in url
    assert str(tenant_id) in url
    assert "instagram_basic" in url
    assert "dialog/oauth" in url


# ── 3. Token encryption / decryption round-trip ───────────────────────────────

def test_token_encrypt_decrypt_round_trip():
    from app.core.security import decrypt_api_key, encrypt_api_key

    token = "EAABsbCS1iHgBOxxx_page_access_token_value"
    encrypted = encrypt_api_key(token)
    assert isinstance(encrypted, bytes)
    assert encrypted != token.encode()  # must not store plaintext
    assert decrypt_api_key(encrypted) == token


def test_different_tokens_produce_different_ciphertext():
    from app.core.security import encrypt_api_key

    ct1 = encrypt_api_key("token_one")
    ct2 = encrypt_api_key("token_two")
    assert ct1 != ct2


def test_fernet_uses_random_iv_each_call():
    from app.core.security import encrypt_api_key

    # Two encryptions of the same plaintext must differ (Fernet uses random IV).
    token = "EAA_same_token"
    assert encrypt_api_key(token) != encrypt_api_key(token)


# ── 4. Rate-limit retry logic ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_rate_limit_retries_and_succeeds():
    """429 on first two attempts, 200 on third — should return the success body."""
    from app.integrations.meta.client import InstagramClient

    _429 = MagicMock(spec=httpx.Response)
    _429.status_code = 429
    _429.headers = {}

    _200 = MagicMock(spec=httpx.Response)
    _200.status_code = 200
    _200.headers = {}
    _200.json.return_value = {"recipient_id": "u1", "message_id": "mid_1"}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.request = AsyncMock(side_effect=[_429, _429, _200])

    client = InstagramClient(
        page_id="page_1",
        access_token="tok",
        _http=mock_http,
        _backoff_base=0,  # zero sleep in tests
    )

    result = await client.send_message("u1", "hello")

    assert result["message_id"] == "mid_1"
    assert mock_http.request.call_count == 3


@pytest.mark.anyio
async def test_rate_limit_exhausted_raises():
    """Three consecutive 429s should raise InstagramAPIError."""
    from app.integrations.meta.client import InstagramAPIError, InstagramClient

    _429 = MagicMock(spec=httpx.Response)
    _429.status_code = 429
    _429.headers = {}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.request = AsyncMock(return_value=_429)

    client = InstagramClient(
        page_id="page_1",
        access_token="tok",
        _http=mock_http,
        _backoff_base=0,
    )

    with pytest.raises(InstagramAPIError) as exc_info:
        await client.send_message("u1", "hello")

    assert exc_info.value.status_code == 429
    assert mock_http.request.call_count == 3


@pytest.mark.anyio
async def test_server_error_retries():
    """500 on first attempt, 200 on second — one retry succeeds."""
    from app.integrations.meta.client import InstagramClient

    _500 = MagicMock(spec=httpx.Response)
    _500.status_code = 500
    _500.headers = {}

    _200 = MagicMock(spec=httpx.Response)
    _200.status_code = 200
    _200.headers = {}
    _200.json.return_value = {"id": "comment_reply_1"}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.request = AsyncMock(side_effect=[_500, _200])

    client = InstagramClient(page_id="p", access_token="t", _http=mock_http, _backoff_base=0)
    result = await client.reply_to_comment("comment_1", "Thanks!")

    assert result["id"] == "comment_reply_1"
    assert mock_http.request.call_count == 2


@pytest.mark.anyio
async def test_4xx_raises_without_retry():
    """4xx errors (other than 429) raise immediately without retrying."""
    from app.integrations.meta.client import InstagramAPIError, InstagramClient

    _403 = MagicMock(spec=httpx.Response)
    _403.status_code = 403
    _403.headers = {}
    _403.json.return_value = {"error": {"message": "Permission denied", "code": 10}}

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    mock_http.request = AsyncMock(return_value=_403)

    client = InstagramClient(page_id="p", access_token="t", _http=mock_http, _backoff_base=0)

    with pytest.raises(InstagramAPIError) as exc_info:
        await client.get_user_profile("u1")

    assert exc_info.value.status_code == 403
    assert mock_http.request.call_count == 1  # no retry
