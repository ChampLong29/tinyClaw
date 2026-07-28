"""Recursive redaction for trace, feedback, and replay artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
    r"password|passwd|secret|cookie|private[_-]?key)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


@dataclass(frozen=True, kw_only=True)
class RedactionPolicy:
    replacement: str = "[REDACTED]"
    max_depth: int = 20


class Redactor:
    def __init__(self, policy: RedactionPolicy | None = None) -> None:
        self.policy = policy or RedactionPolicy()

    def redact(self, value: Any) -> Any:
        return self._redact(value, depth=0)

    def _redact(self, value: Any, *, depth: int) -> Any:
        if depth > self.policy.max_depth:
            return "[MAX_DEPTH]"
        if isinstance(value, Mapping):
            return {
                str(key): (
                    self.policy.replacement
                    if _SENSITIVE_KEY_RE.search(str(key))
                    else self._redact(item, depth=depth + 1)
                )
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [self._redact(item, depth=depth + 1) for item in value]
        if isinstance(value, str):
            redacted = _BEARER_RE.sub(
                f"Bearer {self.policy.replacement}",
                value,
            )
            return _OPENAI_KEY_RE.sub(self.policy.replacement, redacted)
        return value
