from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet
from jose import jwt
from passlib.context import CryptContext

from app.config import settings

# ── Password hashing (Argon2) ─────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────
# Payload shape:
#   access:  {type, sub, tenant_id, role, exp, iat}
#   refresh: {type, sub, tenant_id, exp, iat}


def create_access_token(user_id: str | UUID, tenant_id: str | UUID, role: str) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "type": "access",
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: str | UUID, tenant_id: str | UUID) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "type": "refresh",
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_refresh_token_expire_days),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])


# ── Fernet BYOK key encryption ────────────────────────────────────────────────
# Key is loaded once at boot and held in memory only (Rule 4).
# Never log, never include in error messages, never put in audit_log.
_fernet = Fernet(settings.encryption_key.encode())


def encrypt_api_key(plaintext: str) -> bytes:
    """Encrypt a plaintext Anthropic API key for storage (Rule 4)."""
    return _fernet.encrypt(plaintext.encode())


def decrypt_api_key(ciphertext: bytes) -> str:
    """Decrypt a stored Anthropic API key. Plaintext exists only in memory."""
    return _fernet.decrypt(ciphertext).decode()
