# DevOps Notes

## Database naming after the project rename

The project was renamed from `textile-agents` to `channel-agents`. The code,
`.env.example`, `docker-compose.dev.yml`, `alembic.ini`, and CI config now all
reference a database named `channel_agents` (and `channel_agents_test` for
tests).

**The existing Neon Postgres database is still named `textile_agents` (formerly
`neondb`) and has not been renamed.** Renaming a live database requires a
backup/restore or `ALTER DATABASE` during a maintenance window, plus updating
the connection string secret wherever it's stored — that's a deliberate,
separate operation, not part of this housekeeping pass.

- **Existing environment (current Neon project):** keep using the existing
  `DATABASE_URL` as-is in `.env` — do not change the database name to match
  the new code defaults. The code does not hardcode a database name; it reads
  `DATABASE_URL` from the environment.
- **New environment (fresh Neon project, new deploy target, etc.):** create a
  database named `channel_agents`. The code, `.env.example`, and local
  `docker-compose.dev.yml` all assume this name going forward.

## Known local-dev blocker: Redis (Upstash) endpoint is unreachable

As of 2026-07-23, the `REDIS_URL` in `.env` points to
`active-minnow-117418.upstash.io`, which no longer resolves via DNS (verified
with `nslookup` — `Non-existent domain`, while the Neon and Qdrant hosts in
the same file resolve fine). This means:

- `uvicorn app.main:app --reload` fails at startup — `app/main.py`'s lifespan
  handler blocks on an arq/Redis ping (`create_pool(...)` in `app/main.py:27`)
  and the app never comes up.
- The Upstash Redis database backing this URL appears to have been deleted or
  deprovisioned.

**Action needed before the app can run:** provision a new Upstash (or other
Redis-compatible) instance and update `REDIS_URL` in `.env`. Not fixed as
part of this housekeeping pass — flagging so it isn't mistaken for a code
regression.
