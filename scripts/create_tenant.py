"""CLI script to provision a new tenant."""

import asyncio
import sys


async def create_tenant(name: str, slug: str, plan: str = "starter") -> None:
    raise NotImplementedError


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python scripts/create_tenant.py <name> <slug> [plan]")
        sys.exit(1)

    _plan = sys.argv[3] if len(sys.argv) > 3 else "starter"
    asyncio.run(create_tenant(sys.argv[1], sys.argv[2], _plan))
