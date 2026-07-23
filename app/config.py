from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_name: str = "channel-agents"
    environment: str = "development"
    debug: bool = False
    secret_key: str
    encryption_key: str  # Fernet key — generate via Fernet.generate_key()

    # Database
    database_url: str  # postgresql+asyncpg://...
    database_pool_size: int = 10
    database_max_overflow: int = 20

    @property
    def database_url_asyncpg(self) -> str:
        """Strip sslmode — asyncpg rejects it as an unknown keyword argument."""
        parsed = urlparse(self.database_url)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items() if k != "sslmode"}
        return urlunparse(parsed._replace(query=urlencode(params)))

    @property
    def database_connect_args(self) -> dict:
        """Return ssl=True when the original URL required SSL (Neon, RDS, etc.)."""
        if "sslmode=require" in self.database_url:
            return {"ssl": True}
        return {}

    # Redis (local: redis://localhost:6379 | Upstash: rediss://...)
    redis_url: str = "redis://localhost:6379"

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection_prefix: str = "tenant_"

    # Anthropic — platform key used for managed-billing tenants (BYOK keys live in DB).
    # Kept as ANTHROPIC_API_KEY_MANAGED to make it explicit this is the platform key.
    anthropic_api_key_managed: str | None = None

    # Langfuse
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # JWT
    jwt_algorithm: str = Field(default="HS256")
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 30

    # Meta / Instagram
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_verify_token: str | None = None

    # Telegram
    telegram_bot_token: str | None = None

    # Voyage AI — embeddings for Qdrant RAG
    voyage_api_key: str | None = None


settings = Settings()
