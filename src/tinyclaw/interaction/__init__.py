"""Persistent interaction orchestration primitives."""

from tinyclaw.interaction.clarification import ClarificationService
from tinyclaw.interaction.confirmation import ConfirmationService, ConfirmationTokenSigner
from tinyclaw.interaction.control import ControlCommand, ControlCommandHandler, ControlPrincipal
from tinyclaw.interaction.orchestrator import InteractionOrchestrator
from tinyclaw.interaction.production import (
    ParsedClarificationCommand,
    ParsedConfirmationCommand,
    ParsedTaskCommand,
    ProductionInteractionService,
    TaskExecutionOutcome,
    parse_clarification_command,
    parse_confirmation_command,
    parse_task_command,
    session_lane_name,
)
from tinyclaw.interaction.progress import ProgressCoalescer, ProgressEvent, ProgressEventType
from tinyclaw.interaction.request_store import SQLiteRequestStore
from tinyclaw.interaction.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTaskTransitionError,
    TaskStateMachine,
)
from tinyclaw.interaction.task_store import (
    ActiveTaskExistsError,
    SQLiteTaskStore,
    TaskNotFoundError,
    TaskRevisionConflictError,
    TaskStoreError,
)
from tinyclaw.interaction.tool_gate import (
    ToolExecutionContext,
    ToolExecutionGate,
    ToolRiskPolicy,
)
from tinyclaw.pause_signals import (
    ClarificationRequiredSignal,
    ConfirmationRequiredSignal,
    InteractionPauseSignal,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "ActiveTaskExistsError",
    "ClarificationService",
    "ClarificationRequiredSignal",
    "ConfirmationService",
    "ConfirmationRequiredSignal",
    "ConfirmationTokenSigner",
    "ControlCommand",
    "ControlCommandHandler",
    "ControlPrincipal",
    "InteractionOrchestrator",
    "InteractionPauseSignal",
    "InvalidTaskTransitionError",
    "ProgressCoalescer",
    "ProgressEvent",
    "ProgressEventType",
    "ParsedTaskCommand",
    "ParsedClarificationCommand",
    "ParsedConfirmationCommand",
    "ProductionInteractionService",
    "SQLiteRequestStore",
    "SQLiteTaskStore",
    "TaskNotFoundError",
    "TaskRevisionConflictError",
    "TaskExecutionOutcome",
    "TaskStateMachine",
    "TaskStoreError",
    "ToolExecutionContext",
    "ToolExecutionGate",
    "ToolRiskPolicy",
    "parse_clarification_command",
    "parse_confirmation_command",
    "parse_task_command",
    "session_lane_name",
]
