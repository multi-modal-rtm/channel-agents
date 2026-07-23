"""Prometheus metrics registry.

All counters and histograms live here so they're importable everywhere without
circular dependencies. The /metrics endpoint in main.py serves them.
"""

from prometheus_client import (
    REGISTRY,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

__all__ = [
    "REGISTRY",
    "generate_latest",
    "CONTENT_TYPE_LATEST",
    "llm_calls_total",
    "llm_cost_usd_total",
    "http_requests_total",
    "http_request_duration_seconds",
    "tenant_paused_total",
    "safety_job_actions_total",
]

# ── LLM metrics ───────────────────────────────────────────────────────────────

llm_calls_total = Counter(
    "llm_calls_total",
    "Total LLM API calls",
    ["model", "success"],
)

llm_cost_usd_total = Counter(
    "llm_cost_usd_total",
    "Total LLM cost in USD",
    ["model"],
)

# ── HTTP metrics ──────────────────────────────────────────────────────────────

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled",
    ["method", "endpoint", "status_code"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

# ── Operational metrics ───────────────────────────────────────────────────────

tenant_paused_total = Counter(
    "tenant_paused_total",
    "Number of times a tenant has been auto-paused by the safety job",
    ["reason"],
)

safety_job_actions_total = Counter(
    "safety_job_actions_total",
    "Actions taken by the background safety job",
    ["action"],
)
