"""Fail-closed audited broker for MacIntent's confined workspace actions."""

from __future__ import annotations

import copy
import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from .audit import ActionAudit, AuditError
from .workspace import DELETE_TOOL, TOOLS, PreparedAction, WorkspaceActionError, WorkspaceActions


_TTL = 60
_REASONS = {"multi_call", "unknown_tool", "invalid_turn", "budget", "cancelled", "runtime"}
_END_STATUSES = {"completed", "cancelled", "failed", "denied"}
_STATIC_MESSAGES = {
    "invalid_request": "request is invalid",
    "unknown_operation": "operation is not supported",
    "busy": "another request is active",
    "unknown_request": "request is not active",
    "invalid_state": "request state does not allow this operation",
    "duplicate_prepare": "request already has an action",
    "invalid_ticket": "ticket is invalid",
    "expired": "ticket has expired",
    "audit_unavailable": "audit is unavailable",
    "internal_error": "broker stopped after an internal failure",
    "workspace_changed": "workspace changed",
    "policy_denied": "action denied by policy",
    "invalid_path": "path is not allowed",
    "path_not_found": "path was not found",
    "not_directory": "path is not a directory",
    "bootstrap_failed": "workspace bootstrap failed",
    "unknown_tool": "tool is not allowed",
    "invalid_arguments": "arguments are invalid",
    "invalid_note_name": "note filename must end in .txt",
    "invalid_content": "note content is not allowed",
    "destination_exists": "destination already exists",
    "same_path": "source and destination must differ",
    "source_not_found": "source was not found",
    "invalid_source": "source is not an eligible regular file",
    "source_too_large": "source is too large",
    "source_changed": "source changed",
    "destination_failed": "destination could not be created",
    "copy_failed": "copy failed",
    "note_failed": "note could not be created",
    "delete_failed": "file could not be deleted",
    "outcome_unknown": "action outcome is unknown; inspect the workspace before continuing",
    "create_failed": "directory could not be created",
    "list_failed": "directory could not be listed",
    "too_many_entries": "directory has too many entries",
    "invalid_action": "action is invalid",
    "audit_invalid": "audit data is invalid",
    "invalid_script_name": "script filename must end in .py",
    "invalid_script_code": "script source is invalid or exceeds its bounds",
    "invalid_script_import": "only standard-library imports are supported",
    "script_source_changed": "script file identity or source changed",
    "script_execution_unavailable": "compute-only OS sandbox is unavailable; no unrestricted fallback",
    "script_timeout": "script deadline exceeded",
    "script_output_limit": "script output limit exceeded",
    "script_memory_limit": "script monitored memory limit exceeded",
    "script_cancelled": "script execution cancelled",
    "script_failed": "script execution failed",
}


@dataclass
class _Request:
    request_id: str
    prepared: PreparedAction | None = None
    ticket: str | None = None
    ticket_expires: float = 0.0
    ticket_deadline: float = 0.0
    approved: bool = False
    declined: bool = False
    attempted: bool = False
    ended: str | None = None
    succeeded: bool = False
    execution_state: str = "none"
    request_deadline: float = 0.0
    expired: bool = False
    workflow_id: str | None = None
    step_index: int | None = None


class Broker:
    """Fail-closed state machine with one active request and one action budget."""

    def __init__(self, root: str, *, file_operations: bool = False,
                 script_operations: bool = False, confirm_writes: bool = False, stop_event=None) -> None:
        self.file_operations = file_operations is True
        self.confirm_writes = confirm_writes is True
        self.script_operations = script_operations is True
        self._lock = threading.RLock()
        self.actions = WorkspaceActions(root, allow_delete=self.file_operations)
        self.scripts = None
        if self.script_operations:
            from .scripts import ScriptActions
            self.scripts = ScriptActions(self.actions, stop_event=stop_event)
        try:
            self.audit_log = ActionAudit(self.actions.root_path)
        except Exception:
            self.actions.close()
            raise
        self.audit = self.audit_log
        self.active: _Request | None = None
        self.stopped = False

    def close(self) -> None:
        with self._lock:
            if not self.stopped and self.active is not None and self.active.ended is None:
                try:
                    outcome = self.active.execution_state
                    if outcome == "started":
                        # Closing an interrupted dispatch cannot establish that
                        # no effect happened, even when no result was received.
                        outcome = self.active.execution_state = "unknown"
                    status = "completed" if outcome == "completed" else "failed" if outcome in {"failed", "unknown"} else "cancelled"
                    self._audit(request_id=self.active.request_id, event="ended", execution=outcome, result={"status": status, "action_outcome": outcome})
                except WorkspaceActionError:
                    pass
            self.stopped = True
            try:
                self.audit_log.close()
            finally:
                try:
                    if self.scripts is not None:
                        self.scripts.close()
                finally:
                    self.actions.close()

    def _requires_confirmation(self, action: PreparedAction) -> bool:
        return action.requires_confirmation or self.confirm_writes and action.kind != "list"

    @staticmethod
    def _reply(message_id: str, result: Any = None, error: str | None = None) -> dict[str, Any]:
        if error is None:
            return {"id": message_id, "ok": True, "result": result}
        return {"id": message_id, "ok": False, "error": {"code": error, "message": _STATIC_MESSAGES.get(error, "request failed")}}

    @staticmethod
    def _message_id(message: Any) -> str:
        if isinstance(message, dict) and isinstance(message.get("id"), str) and len(message["id"]) <= 128:
            return message["id"]
        return ""

    @staticmethod
    def _exact(message: Any, fields: set[str]) -> bool:
        return isinstance(message, dict) and set(message) == fields and isinstance(message.get("id"), str) and bool(message["id"]) and len(message["id"]) <= 128 and isinstance(message.get("op"), str)

    @staticmethod
    def _request_id(value: Any) -> bool:
        return isinstance(value, str) and 1 <= len(value) <= 128

    def _fail(self, code: str, *, terminal: bool = False) -> dict[str, Any]:
        if terminal:
            self.stopped = True
        return self._reply(self._current_message_id, error=code)

    def _audit(self, **kwargs: Any) -> None:
        if self.active is not None and self.active.workflow_id is not None and kwargs.get("request_id") == self.active.request_id:
            kwargs["result"] = {**(kwargs.get("result") or {}), "workflow_id": self.active.workflow_id, "step_index": self.active.step_index}
        try:
            self.audit_log.record(**kwargs)
        except AuditError:
            self.stopped = True
            raise WorkspaceActionError("audit_unavailable")

    def _require_active(self, request_id: str) -> _Request:
        if self.active is None or self.active.request_id != request_id or self.active.ended is not None:
            raise WorkspaceActionError("unknown_request")
        return self.active

    @staticmethod
    def _plan_metadata(request: _Request, status: str) -> dict[str, Any]:
        result: dict[str, Any] = {"status": status}
        if request.prepared is not None:
            # Only the action's safe projection is persisted: a note keeps its
            # destination and never its body, in any event or column.
            result["arguments"] = copy.deepcopy(request.prepared.audited_arguments())
            if request.prepared.source is not None:
                result["source_identity"] = asdict(request.prepared.source)
        return result

    def _dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        op = message.get("op")
        if op == "hello":
            if not self._exact(message, {"id", "op"}):
                raise WorkspaceActionError("invalid_request")
            tools = dict(TOOLS)
            if self.file_operations:
                tools["delete_file"] = DELETE_TOOL
            if self.script_operations:
                from .scripts import SCRIPT_TOOLS
                tools.update(SCRIPT_TOOLS)
            return self._reply(message["id"], {
                "tools": [{"name": name, "description": data["description"], "parameters": copy.deepcopy(data["parameters"])} for name, data in tools.items()],
                "workspace": "demo/workspace",
                "max_actions": 1,
                "approval_ttl_seconds": _TTL,
            })
        if op == "begin":
            linked = self._exact(message, {"id", "op", "workflow_id", "step_index"})
            if not linked and not self._exact(message, {"id", "op"}):
                raise WorkspaceActionError("invalid_request")
            if linked and (not isinstance(message["workflow_id"], str) or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", message["workflow_id"]) or type(message["step_index"]) is not int or not 1 <= message["step_index"] <= 4):
                raise WorkspaceActionError("invalid_request")
            if self.stopped:
                raise WorkspaceActionError("internal_error")
            if self.active is not None and self.active.ended is None:
                pending_expired = self.active.ticket is not None and self.active.execution_state == "none" and time.monotonic() >= self.active.ticket_deadline
                if self.active.expired or pending_expired or time.monotonic() >= self.active.request_deadline:
                    self.active.ticket = None
                    self.active.ticket_deadline = 0.0
                    self.active.ended = "cancelled"
                    self._audit(request_id=self.active.request_id, event="ended", execution=self.active.execution_state, result={"status": "cancelled", "action_outcome": self.active.execution_state})
                else:
                    raise WorkspaceActionError("busy")
            request = _Request(str(uuid.uuid4()), request_deadline=time.monotonic() + 180, workflow_id=message.get("workflow_id"), step_index=message.get("step_index"))
            self.active = request
            self._audit(request_id=request.request_id, event="began", result={"status": "begun"})
            return self._reply(message["id"], {"request_id": request.request_id})
        if op == "audit":
            if not self._exact(message, {"id", "op"}):
                raise WorkspaceActionError("invalid_request")
            if self.stopped:
                raise WorkspaceActionError("internal_error")
            try:
                available = self.audit_log.events(200)
                selected = []
                total = 0
                for event in reversed(available):
                    size = len(json.dumps(event, ensure_ascii=True).encode("utf-8"))
                    if total + size > 56 * 1024:
                        break
                    selected.append(event)
                    total += size
                return self._reply(message["id"], {"events": list(reversed(selected)), "truncated": len(selected) < len(available)})
            except AuditError as exc:
                self.stopped = True
                raise WorkspaceActionError("audit_unavailable") from exc
        if self.stopped:
            raise WorkspaceActionError("internal_error")
        if op in {"prepare", "decide", "execute"} and self.active is not None and self.active.ended is None and message.get("request_id") == self.active.request_id and time.monotonic() >= self.active.request_deadline:
            self.active.ticket = None
            self.active.ticket_deadline = 0.0
            self.active.expired = True
            raise WorkspaceActionError("expired")
        if op == "prepare":
            if not self._exact(message, {"id", "op", "request_id", "tool", "arguments"}):
                raise WorkspaceActionError("invalid_request")
            if not self._request_id(message["request_id"]) or not isinstance(message["tool"], str) or not isinstance(message["arguments"], dict):
                raise WorkspaceActionError("invalid_request")
            request = self._require_active(message["request_id"])
            if request.prepared is not None or request.attempted:
                raise WorkspaceActionError("duplicate_prepare")
            # A denied or malformed proposal consumes this request's single
            # action budget as well; callers cannot probe policy and retry.
            request.attempted = True
            # Validate and copy all model-provided values before retaining them.
            if len(message["arguments"]) > 2 or any(not isinstance(k, str) for k in message["arguments"]):
                raise WorkspaceActionError("invalid_arguments")
            try:
                if message["tool"] in {"create_python_script", "run_python_script"}:
                    if self.scripts is None:
                        raise WorkspaceActionError("unknown_tool")
                    prepared = self.scripts.prepare(message["tool"], copy.deepcopy(message["arguments"]))
                else:
                    prepared = self.actions.prepare(message["tool"], copy.deepcopy(message["arguments"]))
            except WorkspaceActionError:
                raise
            request.prepared = prepared
            request.ticket = secrets.token_urlsafe(32)
            request.ticket_expires = time.time() + _TTL
            request.ticket_deadline = time.monotonic() + _TTL
            confirmed = self._requires_confirmation(prepared)
            self._audit(request_id=request.request_id, event="planned", policy="confirm" if confirmed else "allow", tool=prepared.tool, approval="required" if confirmed else "none", result=self._plan_metadata(request, "planned"))
            status = "confirmation_required" if confirmed else "ready"
            return self._reply(message["id"], {"status": status, "ticket": request.ticket, "action": {"tool": prepared.tool, "arguments": copy.deepcopy(prepared.arguments)}, "summary": prepared.summary, "expires_at": int(request.ticket_expires)})
        if op == "decide":
            if not self._exact(message, {"id", "op", "request_id", "ticket", "approve"}):
                raise WorkspaceActionError("invalid_request")
            if not self._request_id(message["request_id"]) or not isinstance(message["ticket"], str) or not isinstance(message["approve"], bool):
                raise WorkspaceActionError("invalid_request")
            request = self._require_active(message["request_id"])
            self._check_ticket(request, message["ticket"], require_confirmation=True)
            if request.approved or request.declined:
                raise WorkspaceActionError("invalid_state")
            if not message["approve"]:
                request.declined = True
                request.ticket = None
                request.ticket_deadline = 0.0
                self._audit(request_id=request.request_id, event="declined", policy="deny", tool=request.prepared.tool if request.prepared else None, approval="declined", result=self._plan_metadata(request, "declined"))
                return self._reply(message["id"], {"status": "declined"})
            request.approved = True
            self._audit(request_id=request.request_id, event="approved", policy="confirm", tool=request.prepared.tool if request.prepared else None, approval="approved", result=self._plan_metadata(request, "approved"))
            return self._reply(message["id"], {"status": "approved"})
        if op == "execute":
            if not self._exact(message, {"id", "op", "request_id", "ticket"}):
                raise WorkspaceActionError("invalid_request")
            if not self._request_id(message["request_id"]) or not isinstance(message["ticket"], str):
                raise WorkspaceActionError("invalid_request")
            request = self._require_active(message["request_id"])
            self._check_ticket(request, message["ticket"])
            request.attempted = True
            # Consume before checking approval/revalidation as well: an
            # execute call is a budgeted attempt even when it is denied.
            request.ticket = None
            request.ticket_deadline = 0.0
            if request.prepared is None or (self._requires_confirmation(request.prepared) and not request.approved):
                raise WorkspaceActionError("invalid_state")
            # The ticket is never written to the audit database.
            self._audit(request_id=request.request_id, event="execution_started", policy="confirm" if request.approved else "allow", tool=request.prepared.tool, approval="approved" if request.approved else "none", execution="started", result=self._plan_metadata(request, "started"))
            request.execution_state = "started"
            try:
                if request.prepared.tool in {"create_python_script", "run_python_script"} and self.scripts is not None:
                    details = self.scripts.execute(request.prepared)
                else:
                    details = self.actions.execute(request.prepared)
            except BaseException as exc:
                # Trusted stop signals and unexpected failures can interrupt
                # native or filesystem effects after the durable intent.
                code = exc.code if isinstance(exc, WorkspaceActionError) else "outcome_unknown"
                request.execution_state = "unknown" if code == "outcome_unknown" else "failed"
                try:
                    self._audit(request_id=request.request_id, event="execution_failed", tool=request.prepared.tool, approval="approved" if request.approved else "none", execution=request.execution_state, error_code=code, result={"status": "failed", "action_outcome": request.execution_state})
                except WorkspaceActionError:
                    pass
                if code == "outcome_unknown":
                    self.stopped = True
                raise
            request.succeeded = True
            request.execution_state = "completed"
            self._audit(request_id=request.request_id, event="execution_completed", tool=request.prepared.tool, approval="approved" if request.approved else "none", execution="completed", result={"status": "executed", "tool": request.prepared.tool, "details": details})
            return self._reply(message["id"], {"status": "executed", "tool": request.prepared.tool, "details": details})
        if op == "reject":
            if not self._exact(message, {"id", "op", "request_id", "reason"}):
                raise WorkspaceActionError("invalid_request")
            if not self._request_id(message["request_id"]) or message["reason"] not in _REASONS:
                raise WorkspaceActionError("invalid_request")
            request = self._require_active(message["request_id"])
            if request.attempted:
                raise WorkspaceActionError("invalid_state")
            request.attempted = True
            request.ticket = None
            self._audit(request_id=request.request_id, event="rejected", policy="deny", tool=request.prepared.tool if request.prepared else None, approval="denied", execution="none", error_code=message["reason"], result={"status": "denied"})
            return self._reply(message["id"], {"status": "denied"})
        if op == "end":
            if not self._exact(message, {"id", "op", "request_id", "status"}):
                raise WorkspaceActionError("invalid_request")
            if not self._request_id(message["request_id"]) or message["status"] not in _END_STATUSES:
                raise WorkspaceActionError("invalid_request")
            request = self._require_active(message["request_id"])
            if message["status"] == "completed" and not request.succeeded:
                raise WorkspaceActionError("invalid_state")
            request.ticket = None
            request.ticket_deadline = 0.0
            request.ended = message["status"]
            self._audit(request_id=request.request_id, event="ended", tool=request.prepared.tool if request.prepared else None, execution=request.execution_state, result={"status": message["status"], "action_outcome": request.execution_state})
            return self._reply(message["id"], {"status": message["status"]})
        raise WorkspaceActionError("unknown_operation")

    def _check_ticket(self, request: _Request, supplied: str, *, require_confirmation: bool = False) -> None:
        """Spend the request's private ticket, and require approval where due.

        ``require_confirmation`` is used by the app-owned decision step: only an
        action the policy marks as confirmed can carry a decision at all.
        """

        if request.ticket is None or not secrets.compare_digest(request.ticket, supplied):
            raise WorkspaceActionError("invalid_ticket")
        if time.monotonic() >= request.ticket_deadline or time.time() >= request.ticket_expires:
            request.ticket = None
            request.ticket_deadline = 0.0
            request.expired = True
            raise WorkspaceActionError("expired")
        if require_confirmation and (request.prepared is None or not self._requires_confirmation(request.prepared)):
            raise WorkspaceActionError("invalid_state")

    def handle(self, message: Any) -> dict[str, Any]:
        with self._lock:
            self._current_message_id = self._message_id(message)
            previous_execution = self.active.execution_state if self.active is not None else None
            try:
                if not isinstance(message, dict):
                    raise WorkspaceActionError("invalid_request")
                return self._dispatch(message)
            except WorkspaceActionError as exc:
                request = self.active
                if request is not None and request.ended is None and not self.stopped:
                    if isinstance(message, dict) and message.get("request_id") == request.request_id and message.get("op") in {"prepare", "decide", "execute", "reject"}:
                        request.ticket = None
                        request.ticket_deadline = 0.0
                        request.attempted = True
                    execution_failure_already_logged = isinstance(message, dict) and message.get("op") == "execute" and previous_execution == "none" and request.execution_state in {"failed", "unknown"}
                    if not execution_failure_already_logged:
                        try:
                            self._audit(request_id=request.request_id, event="rejected", policy="deny", tool=request.prepared.tool if request.prepared else None, error_code=exc.code, result={"status": "denied"})
                        except WorkspaceActionError:
                            return self._reply(self._current_message_id, error="audit_unavailable")
                return self._reply(self._current_message_id, error=exc.code)
            except (AuditError, OSError, ValueError, TypeError, RuntimeError):
                self.stopped = True
                if self.active is not None and self.active.execution_state == "unknown":
                    return self._reply(self._current_message_id, error="outcome_unknown")
                return self._reply(self._current_message_id, error="internal_error")
