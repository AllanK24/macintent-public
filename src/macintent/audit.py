"""Private, bounded SQLite audit storage for workspace actions."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

from .workspace import WorkspaceActions, WorkspaceActionError

MAX_DATABASE_BYTES = 64 * 1024 * 1024
MAX_AUDIT_EVENTS = 100_000
# The exact result metadata each tool may return.  A note is a write, so its
# result is its destination, its byte count, and the fact of creation, never
# its body.
_DETAIL_KEYS: dict[str, frozenset[str]] = {
    "list_directory": frozenset({"path", "entries"}),
    "create_directory": frozenset({"path", "created"}),
    "copy_file": frozenset({"source", "destination", "bytes"}),
    "create_note": frozenset({"path", "bytes", "created"}),
    "delete_file": frozenset({"path", "bytes", "deleted"}),
    "create_python_script": frozenset({"path", "bytes", "created"}),
    "run_python_script": frozenset({"path", "bytes", "exit_code", "executed"}),
}
_ANY_DETAIL_KEYS = frozenset().union(*_DETAIL_KEYS.values())
EVENTS_SQL = """CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at REAL NOT NULL,
    request_id TEXT NOT NULL,
    tool TEXT,
    event TEXT NOT NULL,
    policy TEXT NOT NULL,
    approval TEXT NOT NULL,
    execution TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT
)"""


class AuditError(Exception):
    pass


class ActionAudit:
    """Fixed-location append-only events with storage and privacy limits."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(os.path.abspath(root))
        try:
            root_st = os.lstat(self.root)
        except OSError as exc:
            raise AuditError("audit_unavailable") from exc
        if not stat.S_ISDIR(root_st.st_mode):
            raise AuditError("audit_unavailable")
        self._root_identity = (root_st.st_dev, root_st.st_ino)
        self._root_fd = -1
        try:
            self._root_fd = os.open(
                self.root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError as exc:
            raise AuditError("audit_unavailable") from exc
        try:
            self._make_fixed_dirs()
        except Exception:
            os.close(self._root_fd)
            self._root_fd = -1
            raise
        self.path = self.root / ".local" / "state" / "actions.sqlite3"
        self._db_identity: tuple[int, int] | None = None
        self._db_fd = -1
        self._parent_identities: dict[str, tuple[int, int]] = {}
        try:
            self._guard_files(allow_database_missing=True)
        except Exception:
            os.close(self._root_fd)
            self._root_fd = -1
            raise
        self.connection: sqlite3.Connection | None = None
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
            try:
                self._db_fd = os.open(self.path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                self._db_fd = os.open(self.path, flags)
            opened = os.fstat(self._db_fd)
            identity = (opened.st_dev, opened.st_ino)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (self._db_identity is not None and self._db_identity != identity):
                raise AuditError("audit_unavailable")
            self._db_identity = identity
            self._guard_files()
            os.fchmod(self._db_fd, 0o600)
            self.connection = sqlite3.connect(self.path, isolation_level=None, timeout=2)
            self.connection.execute("PRAGMA trusted_schema=OFF")
            self.connection.execute("PRAGMA journal_mode=DELETE")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute(EVENTS_SQL)
            columns = [row[1] for row in self.connection.execute("PRAGMA table_info(events)").fetchall()]
            expected = [
                "id", "occurred_at", "request_id", "tool", "event", "policy",
                "approval", "execution", "result_json", "error_code",
            ]
            if columns != expected or self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise AuditError("audit_unavailable")
            sql = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='events'").fetchone()[0]
            normalize = lambda value: " ".join(value.lower().split()).replace("if not exists ", "")
            if normalize(sql) != normalize(EVENTS_SQL):
                raise AuditError("audit_unavailable")
            if self.connection.execute("SELECT name FROM sqlite_master WHERE type IN ('trigger','view') OR (type='table' AND name NOT IN ('events','sqlite_sequence'))").fetchone():
                raise AuditError("audit_unavailable")
            page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
            if opened.st_size > MAX_DATABASE_BYTES:
                raise AuditError("audit_unavailable")
            self.connection.execute(f"PRAGMA max_page_count={MAX_DATABASE_BYTES // page_size}")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.commit()
            self._refresh_db_identity()
        except (OSError, sqlite3.Error, AuditError) as exc:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            if self._root_fd >= 0:
                os.close(self._root_fd)
                self._root_fd = -1
            if self._db_fd >= 0:
                os.close(self._db_fd)
                self._db_fd = -1
            raise AuditError("audit_unavailable") from exc

    def _make_fixed_dirs(self) -> None:
        fd = os.dup(self._root_fd)
        try:
            for name in (".local", "state"):
                try:
                    child = os.open(
                        name,
                        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=fd,
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                        child = os.open(
                            name,
                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                            dir_fd=fd,
                        )
                    except OSError as exc:
                        raise AuditError("audit_unavailable") from exc
                except OSError as exc:
                    raise AuditError("audit_unavailable") from exc
                try:
                    st = os.fstat(child)
                    if not stat.S_ISDIR(st.st_mode):
                        raise AuditError("audit_unavailable")
                    old_fd = fd
                    fd = child
                    os.close(old_fd)
                except Exception:
                    os.close(child)
                    raise
        finally:
            os.close(fd)

    def _guard_files(self, *, allow_database_missing: bool = False) -> None:
        # Validate each prefix before consulting anything below it.
        for name in ("", ".local", ".local/state"):
            try:
                identity = os.lstat(self.root / name)
            except OSError as exc:
                raise AuditError("audit_unavailable") from exc
            current = (identity.st_dev, identity.st_ino)
            if not stat.S_ISDIR(identity.st_mode) or (name and ((identity.st_mode & 0o077) != 0 or identity.st_uid != os.geteuid())):
                raise AuditError("audit_unavailable")
            expected = self._root_identity if not name else self._parent_identities.get(name)
            if expected is not None and current != expected:
                raise AuditError("audit_unavailable")
            if name:
                self._parent_identities.setdefault(name, current)
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            try:
                st = os.lstat(candidate)
            except FileNotFoundError:
                if suffix == "" and not allow_database_missing:
                    raise AuditError("audit_unavailable")
                continue
            except OSError as exc:
                raise AuditError("audit_unavailable") from exc
            if stat.S_ISLNK(st.st_mode) or st.st_nlink != 1 or not stat.S_ISREG(st.st_mode):
                raise AuditError("audit_unavailable")
            if suffix and ((st.st_mode & 0o077) != 0 or st.st_uid != os.geteuid()):
                raise AuditError("audit_unavailable")
            if suffix == "":
                current = (st.st_dev, st.st_ino)
                if self._db_identity is not None and current != self._db_identity:
                    raise AuditError("audit_unavailable")
                self._db_identity = current

    def _refresh_db_identity(self) -> None:
        try:
            st = os.lstat(self.path)
        except OSError as exc:
            raise AuditError("audit_unavailable") from exc
        if stat.S_ISLNK(st.st_mode) or st.st_nlink != 1 or not stat.S_ISREG(st.st_mode):
            raise AuditError("audit_unavailable")
        current = (st.st_dev, st.st_ino)
        if self._db_identity is not None and self._db_identity != current:
            raise AuditError("audit_unavailable")
        self._db_identity = current
        try:
            os.fchmod(self._db_fd, 0o600)
        except OSError as exc:
            raise AuditError("audit_unavailable") from exc

    def _guard_use(self) -> None:
        if self.connection is None:
            raise AuditError("audit_unavailable")
        self._guard_files()
        try:
            opened = os.fstat(self._db_fd)
        except OSError as exc:
            raise AuditError("audit_unavailable") from exc
        if self._db_identity != (opened.st_dev, opened.st_ino) or opened.st_nlink != 1:
            raise AuditError("audit_unavailable")
        try:
            st = os.lstat(self.path)
        except OSError as exc:
            raise AuditError("audit_unavailable") from exc
        if self._db_identity != (st.st_dev, st.st_ino):
            raise AuditError("audit_unavailable")

    @staticmethod
    def _json_value(value: Any) -> str | None:
        if value is None:
            return None
        try:
            encoded = json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        except (TypeError, ValueError) as exc:
            raise AuditError("audit_invalid") from exc
        if len(encoded) > 64 * 1024:
            raise AuditError("audit_invalid")
        return encoded

    @staticmethod
    def _validate_result(result: Any, *, tool: str | None = None) -> None:
        """Validate one persisted result object against the closed shapes.

        ``tool`` is the event's tool column.  A result that names its own tool
        is judged against that one, so a caller cannot label a note's result as
        a listing to widen what may be written.
        """

        if result is None:
            return
        if not isinstance(result, dict) or set(result) - {"status", "arguments", "source_identity", "tool", "details", "action_outcome", "workflow_id", "step_index"}:
            raise AuditError("audit_invalid")
        if result.get("status") not in {"begun", "planned", "approved", "declined", "started", "executed", "failed", "denied", "completed", "cancelled"}:
            raise AuditError("audit_invalid")
        if "tool" in result and result["tool"] not in _DETAIL_KEYS:
            raise AuditError("audit_invalid")
        if tool is not None and "tool" in result and result["tool"] != tool:
            raise AuditError("audit_invalid")
        if "action_outcome" in result and result["action_outcome"] not in {"none", "started", "completed", "failed", "unknown"}:
            raise AuditError("audit_invalid")
        if "workflow_id" in result or "step_index" in result:
            if not isinstance(result.get("workflow_id"), str) or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", result["workflow_id"]) or type(result.get("step_index")) is not int or not 1 <= result["step_index"] <= 4:
                raise AuditError("audit_invalid")
        effective_tool = result.get("tool", tool)
        try:
            if effective_tool == "delete_file" and result.get("status") in {"planned", "approved", "declined", "started"}:
                if (
                    not isinstance(result.get("arguments"), dict)
                    or set(result["arguments"]) != {"path"}
                    or "source_identity" not in result
                ):
                    raise AuditError("audit_invalid")
            if "arguments" in result:
                arguments = result["arguments"]
                # Paths only, for every tool.  A ``create_note`` body is model
                # authored text and is never persisted, so an argument set that
                # carries one is refused here as well as at the broker.
                if effective_tool in {"delete_file", "create_python_script", "run_python_script"}:
                    valid_arguments = isinstance(arguments, dict) and set(arguments) == {"path"}
                else:
                    valid_arguments = isinstance(arguments, dict) and set(arguments) in ({"path"}, {"source", "destination"})
                if not valid_arguments:
                    raise AuditError("audit_invalid")
                for name, path in arguments.items():
                    if name in {"path", "source", "destination"}:
                        WorkspaceActions.validate_path(path, allow_base=name == "path" and effective_tool == "list_directory")
                if effective_tool in {"create_python_script", "run_python_script"} and not arguments["path"].endswith(".py"):
                    raise AuditError("audit_invalid")
            if "source_identity" in result:
                identity = result["source_identity"]
                if (effective_tool not in {"copy_file", "delete_file", "run_python_script"} or not isinstance(identity, dict)
                        or set(identity) != {"dev", "ino", "size", "mtime_ns", "ctime_ns", "nlink", "mode"}
                        or any(type(value) is not int for value in identity.values())):
                    raise AuditError("audit_invalid")
                if effective_tool in {"delete_file", "run_python_script"} and (
                    not 0 <= identity["dev"] <= 2**63 - 1
                    or not 0 <= identity["ino"] <= 2**63 - 1
                    or not 0 <= identity["size"] <= (16384 if effective_tool == "run_python_script" else 8 * 1024 * 1024)
                    or identity["nlink"] != 1
                    or not 0 <= identity["mode"] <= 0o177777
                    or not stat.S_ISREG(identity["mode"])
                    or not -(2**63) <= identity["mtime_ns"] <= 2**63 - 1
                    or not -(2**63) <= identity["ctime_ns"] <= 2**63 - 1
                ):
                    raise AuditError("audit_invalid")
            if "details" in result:
                details = result["details"]
                # When the event names its tool, the result may carry only that
                # tool's own metadata.  An unlabelled result keeps the union.
                allowed = _DETAIL_KEYS.get(effective_tool or "", _ANY_DETAIL_KEYS)
                if not isinstance(details, dict) or set(details) - allowed:
                    raise AuditError("audit_invalid")
                for name in ("path", "source", "destination"):
                    if name in details:
                        WorkspaceActions.validate_path(details[name], allow_base=name == "path" and effective_tool == "list_directory")
                if effective_tool in {"create_python_script", "run_python_script"}:
                    required = {"path", "bytes", "created"} if effective_tool == "create_python_script" else {"path", "bytes", "exit_code", "executed"}
                    if (set(details) != required or not details["path"].endswith(".py")
                            or type(details["bytes"]) is not int or not 1 <= details["bytes"] <= 16384
                            or effective_tool == "create_python_script" and details["created"] is not True
                            or effective_tool == "run_python_script" and (
                                details["executed"] is not True or type(details["exit_code"]) is not int
                                or not 0 <= details["exit_code"] <= 255)):
                        raise AuditError("audit_invalid")
                if effective_tool == "delete_file" and (
                    set(details) != {"path", "bytes", "deleted"} or details.get("deleted") is not True
                ):
                    raise AuditError("audit_invalid")
                if effective_tool != "delete_file" and "deleted" in details:
                    raise AuditError("audit_invalid")
                if "created" in details and type(details["created"]) is not bool:
                    raise AuditError("audit_invalid")
                if "bytes" in details and (type(details["bytes"]) is not int or not 0 <= details["bytes"] <= 8 * 1024 * 1024):
                    raise AuditError("audit_invalid")
                if "entries" in details:
                    if not isinstance(details["entries"], list) or len(details["entries"]) > 200:
                        raise AuditError("audit_invalid")
                    for entry in details["entries"]:
                        if not isinstance(entry, dict) or set(entry) not in ({"name", "type"}, {"name", "type", "size"}) or entry["type"] not in {"file", "directory"}:
                            raise AuditError("audit_invalid")
                        WorkspaceActions.validate_path("demo/workspace/" + entry["name"], allow_base=False)
                        if "size" in entry and (type(entry["size"]) is not int or entry["size"] < 0):
                            raise AuditError("audit_invalid")
        except (WorkspaceActionError, TypeError) as exc:
            raise AuditError("audit_invalid") from exc

    def record(
        self,
        *,
        request_id: str,
        event: str,
        tool: str | None = None,
        policy: str = "workspace_only",
        approval: str = "none",
        execution: str = "none",
        result: Any = None,
        error_code: str | None = None,
    ) -> None:
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            raise AuditError("audit_invalid")
        if event not in {"began", "planned", "approved", "declined", "execution_started", "execution_failed", "execution_completed", "rejected", "ended"} or tool is not None and tool not in _DETAIL_KEYS or policy not in {"workspace_only", "allow", "confirm", "deny"} or approval not in {"none", "required", "approved", "declined", "denied"} or execution not in {"none", "started", "completed", "failed", "unknown", "cancelled", "denied"}:
            raise AuditError("audit_invalid")
        if error_code is not None and (not isinstance(error_code, str) or not re.fullmatch(r"[a-z_]{1,64}", error_code)):
            raise AuditError("audit_invalid")
        self._validate_result(result, tool=tool)
        # Callers pass only validated names and structured metadata.  Do not
        # offer a raw message/prompt/body column even accidentally.
        self._guard_use()
        result_json = self._json_value(result)
        try:
            if self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] >= MAX_AUDIT_EVENTS:
                raise AuditError("audit_unavailable")
            self.connection.execute(
                "INSERT INTO events(occurred_at,request_id,tool,event,policy,approval,execution,result_json,error_code) VALUES(?,?,?,?,?,?,?,?,?)",
                (time.time(), request_id, tool, event, policy, approval, execution, result_json, error_code),
            )
            self.connection.commit()
        except (sqlite3.Error, OSError) as exc:
            raise AuditError("audit_unavailable") from exc
        self._guard_use()

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        self._guard_use()
        try:
            rows = self.connection.execute(
                "SELECT id,occurred_at,request_id,tool,event,policy,approval,execution,result_json,error_code FROM events ORDER BY id DESC LIMIT ?",
                (min(max(int(limit), 0), 200),),
            ).fetchall()
        except (sqlite3.Error, OSError, ValueError) as exc:
            raise AuditError("audit_unavailable") from exc
        output: list[dict[str, Any]] = []
        for row in reversed(rows):
            result = None
            if row[8] is not None:
                try:
                    result = json.loads(row[8])
                except (TypeError, ValueError) as exc:
                    raise AuditError("audit_unavailable") from exc
            output.append({
                "id": row[0],
                "occurred_at": row[1],
                "request_id": row[2],
                "tool": row[3],
                "event": row[4],
                "policy": row[5],
                "approval": row[6],
                "execution": row[7],
                "result": result,
                "error": row[9],
            })
        return output

    def close(self) -> None:
        connection = getattr(self, "connection", None)
        if connection is not None:
            connection.close()
            self.connection = None
        fd = getattr(self, "_db_fd", -1)
        if fd >= 0:
            os.close(fd)
            self._db_fd = -1
        fd = getattr(self, "_root_fd", -1)
        if fd >= 0:
            os.close(fd)
            self._root_fd = -1


AuditLog = ActionAudit
Audit = ActionAudit
