"""Auth profile rotation and failover classification."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FailoverReason(Enum):
    """Classification of API failures to determine retry strategy."""
    rate_limit = "rate_limit"
    auth = "auth"
    timeout = "timeout"
    billing = "billing"
    overflow = "overflow"
    unknown = "unknown"


def classify_failure(exc: Exception) -> FailoverReason:
    """Classify an exception to determine the retry strategy."""
    msg = str(exc).lower()
    if "rate" in msg or "429" in msg:
        return FailoverReason.rate_limit
    if "auth" in msg or "401" in msg or "key" in msg:
        return FailoverReason.auth
    if "timeout" in msg or "timed out" in msg:
        return FailoverReason.timeout
    if "billing" in msg or "quota" in msg or "402" in msg:
        return FailoverReason.billing
    if "context" in msg or "token" in msg or "overflow" in msg:
        return FailoverReason.overflow
    return FailoverReason.unknown


@dataclass
class AuthProfile:
    """An API key configuration with cooldown tracking."""
    name: str
    provider: str
    api_key: str
    cooldown_until: float = 0.0
    failure_reason: str | None = None
    last_good_at: float = 0.0


class ProfileManager:
    """Manages a pool of AuthProfile instances with cooldown-aware selection."""

    def __init__(self, profiles: list[AuthProfile]) -> None:
        self.profiles = profiles

    def select_profile(self) -> AuthProfile | None:
        """Return the first non-cooldown profile, or None if all are cooling down."""
        now = time.time()
        for profile in self.profiles:
            if now >= profile.cooldown_until:
                return profile
        return None

    def select_all_available(self) -> list[AuthProfile]:
        """Return all non-cooldown profiles in order."""
        now = time.time()
        return [p for p in self.profiles if now >= p.cooldown_until]

    def mark_failure(
        self,
        profile: AuthProfile,
        reason: FailoverReason,
        cooldown_seconds: float = 300.0,
    ) -> None:
        """Put a profile into cooldown after a failure."""
        profile.cooldown_until = time.time() + cooldown_seconds
        profile.failure_reason = reason.value

    def mark_success(self, profile: AuthProfile) -> None:
        """Clear failure state and record last success."""
        profile.failure_reason = None
        profile.last_good_at = time.time()

    def list_profiles(self) -> list[dict[str, Any]]:
        """Return status of all profiles."""
        now = time.time()
        result = []
        for p in self.profiles:
            remaining = max(0.0, p.cooldown_until - now)
            status = "available" if remaining == 0 else f"cooldown ({remaining:.0f}s)"
            result.append({
                "name": p.name,
                "provider": p.provider,
                "status": status,
                "failure_reason": p.failure_reason,
                "last_good": (
                    time.strftime("%H:%M:%S", time.localtime(p.last_good_at))
                    if p.last_good_at > 0 else "never"
                ),
            })
        return result
