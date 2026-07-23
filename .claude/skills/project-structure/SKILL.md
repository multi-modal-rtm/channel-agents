# Project Structure

## What this project is
Multi-tenant SaaS for AI-driven Instagram/Telegram sales automation, targeting Uzbek SMEs. First tenant is a textile manufacturer. Architecture: shared infrastructure, isolated by tenant_id, RLS-enforced at the database layer.

## Stack (do not deviate without asking)
- Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, Pydantic v2
- PostgreSQL 16 with Row-Level Security
- Qdrant for vector search (one collection per tenant)
- Redis for cache/queue (keys prefixed with tenant_id)
- Anthropic API only for LLM (claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5)
- Langfuse + structlog for observability
- arq or celery for background jobs (pick one, stick with it)

## Conventions
- Async everywhere. No sync DB calls in request handlers.
- Type hints on every function signature.
- Pydantic v2 for all request/response and config.
- Structured logging only — no `print`, no f-string log messages with sensitive data.
- Settings via pydantic-settings, never os.getenv directly in business logic.
- Every external service call has a timeout and a retry policy.
- Tests live next to features they cover; integration tests separately.
- Migrations are append-only — never edit a migration once committed.

## Code organization rules
- `app/api/` is thin: parse request, call service, return response. No business logic.
- `app/services/` contains business logic. Stateless functions or stateless classes.
- `app/agents/` is the LLM orchestration layer. Agents do not access the DB directly — they go through services.
- `app/integrations/` wraps external APIs (Anthropic, Meta, Telegram, 1C). One folder per provider.
- `app/db/models/` is one file per entity, all inheriting from base classes that enforce tenant_id.

## What NOT to do
- No Django, no Flask, no SQLModel.
- No global state outside of tenant_context ContextVar.
- No raw SQL in business logic (use SQLAlchemy ORM); raw SQL is OK in migrations and RLS policies.
- No "utility" or "helpers" grab-bag modules. Put utilities in domain-specific files.
- No new top-level folders without justification.