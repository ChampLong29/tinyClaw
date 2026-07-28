"""Content-addressed artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tinyclaw.observability.redaction import Redactor


@dataclass(frozen=True, kw_only=True)
class ArtifactRef:
    artifact_ref: str
    sha256: str
    size_bytes: int
    mime_type: str


class ArtifactStore:
    def __init__(
        self,
        root: Path | str,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()

    def put_json(
        self,
        value: Mapping[str, Any] | list[Any],
        *,
        redact: bool = True,
    ) -> ArtifactRef:
        prepared = self.redactor.redact(value) if redact else value
        data = json.dumps(
            prepared,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self.put_bytes(data, mime_type="application/json")

    def put_text(
        self,
        text: str,
        *,
        mime_type: str = "text/plain; charset=utf-8",
        redact: bool = True,
    ) -> ArtifactRef:
        prepared = self.redactor.redact(text) if redact else text
        return self.put_bytes(str(prepared).encode("utf-8"), mime_type=mime_type)

    def put_bytes(
        self,
        data: bytes,
        *,
        mime_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / digest[:2] / digest
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return ArtifactRef(
            artifact_ref=f"artifact://sha256/{digest}",
            sha256=digest,
            size_bytes=len(data),
            mime_type=mime_type,
        )

    def get_bytes(self, artifact_ref: str) -> bytes:
        digest = self._digest_from_ref(artifact_ref)
        return (self.root / digest[:2] / digest).read_bytes()

    def get_json(self, artifact_ref: str) -> Any:
        return json.loads(self.get_bytes(artifact_ref).decode("utf-8"))

    @staticmethod
    def _digest_from_ref(artifact_ref: str) -> str:
        prefix = "artifact://sha256/"
        if not artifact_ref.startswith(prefix):
            raise ValueError(f"unsupported artifact ref: {artifact_ref!r}")
        digest = artifact_ref[len(prefix) :]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"invalid artifact digest: {digest!r}")
        return digest
