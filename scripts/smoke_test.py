"""
Smoke test — runs against a live deployed instance after every deploy.

Usage:
    python scripts/smoke_test.py https://your-app.example.com
    python scripts/smoke_test.py http://localhost:8000  # local

Exits non-zero if any check fails.
"""

from __future__ import annotations

import os
import sys

import httpx

BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
OWNER_EMAIL = os.environ.get("SMOKE_EMAIL", "owner@alpha.com")
OWNER_PASSWORD = os.environ.get("SMOKE_PASSWORD", "")

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name}" + (f": {detail}" if detail else ""))
        failures.append(name)


def run() -> None:
    print(f"\nSmoke test → {BASE_URL}\n")

    with httpx.Client(base_url=BASE_URL, timeout=15) as client:

        # 1. Health check
        r = client.get("/health")
        check("/health → 200", r.status_code == 200)
        check("/health body", r.json().get("status") == "ok", str(r.json()))

        # 2. Auth login
        if not OWNER_PASSWORD:
            print("  SKIP  /auth/login (set SMOKE_EMAIL + SMOKE_PASSWORD to enable)")
            token = None
        else:
            r = client.post(
                "/api/v1/auth/login",
                json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD},
            )
            check("/auth/login → 200", r.status_code == 200, r.text[:120])
            token = r.json().get("access_token") if r.status_code == 200 else None

        auth_headers = {"Authorization": f"Bearer {token}"} if token else {}

        # 3. Tenant me
        if token:
            r = client.get("/api/v1/tenants/me", headers=auth_headers)
            check("/tenants/me → 200", r.status_code == 200, r.text[:120])

        # 4. Agents list
        if token:
            r = client.get("/api/v1/agents/", headers=auth_headers)
            check("/agents/ → 200", r.status_code == 200, r.text[:120])

        # 5. Usage today
        if token:
            r = client.get("/api/v1/tenants/me/usage/today", headers=auth_headers)
            check("/usage/today → 200", r.status_code == 200, r.text[:120])

        # 6. Metrics endpoint (unauthenticated, returns prometheus text)
        r = client.get("/metrics")
        check("/metrics → 200", r.status_code == 200)
        check("/metrics has llm_calls", "llm_calls_total" in r.text)

        # 7. 404 for unknown routes
        r = client.get("/api/v1/nonexistent")
        check("unknown route → 404", r.status_code == 404)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        sys.exit(1)
    else:
        print("All checks passed.")


if __name__ == "__main__":
    run()
