# Channel Agents

Multi-tenant AI agent platform for textile manufacturers. Handles customer inquiries over Telegram (and soon WhatsApp/Instagram), performs lead qualification, and answers product/FAQ questions — all with per-tenant cost controls, kill switches, and full audit trails.

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2 |
| Database | PostgreSQL — **Neon** (serverless) |
| Vector store | **Qdrant Cloud** |
| Cache / Queue | Redis + **arq** — **Upstash** (serverless Redis) |
| LLM | Anthropic API — claude-opus-4-7, claude-sonnet-4-6, claude-haiku-4-5 |
| Observability | Langfuse + structlog + Prometheus |

---

## Table of Contents

- [Local Development](#local-development)
- [Production Deploy](#production-deploy)
- [Onboarding a New Tenant](#onboarding-a-new-tenant)
- [Operational Runbooks](#operational-runbooks)
- [Architecture](#architecture)

---

## Local Development

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) (`pip install uv`)
- Docker Desktop (for local Postgres / Redis / Qdrant)

### 1. Clone and install

```bash
git clone https://github.com/yourorg/channel-agents
cd channel-agents
uv venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
uv pip install -e ".[dev]"
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set SECRET_KEY, JWT_SECRET_KEY, FERNET_KEY, ANTHROPIC_API_KEY_MANAGED
```

Generate required random values:

```bash
python -c "import secrets; print(secrets.token_hex(32))"                               # SECRET_KEY
python -c "import secrets; print(secrets.token_hex(32))"                               # JWT_SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # FERNET_KEY
```

### 3. Start infrastructure

```bash
docker compose -f docker-compose.dev.yml up -d db redis qdrant
```

### 4. Run migrations

```bash
alembic upgrade head
```

### 5. Seed dev data (optional)

```bash
python scripts/seed_dev.py
# Creates "demo" tenant with 20 products, 15 FAQs, 4 agents
```

### 6. Start the app

```bash
# Terminal 1 — API server
uvicorn app.main:app --reload

# Terminal 2 — background worker + cron
python -m arq app.workers.arq_app.WorkerSettings
```

API at http://localhost:8000. Swagger docs at http://localhost:8000/docs.

### Running tests

```bash
# Unit tests (no DB required)
pytest tests/unit/ -x -q

# Integration tests (requires local Postgres from step 3)
export PG_TEST_ADMIN_DSN="postgresql+asyncpg://channel:channel_dev@localhost:5432/channel_agents"
pytest tests/integration/ -x -q

# All tests
pytest -x -q
```

---

## Production Deploy

All three platforms work the same way: point at the Docker image, set env vars, and let managed services handle persistence.

### Option A — Railway

1. New project → Deploy from GitHub repo
2. Add an `app` service and a `worker` service (separate services, same image)
3. Set environment variables (see table below)
4. Add a one-off deploy command: `alembic upgrade head`

### Option B — Render

1. New Web Service → Connect GitHub → Docker runtime
2. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
3. Add a Background Worker service: `python -m arq app.workers.arq_app.WorkerSettings`
4. Add a Job for migrations: `alembic upgrade head`

### Option C — Fly.io

```bash
fly launch --no-deploy
# Edit fly.toml: set [build] dockerfile = "Dockerfile"
fly secrets set DATABASE_URL=... REDIS_URL=... # (see vars below)
fly deploy
```

### Option D — Self-hosted (docker-compose)

```bash
git clone https://github.com/yourorg/channel-agents && cd channel-agents
cp .env.example .env && nano .env   # fill all values

docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

The `migrate` service runs `alembic upgrade head` before the app starts.

### Required environment variables

| Variable | Description |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` (Neon recommended) |
| `REDIS_URL` | `rediss://...` (Upstash recommended) |
| `QDRANT_URL` | Qdrant Cloud cluster URL |
| `QDRANT_API_KEY` | Qdrant Cloud API key |
| `SECRET_KEY` | 32-byte random hex |
| `JWT_SECRET_KEY` | 32-byte random hex |
| `FERNET_KEY` | Fernet key for BYOK encryption |
| `ANTHROPIC_API_KEY_MANAGED` | Anthropic key for managed-billing tenants |
| `LANGFUSE_PUBLIC_KEY` | Langfuse observability (optional) |
| `LANGFUSE_SECRET_KEY` | Langfuse observability (optional) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `VOYAGE_API_KEY` | Voyage AI key for embeddings |

### Post-deploy smoke test

```bash
SMOKE_EMAIL=owner@yourtenantslug.com \
SMOKE_PASSWORD=yourpassword \
python scripts/smoke_test.py https://your-app.example.com
```

---

## Onboarding a New Tenant

### 1. Register

```bash
curl -X POST https://your-app.example.com/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "owner@newtenant.com", "password": "secure-password",
       "tenant_name": "New Textile Co", "slug": "new-textile-co"}'
```

### 2. Log in

```bash
TOKEN=$(curl -s -X POST https://your-app.example.com/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "owner@newtenant.com", "password": "secure-password"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

### 3. Set a daily budget

```bash
curl -X PATCH https://your-app.example.com/api/v1/tenants/me \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"daily_budget_usd": 5.00}'
```

### 4. Configure BYOK (optional)

```bash
curl -X POST https://your-app.example.com/api/v1/tenants/me/byok \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"api_key": "sk-ant-..."}'
```

### 5. Set up Telegram

```bash
curl -X POST https://your-app.example.com/api/v1/integrations/telegram/webhook \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"bot_token": "12345:ABC..."}'
```

---

## Operational Runbooks

### Pause a tenant (kill switch)

Use immediately if a tenant misbehaves or a security issue is discovered.

```bash
curl -X POST https://your-app.example.com/api/v1/tenants/me/pause \
  -H "Authorization: Bearer $TOKEN"
```

Sets `status=paused` and `autonomous_actions_enabled=false`. All background tasks exit early. New LLM calls are blocked.

```bash
# Resume
curl -X POST https://your-app.example.com/api/v1/tenants/me/resume \
  -H "Authorization: Bearer $TOKEN"
```

### Check today's costs

```bash
curl https://your-app.example.com/api/v1/tenants/me/usage/today \
  -H "Authorization: Bearer $TOKEN"
# → { "cost_usd": 1.23, "call_count": 45, "error_count": 2 }
```

Date range with model breakdown:

```bash
curl "https://your-app.example.com/api/v1/tenants/me/usage?from=2026-05-01&to=2026-05-07" \
  -H "Authorization: Bearer $TOKEN"
```

### Safety job auto-pause triggers

The safety job runs every 5 minutes:

- **Budget ≥ 95%** of `daily_budget_usd` → auto-pause tenant + alert
- **Error rate > 20%** in last 15 min (min 5 calls) → auto-pause tenant + alert
- **Agent ≥ 10 consecutive failures** → disable that agent only

All events are written to `audit_log`.

### Debug a failed agent

```bash
# List agents — check enabled status
curl https://your-app.example.com/api/v1/agents/ -H "Authorization: Bearer $TOKEN"

# Re-enable a disabled agent
curl -X PATCH https://your-app.example.com/api/v1/agents/{agent_id} \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"enabled": true}'

# Test an agent
curl -X POST https://your-app.example.com/api/v1/agents/{agent_id}/test \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"message": "What fabrics do you carry?"}'
```

### Prometheus metrics

```bash
curl http://localhost:8000/metrics | grep -E "llm_calls|cost|tenant_paused|safety"
```

| Metric | Description |
|---|---|
| `llm_calls_total` | Labeled by model and success |
| `llm_cost_usd_total` | Running cost by model |
| `tenant_paused_total` | Auto-pause events by reason |
| `safety_job_actions_total` | Safety job actions |
| `http_request_duration_seconds` | API latency histogram |

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    nginx (prod)                       │
│            rate-limit · TLS termination               │
└─────────────────────┬────────────────────────────────┘
                      │
┌─────────────────────▼────────────────────────────────┐
│               FastAPI (app)                           │
│  /api/v1/auth   /api/v1/tenants   /api/v1/agents     │
│  /api/v1/conversations    /webhooks/telegram          │
│  /health   /metrics                                   │
└──────────┬───────────────────────────────────────────┘
           │ arq.enqueue_job
┌──────────▼───────────────────────────────────────────┐
│                 arq worker                            │
│  handle_incoming_message · safety_job (cron 5 min)   │
│                                                       │
│  OrchestratorAgent                                    │
│    └─ ConversationAgent (agentic loop)                │
│    └─ LeadQualifierAgent                              │
│    └─ ContentAgent                                    │
└──────┬───────────────────────────────────────────────┘
       │
       ├── Anthropic API (claude-sonnet-4-6 default)
       ├── Qdrant (vector search)
       ├── PostgreSQL + RLS (per-tenant row isolation)
       └── Redis (arq job queue)
```

**Multi-tenancy:** Every DB table has `tenant_id`. PostgreSQL RLS with `FORCE ROW LEVEL SECURITY` ensures queries only see the current tenant's rows. A `ContextVar` stores the active tenant ID and sets `app.tenant_id` GUC on each session.

**BYOK:** Tenant API keys are encrypted with Fernet before storage. The decrypted key lives only in memory for the duration of the request.

**Cost control:** `TenantAwareAnthropicClient` checks the daily budget before every LLM call. At 80% spend it logs a soft warning; at 100% it raises `BudgetExceededError`. The safety job independently auto-pauses at 95%.

## Project layout

```
app/
├── agents/          AI agent implementations (conversation, content, lead qualifier)
├── api/v1/          REST endpoints
├── core/            Security, logging, tenant context var, metrics
├── db/              SQLAlchemy models + RLS-aware session factory
├── integrations/    Telegram, Anthropic client
├── schemas/         Pydantic request/response models
├── services/        Business logic layer
├── workers/         arq async task workers + cron
└── utils/           Fernet encryption, Uzbek phone normalisation
```
