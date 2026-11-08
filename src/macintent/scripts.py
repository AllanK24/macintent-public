"""Explicitly gated .py creation and captured-source, compute-only execution."""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import os
import stat
import sys
from typing import Any

from .sandbox import SandboxError, run_script
from .workspace import (
    PreparedAction, WorkspaceActionError, WorkspaceActions, _READ_FLAGS,
    _file_identity, _identity, _same_file,
)


MAX_SCRIPT_CHARS = 8192
MAX_SCRIPT_BYTES = 16384
SCRIPT_TOOLS = {
    "create_python_script": {"description": "Create one bounded new .py file after exact code/path approval.", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}, "code": {"type": "string"}},
        "required": ["path", "code"], "additionalProperties": False}},
    "run_python_script": {"description": "Separately approve exact code/identity for compute-only OS-sandboxed execution.", "parameters": {
        "type": "object", "properties": {"path": {"type": "string"}},
        "required": ["path"], "additionalProperties": False}},
}


def validate_script_path(value: Any) -> tuple[str, ...]:
    parts = WorkspaceActions.validate_path(value, allow_base=False)
    if not parts[-1].endswith(".py"):
        raise WorkspaceActionError("invalid_script_name")
    return parts


def validate_script_code(value: Any) -> bytes:
    if type(value) is not str or not value.strip() or len(value) > MAX_SCRIPT_CHARS:
        raise WorkspaceActionError("invalid_script_code")
    if any((ord(c) < 32 and c not in "\n\t") or 127 <= ord(c) < 160 for c in value):
        raise WorkspaceActionError("invalid_script_code")
    try:
        data = value.encode("utf-8")
        if len(data) > MAX_SCRIPT_BYTES:
            raise WorkspaceActionError("invalid_script_code")
        tree = ast.parse(value, filename="<approved-script>")
        compile(tree, "<approved-script>", "exec")  # Compile only: never evaluate source during creation.
    except (SyntaxError, UnicodeError, ValueError, RecursionError):
        raise WorkspaceActionError("invalid_script_code") from None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                raise WorkspaceActionError("invalid_script_import")
            names = [node.module]
        else:
            continue
        if any(name.split(".")[0] not in sys.stdlib_module_names for name in names):
            raise WorkspaceActionError("invalid_script_import")
    return data


@dataclass(frozen=True)
class PreparedScript(PreparedAction):
    @property
    def requires_confirmation(self) -> bool:
        return True

    def audited_arguments(self) -> dict[str, str]:
        return {"path": self.arguments["path"]}


class ScriptActions:
    def __init__(self, workspace: WorkspaceActions, *, stop_event: Any = None) -> None:
        self.workspace = workspace
        self.stop_event = stop_event
        self._output: dict[str, str] | None = None

    def close(self) -> None:
        self._output = None

    def take_output(self) -> dict[str, str]:
        result, self._output = self._output, None
        return result if result is not None else {"stdout": "", "stderr": ""}

    def _read(self, parts: tuple[str, ...], *, expected: PreparedScript | None = None):
        workspace = self.workspace
        parent_fd, name = (workspace._revalidate_parent(parts, expected.parent) if expected else workspace._open_parent(parts))
        source_fd = -1
        try:
            parent = _identity(os.fstat(parent_fd))
            source_fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
            info = os.fstat(source_fd)
            identity = _file_identity(info)
            path_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= MAX_SCRIPT_BYTES
                    or not _same_file(identity, path_info)
                    or expected is not None and (identity != expected.source or parent != expected.parent)):
                raise WorkspaceActionError("script_source_changed")
            chunks, total = [], 0
            while total <= MAX_SCRIPT_BYTES:
                block = os.read(source_fd, min(4096, MAX_SCRIPT_BYTES + 1 - total))
                if not block:
                    break
                chunks.append(block)
                total += len(block)
            data = b"".join(chunks)
            if (len(data) != info.st_size or not _same_file(identity, os.fstat(source_fd))
                    or not _same_file(identity, os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
                    or expected is not None and data != expected.note_bytes):
                raise WorkspaceActionError("script_source_changed")
            try:
                code = data.decode("utf-8")
            except UnicodeError:
                raise WorkspaceActionError("invalid_script_code") from None
            validate_script_code(code)
            return parent, identity, data, code
        except OSError:
            raise WorkspaceActionError("script_source_changed") from None
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            os.close(parent_fd)

    def prepare(self, tool: str, arguments: dict[str, str]) -> PreparedScript:
        self._output = None
        workspace = self.workspace
        workspace._check_base()
        if tool == "create_python_script":
            if set(arguments) != {"path", "code"}:
                raise WorkspaceActionError("invalid_arguments")
            parts = validate_script_path(arguments["path"])
            data = validate_script_code(arguments["code"])
            fd, name = workspace._open_parent(parts)
            try:
                parent = _identity(os.fstat(fd))
                try:
                    os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise WorkspaceActionError("destination_exists")
            finally:
                os.close(fd)
            source, kind = None, "script_create"
            preview = dict(arguments)
            summary = "Create new Python script " + arguments["path"] + f" ({len(data)} bytes); not executed"
        elif tool == "run_python_script":
            if set(arguments) != {"path"}:
                raise WorkspaceActionError("invalid_arguments")
            parts = validate_script_path(arguments["path"])
            parent, source, data, code = self._read(parts)
            kind = "script_run"
            preview = {"path": arguments["path"], "code": code}
            summary = ("Run exact captured Python script " + arguments["path"] + " in compute-only OS sandbox; "
                       "no file I/O/network/subprocesses; 2s CPU, 5s wall, monitored 128MiB RSS, 16KiB output; SHA256 "
                       + hashlib.sha256(data).hexdigest())
        else:
            raise WorkspaceActionError("unknown_tool")
        return PreparedScript(tool, preview, summary, kind, parts, parts[:-1], None, None,
                              workspace._root, workspace._base, None, parent, source,
                              parent if source else None, None, data)

    def execute(self, action: PreparedScript) -> dict[str, Any]:
        workspace = self.workspace
        if type(action) is not PreparedScript or validate_script_path(action.arguments.get("path")) != action.path_parts:
            raise WorkspaceActionError("invalid_action")
        workspace._revalidate_common(action)
        if (set(action.arguments) != {"path", "code"}
                or validate_script_code(action.arguments["code"]) != action.note_bytes):
            raise WorkspaceActionError("invalid_action")
        if action.tool == "create_python_script" and action.kind == "script_create":
            return workspace._create_note(action)
        if action.tool != "run_python_script" or action.kind != "script_run":
            raise WorkspaceActionError("invalid_action")
        self._read(action.path_parts, expected=action)
        try:
            result = run_script(action.note_bytes, root=workspace.root_path, stop_event=self.stop_event)
        except SandboxError as exc:
            raise WorkspaceActionError(exc.code) from None
        except KeyboardInterrupt:
            raise WorkspaceActionError("script_cancelled") from None
        if (type(result.exit_code) is not int or not 0 <= result.exit_code <= 255
                or type(result.stdout) is not str or type(result.stderr) is not str
                or len(result.stdout) + len(result.stderr) > 16 * 1024):
            raise WorkspaceActionError("outcome_unknown")
        self._output = {"stdout": result.stdout, "stderr": result.stderr}
        return {"path": action.arguments["path"], "bytes": len(action.note_bytes), "exit_code": result.exit_code, "executed": True}
