"""
Tests for the structlog scrubbing processor.

Verifies that raw API keys, passwords, and tokens never appear
in log output regardless of which key name is used.
"""

from __future__ import annotations

import re

from app.core.logging import _scrub_sensitive, _REDACTED


# ── Exact key matches ─────────────────────────────────────────────────────────

def test_scrubs_api_key():
    event = {"api_key": "sk-ant-api01-secret", "message": "hello"}
    result = _scrub_sensitive(None, None, event)
    assert result["api_key"] == _REDACTED
    assert result["message"] == "hello"


def test_scrubs_password():
    event = {"password": "SuperSecret123!", "user": "alice"}
    result = _scrub_sensitive(None, None, event)
    assert result["password"] == _REDACTED
    assert result["user"] == "alice"


def test_scrubs_token():
    event = {"token": "eyJhbGciOiJIUzI1NiJ9.payload.sig"}
    result = _scrub_sensitive(None, None, event)
    assert result["token"] == _REDACTED


def test_scrubs_access_token():
    event = {"access_token": "tok_abc123", "status": "ok"}
    result = _scrub_sensitive(None, None, event)
    assert result["access_token"] == _REDACTED


def test_scrubs_authorization():
    event = {"authorization": "Bearer sk-secret"}
    result = _scrub_sensitive(None, None, event)
    assert result["authorization"] == _REDACTED


def test_scrubs_secret_key():
    event = {"secret_key": "fernet_key_value"}
    result = _scrub_sensitive(None, None, event)
    assert result["secret_key"] == _REDACTED


# ── Pattern matches (key names containing sensitive words) ────────────────────

def test_scrubs_anthropic_key_partial_match():
    event = {"anthropic_key_encrypted": b"ciphertext"}
    result = _scrub_sensitive(None, None, event)
    assert result["anthropic_key_encrypted"] == _REDACTED


def test_scrubs_webhook_secret():
    event = {"webhook_secret": "topsecret"}
    result = _scrub_sensitive(None, None, event)
    assert result["webhook_secret"] == _REDACTED


def test_scrubs_arbitrary_password_variant():
    event = {"db_password": "postgres123"}
    result = _scrub_sensitive(None, None, event)
    assert result["db_password"] == _REDACTED


# ── Safe keys are preserved ───────────────────────────────────────────────────

def test_preserves_safe_fields():
    event = {
        "tenant_id": "aaaa-...",
        "model": "claude-sonnet-4-6",
        "cost_usd": 0.001,
        "event": "llm_call",
        "level": "debug",
    }
    result = _scrub_sensitive(None, None, event)
    assert result == event


def test_preserves_user_field():
    event = {"user": "owner@example.com", "action": "login"}
    result = _scrub_sensitive(None, None, event)
    assert result["user"] == "owner@example.com"


# ── Log output integration: scrubbing happens before any renderer ─────────────

def test_scrubbing_in_log_output(capsys):
    """Verify no raw secret appears when structlog actually renders a line."""
    import structlog
    from app.core.logging import configure_logging
    configure_logging("development")

    log = structlog.get_logger("test_scrub")
    log.info("auth_attempt", password="hunter2", user="alice")

    captured = capsys.readouterr().out + capsys.readouterr().err
    # The rendered output should not contain the raw password
    # (structlog dev renderer goes to stdout)
    # We check the event dict was scrubbed — if structlog printed anything,
    # it must not include "hunter2"
    assert "hunter2" not in captured
