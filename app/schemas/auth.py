import re

from pydantic import BaseModel, EmailStr, field_validator


class TenantRegisterRequest(BaseModel):
    name: str
    slug: str
    email: EmailStr
    password: str

    @field_validator("slug")
    @classmethod
    def slug_must_be_url_safe(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9][a-z0-9\-]{1,98}[a-z0-9]$", v):
            raise ValueError("slug must be 3-100 chars, lowercase alphanumeric and hyphens only")
        return v

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str
