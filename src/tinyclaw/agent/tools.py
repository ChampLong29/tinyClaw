"""Tool system for tinyClaw agents.

Provides a schema + handler pattern for LLM-called tools.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Safety utilities
# ---------------------------------------------------------------------------

MAX_TOOL_OUTPUT = 50000


def safe_path(raw: str, workdir: Path | None = None) -> Path:
    """Resolve path safely, preventing traversal outside workdir."""
    if workdir is None:
        workdir = Path.cwd()
    target = (workdir / raw).resolve()
    if not str(target).startswith(str(workdir.resolve())):
        raise ValueError(f"Path traversal blocked: {raw}")
    return target


def truncate(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    """Truncate overly long output with a hint."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} total chars]"


# ---------------------------------------------------------------------------
# Built-in tool implementations
# ---------------------------------------------------------------------------

def tool_bash(command: str, timeout: int = 30, workdir: Path | None = None) -> str:
    """Execute a shell command and return its output."""
    dangerous = ["rm -rf /", "mkfs", "> /dev/sd", "dd if="]
    for pattern in dangerous:
        if pattern in command:
            return f"Error: Refused to run dangerous command containing '{pattern}'"
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=str(workdir or Path.cwd()),
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return truncate(output) if output else "[no output]"
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
    except Exception as exc:
        return f"Error: {exc}"


def tool_read_file(file_path: str, workdir: Path | None = None) -> str:
    """Read the contents of a file."""
    try:
        target = safe_path(file_path, workdir)
        if not target.exists():
            return f"Error: File not found: {file_path}"
        if not target.is_file():
            return f"Error: Not a file: {file_path}"
        content = target.read_text(encoding="utf-8")
        return truncate(content)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


def tool_write_file(file_path: str, content: str, workdir: Path | None = None) -> str:
    """Write content to a file. Creates parent directories if needed."""
    try:
        target = safe_path(file_path, workdir)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Successfully wrote {len(content)} chars to {file_path}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


def tool_edit_file(file_path: str, old_string: str, new_string: str, workdir: Path | None = None) -> str:
    """Replace exact text in a file. old_string must appear exactly once."""
    try:
        target = safe_path(file_path, workdir)
        if not target.exists():
            return f"Error: File not found: {file_path}"
        content = target.read_text(encoding="utf-8")
        count = content.count(old_string)
        if count == 0:
            return "Error: old_string not found in file. Make sure it matches exactly."
        if count > 1:
            return (
                f"Error: old_string found {count} times. "
                "It must be unique. Provide more surrounding context."
            )
        new_content = content.replace(old_string, new_string, 1)
        target.write_text(new_content, encoding="utf-8")
        return f"Successfully edited {file_path}"
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"Error: {exc}"


def tool_get_current_time() -> str:
    """Return current Beijing time."""
    from tinyclaw.utils.timezone import now_beijing

    now = now_beijing()
    return now.strftime("%Y-%m-%d %H:%M:%S CST")


# ---------------------------------------------------------------------------
# Built-in tool schemas
# ---------------------------------------------------------------------------

BUILTIN_TOOLS: list[dict[str, Any]] = [
    {
        "name": "bash",
        "description": (
            "Run a shell command and return its output. "
            "Use for system commands, git, package managers, etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute."},
                "timeout": {"type": "integer", "description": "Timeout in seconds. Default 30."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file (relative to working directory)."},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file. Creates parent directories if needed. "
            "Overwrites existing content."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file."},
                "content": {"type": "string", "description": "The content to write."},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact string in a file with a new string. "
            "The old_string must appear exactly once. Always read the file first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file."},
                "old_string": {"type": "string", "description": "The exact text to find and replace."},
                "new_string": {"type": "string", "description": "The replacement text."},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "get_current_time",
        "description": "Get the current date and time in Beijing timezone.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ---------------------------------------------------------------------------
# Tool Dispatcher
# ---------------------------------------------------------------------------

ToolHandler = Callable[..., str]


class ToolDispatcher:
    """Manages tool schemas and their handlers.

    The dispatcher maintains:
      - tools: list of tool schemas passed to the LLM API
      - handlers: dict mapping tool name -> handler function

    Usage:
        dispatcher = ToolDispatcher()
        dispatcher.register(tool_schema, handler_fn)
        result = dispatcher.dispatch(tool_name, tool_input)
    """

    def __init__(self) -> None:
        self._schemas: list[dict[str, Any]] = []
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, schema: dict[str, Any], handler: ToolHandler) -> None:
        """Register a tool with its schema and handler."""
        name = schema.get("name")
        if not name:
            raise ValueError("Tool schema must have a 'name' field")
        self._schemas.append(schema)
        self._handlers[name] = handler

    def register_builtin(self, workdir: Path | None = None) -> None:
        """Register all built-in tools with their default handlers."""
        import functools

        for schema in BUILTIN_TOOLS:
            name = schema["name"]
            if name == "bash":
                handler = functools.partial(tool_bash, workdir=workdir)
            elif name == "read_file":
                handler = functools.partial(tool_read_file, workdir=workdir)
            elif name == "write_file":
                handler = functools.partial(tool_write_file, workdir=workdir)
            elif name == "edit_file":
                handler = functools.partial(tool_edit_file, workdir=workdir)
            elif name == "get_current_time":
                handler = tool_get_current_time
            else:
                continue
            self.register(schema, handler)

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        """Dispatch a tool call to its handler. Returns string result."""
        handler = self._handlers.get(tool_name)
        if handler is None:
            return f"Error: Unknown tool '{tool_name}'"
        try:
            return handler(**tool_input)
        except TypeError as exc:
            return f"Error: Invalid arguments for {tool_name}: {exc}"
        except Exception as exc:
            return f"Error: {tool_name} failed: {exc}"

    @property
    def tools(self) -> list[dict[str, Any]]:
        """Return the list of tool schemas for the LLM API."""
        return list(self._schemas)

    def list_tools(self) -> list[str]:
        """Return the list of registered tool names."""
        return list(self._handlers.keys())
