# External API Integration Rules

## General principles
- Every external API has its own folder under app/integrations/.
- Each folder contains: client.py (low-level HTTP), models.py (Pydantic schemas for their payloads), webhook_handler.py if applicable.
- Never call external APIs from agents or services directly — go through the integration layer.
- Every external call has: timeout (default 30s), retry with exponential backoff (max 3), circuit breaker (after 10 consecutive failures, open for 5 min).

## Anthropic
- All calls go through TenantAwareAnthropicClient (app/integrations/anthropic_client.py).
- Cost is logged to llm_calls table before the response returns.
- Use the official anthropic SDK, not raw HTTP.
- Streaming supported but cost tracking happens at stream end.

## Meta (Instagram + Facebook)
- Use the Graph API directly via httpx, not Facebook's Python SDK (it lags behind).
- All requests include the access token of the connected page (stored encrypted per tenant).
- Webhook signature verification using app secret — required, no exceptions.
- Handle rate limits gracefully: respect X-Business-Use-Case-Usage header.
- Long-lived page access tokens stored encrypted; refresh logic when they expire.

## Telegram
- One bot per tenant (each tenant provides their own bot token via @BotFather).
- Bot token stored encrypted.
- Webhook URL pattern: /webhooks/telegram/{tenant_slug} so we can identify tenant from URL.
- Verify Telegram-Bot-Api-Secret-Token header.

## 1C
- 1C is on-premise at most clients. Communication via HTTP services (REST or OData).
- Network: client opens outbound connection to our platform, or platform calls VPN'd endpoint.
- For first tenant, stub the 1C client — return mock data. Real integration is per-client custom work.
- Always treat 1C data as canonical for orders, products, prices. Our DB caches it but 1C wins on conflict.

## Local UZ services (Click, Payme, Uzcard)
- Stub for v1. Just generate fake payment URLs.
- Real integration is post-MVP.

## Webhooks (incoming)
- Verify signature first, before any other processing.
- Return 200 OK fast (within 1s).
- Do real work in background tasks.
- Idempotency: every webhook event has a unique ID; deduplicate before processing.

## Secrets
- All API keys, tokens, secrets in environment variables OR encrypted in DB.
- Never in code, never in logs, never in audit_log payloads.
- Rotate quarterly minimum.