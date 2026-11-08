"""Closed action data, base schemas and trusted confirmation primitives."""

from __future__ import annotations

from dataclasses import dataclass
import json
import select
import sys
import time
from typing import Any

from .workspace import WorkspaceActionError, WorkspaceActions

MAX_COMMAND = 2048
MAX_OUTPUT_BYTES = 16 * 1024
_ROOT = "demo/workspace"
_FIELDS = {"list_files": {"directory"}, "create_folder": {"name"},
           "create_text_file": {"filename", "directory", "content"}}
_BROKER_CODES = frozenset({
    "audit_unavailable", "destination_exists", "expired", "invalid_path", "path_not_found",
    "not_directory", "invalid_content", "invalid_note_name", "workspace_changed",
    "outcome_unknown", "create_failed", "note_failed", "list_failed", "policy_denied",
    "too_many_entries", "invalid_state", "invalid_ticket", "internal_error",
})


def _tool(name: str, description: str, properties: dict[str, str]) -> dict[str, Any]:
    return {"name": name, "description": description, "parameters": {"type": "object",
            "properties": {key: {"type": "string", "description": value} for key, value in properties.items()},
            "required": list(properties), "additionalProperties": False}}


TOOLS = [
    _tool("list_files", "List the files in an existing directory.",
          {"directory": "Exact folder requested by the user. Use '.' only for the current directory."}),
    _tool("create_folder", "Create a new folder in the current directory.",
          {"name": "Exact new folder name requested by the user."}),
    _tool("create_text_file", "Create a new text file with literal text in an existing directory.",
          {"filename": "Exact filename including .txt, without any folder path.",
           "directory": "Exact parent folder requested by the user, or '.' for the current directory.",
           "content": "Exact text to write, as requested by the user."}),
]


class ActionError(ValueError):
    def __init__(self, code: str = "invalid_model_output") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, str]


@dataclass(frozen=True)
class Execution:
    status: str
    details: dict[str, Any] | None = None
    code: str | None = None
    effect_completed: bool = False


def broker_arguments(call: ToolCall) -> dict[str, str]:
    if (not isinstance(call, ToolCall) or call.name not in _FIELDS
            or not isinstance(call.arguments, dict) or set(call.arguments) != _FIELDS[call.name]
            or any(type(v) is not str for v in call.arguments.values())):
        raise ActionError("invalid_tool_arguments")
    try:
        if call.name == "create_folder":
            name = call.arguments["name"]
            if "/" in name or "\\" in name:
                raise ActionError("invalid_tool_arguments")
            full = _ROOT + "/" + name
            WorkspaceActions.validate_path(full, allow_base=False)
        else:
            directory = call.arguments["directory"]
            if len(directory) > 480 or directory == _ROOT or directory.startswith(_ROOT + "/"):
                raise ActionError("invalid_tool_arguments")
            parent = _ROOT if directory == "." else _ROOT + "/" + directory
            WorkspaceActions.validate_path(parent)
            full = parent
        if call.name == "create_text_file":
            filename = call.arguments["filename"]
            if "/" in filename or "\\" in filename:
                raise ActionError("invalid_tool_arguments")
            full = parent + "/" + filename
            WorkspaceActions.validate_note_path(full)
            WorkspaceActions.validate_note_content(call.arguments["content"])
    except WorkspaceActionError:
        raise ActionError("invalid_tool_arguments") from None
    return {"path": full, **({"content": call.arguments["content"]} if call.name == "create_text_file" else {})}


def terminal_confirm(action: dict[str, Any], expires_at: int) -> bool:
    """A bounded trusted default; the web session supplies its own exact callback."""
    print("Review exact effect: " + json.dumps(action, ensure_ascii=True), flush=True)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("No interactive confirmation; declined.", flush=True)
        return False
    remaining = min(60.0, expires_at - time.time())
    if remaining <= 0:
        return False
    print("Type exactly approve to perform this one action (anything else declines): ", end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], remaining)
    return bool(ready) and sys.stdin.readline().rstrip("\r\n") == "approve" and time.time() < expires_at
