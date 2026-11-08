"""Workspace tool definitions and final argument/text validation."""

from copy import deepcopy
from typing import Any

from .action_types import MAX_COMMAND, TOOLS as BASE_TOOLS, ActionError, ToolCall, _tool, broker_arguments
from .workspace import WorkspaceActionError, WorkspaceActions

TOOLS = [
    *deepcopy(BASE_TOOLS),
    _tool("copy_file", "Copy one file to a new file in the workspace; the destination must not exist. User approval is required.",
          {"source": "Exact source file path relative to the workspace, including filename.",
           "destination": "Exact new destination file path relative to the workspace, including filename."}),
    _tool("delete_file", "Permanently delete one existing workspace file, not a folder. User approval is required; no undo.",
          {"path": "Exact file path relative to the workspace, including filename. No wildcards."}),
]
_FIELDS = {tool["name"]: set(tool["parameters"]["required"]) for tool in TOOLS}
_ROOT = "demo/workspace"


def validate_transcript(transcript: str) -> str:
    """Validate transport bounds without a grammar or deterministic rewriting."""
    if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > MAX_COMMAND:
        raise ActionError("invalid_command")
    try:
        transcript.encode("utf-8")
    except UnicodeError:
        raise ActionError("invalid_command") from None
    return transcript


def _file_path(value: Any) -> str:
    if not isinstance(value, str) or value == _ROOT or value.startswith(_ROOT + "/"):
        raise ActionError("invalid_tool_arguments")
    full = _ROOT + "/" + value
    try:
        WorkspaceActions.validate_path(full, allow_base=False)
    except WorkspaceActionError:
        raise ActionError("invalid_tool_arguments") from None
    return full


def file_arguments(call: ToolCall) -> dict[str, str]:
    """Validate proposed effects only, never interpret or repair the request."""
    if (not isinstance(call, ToolCall) or type(call.name) is not str or call.name not in _FIELDS
            or not isinstance(call.arguments, dict) or set(call.arguments) != _FIELDS[call.name]
            or any(type(value) is not str for value in call.arguments.values())):
        raise ActionError("invalid_tool_arguments")
    if call.name not in {"copy_file", "delete_file"}:
        return broker_arguments(call)
    arguments = {key: _file_path(value) for key, value in call.arguments.items()}
    if call.name == "copy_file" and arguments["source"].casefold() == arguments["destination"].casefold():
        raise ActionError("same_path")
    return arguments
