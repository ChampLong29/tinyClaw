"""High-risk action confirmation with signed, scope-bound tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from tinyclaw.contracts._common import parse_datetime, require_text, utc_now
from tinyclaw.contracts.interaction import TaskState
from tinyclaw.interaction.clarification import (
    RequestAlreadyResolvedError,
    RequestAuthorizationError,
    RequestExpiredError,
)
from tinyclaw.interaction.request_store import (
    ConfirmationDecision,
    ConfirmationRequest,
    RequestState,
    RiskLevel,
    SQLiteRequestStore,
)
from tinyclaw.interaction.state_machine import TaskStateMachine


class InvalidConfirmationTokenError(PermissionError):
    pass


class ActionDigestMismatchError(PermissionError):
    pass


def compute_action_digest(action: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        action,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_or_create_confirmation_secret(
    workspace: Path,
    configured_secret: str | None = None,
) -> str:
    """Load a stable signer secret, creating a workspace-local one when absent."""
    if configured_secret:
        if len(configured_secret.encode("utf-8")) < 16:
            raise ValueError("CONFIRMATION_TOKEN_SECRET must be at least 16 bytes")
        return configured_secret
    secret_path = workspace / ".confirmation-token-secret"
    try:
        stored = secret_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        stored = ""
    if stored:
        if len(stored.encode("utf-8")) < 16:
            raise ValueError(f"invalid confirmation secret in {secret_path}")
        return stored

    candidate = secrets.token_urlsafe(32)
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with secret_path.open("x", encoding="utf-8") as handle:
            handle.write(candidate)
    except FileExistsError:
        candidate = secret_path.read_text(encoding="utf-8").strip()
    return candidate


class ConfirmationTokenSigner:
    def __init__(self, secret: bytes | str) -> None:
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if len(secret) < 16:
            raise ValueError("confirmation token secret must be at least 16 bytes")
        self._secret = secret

    def issue(self, request: ConfirmationRequest) -> str:
        claims = self._claims(request)
        signature = hmac.new(self._secret, claims, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")

    def verify(self, request: ConfirmationRequest, token: str) -> bool:
        require_text(token, "action_token")
        expected = self.issue(request)
        return hmac.compare_digest(expected, token)

    @staticmethod
    def _claims(request: ConfirmationRequest) -> bytes:
        return "\x1f".join(
            (
                request.request_id,
                request.task_id,
                request.session_id,
                request.global_user_id,
                request.action_digest,
                request.expires_at.isoformat(),
            )
        ).encode("utf-8")


class ConfirmationService:
    def __init__(
        self,
        request_store: SQLiteRequestStore,
        state_machine: TaskStateMachine,
        signer: ConfirmationTokenSigner,
    ) -> None:
        self.request_store = request_store
        self.state_machine = state_machine
        self.signer = signer

    def open(
        self,
        *,
        task_id: str,
        global_user_id: str,
        action_summary: str,
        action: Mapping[str, Any],
        risk_level: RiskLevel,
        scope: Mapping[str, Any],
        actor: str,
        expires_at: datetime | str | None = None,
    ) -> tuple[ConfirmationRequest, str]:
        task = self.state_machine.store.get_task(task_id)
        request = ConfirmationRequest(
            task_id=task.task_id,
            session_id=task.session_id,
            global_user_id=global_user_id,
            action=dict(action),
            action_summary=action_summary,
            action_digest=compute_action_digest({"action": action, "scope": dict(scope)}),
            risk_level=risk_level,
            scope=dict(scope),
            expires_at=parse_datetime(expires_at) or (utc_now() + timedelta(minutes=10)),
        )
        self.request_store.create(request)
        try:
            self.state_machine.transition(
                task.task_id,
                TaskState.WAITING_CONFIRMATION,
                actor=actor,
                reason="confirmation_opened",
                expected_revision=task.revision,
                pending_request_ref=request.request_id,
            )
        except Exception:
            self.request_store.resolve(
                request,
                state=RequestState.CANCELLED,
                expected_revision=request.revision,
            )
            raise
        return request, self.signer.issue(request)

    def decide(
        self,
        request_id: str,
        *,
        session_id: str,
        global_user_id: str,
        action_token: str,
        action: Mapping[str, Any] | None = None,
        decision: ConfirmationDecision,
        actor: str,
        modification: Mapping[str, Any] | None = None,
    ) -> ConfirmationRequest:
        request = self.request_store.get(request_id)
        if not isinstance(request, ConfirmationRequest):
            raise TypeError("request is not a confirmation")
        self._authorize(request, session_id, global_user_id)
        self._ensure_open(request)
        if request.expires_at <= utc_now():
            self._expire(request, actor=actor)
            raise RequestExpiredError(request.request_id)
        resolved_action = dict(action) if action is not None else dict(request.action)
        if not resolved_action:
            raise ValueError("confirmation request does not contain a persisted action")
        if (
            compute_action_digest({"action": resolved_action, "scope": dict(request.scope)})
            != request.action_digest
        ):
            raise ActionDigestMismatchError("confirmed action parameters changed")
        if not self.signer.verify(request, action_token):
            raise InvalidConfirmationTokenError("invalid confirmation token")
        if decision == ConfirmationDecision.MODIFY and not modification:
            raise ValueError("modify decision requires modification")

        state_by_decision = {
            ConfirmationDecision.APPROVE_ONCE: RequestState.APPROVED,
            ConfirmationDecision.DENY: RequestState.DENIED,
            ConfirmationDecision.MODIFY: RequestState.MODIFIED,
        }
        resolved = self.request_store.resolve(
            request,
            state=state_by_decision[decision],
            expected_revision=request.revision,
            decision=decision,
            modification=dict(modification) if modification else None,
        )
        task = self.state_machine.store.get_task(request.task_id)
        target_state = (
            TaskState.CANCELLED if decision == ConfirmationDecision.DENY else TaskState.RUNNING
        )
        self.state_machine.transition(
            task.task_id,
            target_state,
            actor=actor,
            reason=f"confirmation_{decision.value}",
            expected_revision=task.revision,
            pending_request_ref=None,
        )
        return resolved

    def consume_approval(
        self,
        request_id: str,
        *,
        session_id: str,
        global_user_id: str,
        action: Mapping[str, Any],
    ) -> ConfirmationRequest:
        """Atomically consume an exact approve-once grant before execution."""
        request = self.request_store.get(request_id)
        if not isinstance(request, ConfirmationRequest):
            raise TypeError("request is not a confirmation")
        self._authorize(request, session_id, global_user_id)
        if request.state != RequestState.APPROVED:
            raise RequestAlreadyResolvedError(request.request_id)
        if request.expires_at <= utc_now():
            self.request_store.resolve(
                request,
                state=RequestState.EXPIRED,
                expected_revision=request.revision,
            )
            raise RequestExpiredError(request.request_id)
        if (
            compute_action_digest({"action": action, "scope": dict(request.scope)})
            != request.action_digest
        ):
            raise ActionDigestMismatchError("approved action parameters changed")
        return self.request_store.resolve(
            request,
            state=RequestState.CONSUMED,
            expected_revision=request.revision,
        )

    def expire(self, request_id: str, *, actor: str = "system") -> ConfirmationRequest:
        request = self.request_store.get(request_id)
        if not isinstance(request, ConfirmationRequest):
            raise TypeError("request is not a confirmation")
        return self._expire(request, actor=actor)

    def _expire(self, request: ConfirmationRequest, *, actor: str) -> ConfirmationRequest:
        self._ensure_open(request)
        expired = self.request_store.resolve(
            request,
            state=RequestState.EXPIRED,
            expected_revision=request.revision,
        )
        task = self.state_machine.store.get_task(request.task_id)
        if task.state == TaskState.WAITING_CONFIRMATION:
            self.state_machine.transition(
                task.task_id,
                TaskState.EXPIRED,
                actor=actor,
                reason="confirmation_expired",
                expected_revision=task.revision,
                pending_request_ref=None,
            )
        return expired

    @staticmethod
    def _authorize(
        request: ConfirmationRequest,
        session_id: str,
        global_user_id: str,
    ) -> None:
        if request.session_id != session_id or request.global_user_id != global_user_id:
            raise RequestAuthorizationError("confirmation identity/session mismatch")

    @staticmethod
    def _ensure_open(request: ConfirmationRequest) -> None:
        if request.state != RequestState.OPEN:
            raise RequestAlreadyResolvedError(request.request_id)
