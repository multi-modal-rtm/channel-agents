# Multi-Tenant Safety — Non-Negotiable Rules

This is a multi-tenant system. A single bug here ends the business. These rules are absolute.

## Rule 1: Every tenant-scoped table has tenant_id NOT NULL
- Column is UUID, second column after id, indexed as the first column of every index.
- Foreign keys to other tenant-scoped tables must be in the same tenant (enforce via composite FK or trigger).

## Rule 2: Row-Level Security is enabled on every tenant-scoped table
- Policy: `USING (tenant_id = current_setting('app.tenant_id')::uuid)`
- App sets `SET LOCAL app.tenant_id = '<uuid>'` at the start of every transaction via session checkout hook.
- Tests must verify RLS blocks cross-tenant reads, writes, updates, and deletes.
- Bypassing RLS requires using a separate `admin_session` and is allowed ONLY in:
  - Tenant creation (no tenant context exists yet)
  - Platform-level admin operations
  - Migrations

## Rule 3: Tenant context is a ContextVar, never a function argument passed around
- Set in middleware after JWT decode.
- All DB sessions read from it. If unset, raise — do not default.
- Cleared after request.

## Rule 4: BYOK API keys are encrypted at rest
- Use Fernet (or AES-256-GCM) with key from secret manager.
- Plaintext key exists only in memory during a single LLM call.
- Never log the key. Never include it in error messages. Never put it in audit_log.
- When a tenant rotates or deletes their key, zero the old ciphertext immediately.

## Rule 5: Cache keys always include tenant_id
- Redis: prefix all keys with `tenant:<uuid>:`
- In-memory caches: key includes tenant_id
- LLM prompt caching: disabled across tenants (never share cache between tenants)
- Vector search results: scoped to tenant's collection only

## Rule 6: Every LLM call is logged with tenant_id and cost
- Write to llm_calls table before returning to caller.
- If logging fails, the call still happened — record the failure, alert.
- Daily budget per tenant enforced as hard cap.

## Rule 7: Audit log records every mutation
- Append-only.
- Includes tenant_id, user_id (or agent_id), action, entity, payload.
- Required for: agent actions, config changes, key changes, manual messages, tenant pause/resume.

## Rule 8: Kill switches at multiple levels
- Per agent: disable single agent
- Per tenant: pause all autonomous actions
- Per platform: feature flag to disable a feature globally
- All kill switches must be reachable via API and via direct DB update (for emergencies).

## Rule 9: Test for isolation, not just functionality
- Every tenant-scoped feature has a corresponding cross-tenant isolation test.
- Tests in tests/multi_tenant_isolation/ are mandatory CI gates.

## Rule 10: When in doubt, fail closed
- Missing tenant context → reject request
- Unknown agent type → reject
- LLM call without budget check → reject
- Webhook with invalid signature → reject
- Better to refuse a legitimate request than to leak data once.