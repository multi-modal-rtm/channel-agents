from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

_tenant_id: ContextVar[UUID | None] = ContextVar("tenant_id", default=None)


def get_current_tenant_id() -> UUID:
    """Return the current tenant UUID. Raises RuntimeError if not set (Rule 10: fail closed)."""
    tid = _tenant_id.get()
    if tid is None:
        raise RuntimeError("No tenant_id in context — request must pass through auth middleware first")
    return tid


def set_current_tenant_id(tenant_id: UUID) -> None:
    _tenant_id.set(tenant_id)


def clear_current_tenant_id() -> None:
    _tenant_id.set(None)


@contextmanager
def tenant_context(tenant_id: UUID):
    """Temporarily set the tenant ContextVar; restores previous value on exit.

    Safe to use inside async tasks and background workers: the ContextVar token
    pattern guarantees the previous value (None or a prior tenant_id) is restored
    even when called from within an already-authenticated request context.
    """
    token = _tenant_id.set(tenant_id)
    try:
        yield
    finally:
        _tenant_id.reset(token)
