"""Persistent global identity and versioned session resolution."""

from tinyclaw.identity.resolver import (
    IdentityResolver,
    IdentitySessionResolution,
    IdentitySessionResolver,
    SessionPolicy,
    SessionResolver,
)
from tinyclaw.identity.store import (
    IdentityAuditEvent,
    IdentityLinkConflictError,
    IdentityNotFoundError,
    IdentityStoreError,
    SQLiteIdentityStore,
)

__all__ = [
    "IdentityAuditEvent",
    "IdentityLinkConflictError",
    "IdentityNotFoundError",
    "IdentityResolver",
    "IdentitySessionResolution",
    "IdentitySessionResolver",
    "IdentityStoreError",
    "SQLiteIdentityStore",
    "SessionPolicy",
    "SessionResolver",
]
