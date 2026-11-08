"""Trusted serial coordinator. Plans never authorize effects or a retry."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time
from typing import Any, Callable
import uuid

from .audit import ActionAudit, AuditError
from .broker import Broker
from .contract import ActionPlan, action_arguments, proposal_value, validate_proposal
from .action_types import Execution, ActionError, ToolCall, terminal_confirm
from .workspace import WorkspaceActionError, _file_identity


class _Declined(Exception):
    pass


def _confirm_plan(plan: ActionPlan, inputs: Any, output: Any) -> bool:
    if not inputs.isatty() or not output.isatty():
        raise ActionError("interactive_confirmation_required")
    print("Review this entire plan. Continuing does NOT approve any write or script run. "
          "Each effect still needs its own fresh approval; failure/decline stops remaining steps. "
          "Completed effects remain, with no rollback.", file=output, flush=True)
    print("Type exactly continue to review the steps individually (anything else declines): ",
          end="", file=output, flush=True)
    return inputs.readline(34).rstrip("\r\n") == "continue"


def _cancelled(stop: Any) -> None:
    if stop.is_set():
        raise KeyboardInterrupt


def _rpc(broker: Broker, op: str, **fields: Any) -> dict[str, Any]:
    reply = broker.handle({"id": uuid.uuid4().hex, "op": op, **fields})
    if type(reply) is not dict or reply.get("ok") is not True or type(reply.get("result")) is not dict:
        error = reply.get("error") if type(reply) is dict else None
        code = error.get("code") if type(error) is dict else None
        # Broker codes are static snake-case identifiers; never surface text.
        from .broker import _STATIC_MESSAGES
        if code not in _STATIC_MESSAGES and code not in {"script_cancelled"}:
            code = "broker_rejected"
        raise ActionError(code)
    return reply["result"]


def _one_action(broker: Broker, call: ToolCall, *, stop: Any, confirm: Callable,
                workflow_id: str | None, index: int, output: Any) -> Execution:
    tool, arguments = action_arguments(call)
    request_id = None
    terminal, observed = "failed", False
    result = Execution("failed", code="action_failed")
    try:
        _cancelled(stop)
        linked = {"workflow_id": workflow_id, "step_index": index} if workflow_id else {}
        request_id = _rpc(broker, "begin", **linked)["request_id"]
        prepared = _rpc(broker, "prepare", request_id=request_id, tool=tool, arguments=arguments)
        _cancelled(stop)
        action = prepared.get("action")
        expected = {"tool": tool, "arguments": arguments}
        if tool == "run_python_script":
            if (type(action) is not dict or set(action) != {"tool", "arguments"} or action["tool"] != tool
                    or type(action["arguments"]) is not dict or set(action["arguments"]) != {"path", "code"}
                    or action["arguments"]["path"] != arguments["path"]):
                raise ActionError("broker_rejected")
            from .scripts import validate_script_code
            validate_script_code(action["arguments"]["code"])
            expected = action
        needs_approval = tool != "list_directory"
        if (set(prepared) != {"status", "ticket", "action", "summary", "expires_at"}
                or action != expected
                or prepared["status"] != ("confirmation_required" if needs_approval else "ready")
                or type(prepared["expires_at"]) is not int
                or not time.time() < prepared["expires_at"] <= time.time() + 60
                or type(prepared["ticket"]) is not str or not 1 <= len(prepared["ticket"]) <= 256
                or type(prepared["summary"]) is not str or len(prepared["summary"]) > 2048):
            raise ActionError("broker_rejected")
        print("Broker preview: " + json.dumps(prepared["summary"], ensure_ascii=True), file=output, flush=True)
        if needs_approval:
            approved = confirm(json.loads(json.dumps(action)), prepared["expires_at"])
            if type(approved) is not bool:
                raise ActionError("invalid_confirmation")
            _cancelled(stop)
            decision = _rpc(broker, "decide", request_id=request_id, ticket=prepared["ticket"], approve=approved)
            if decision != {"status": "approved" if approved else "declined"}:
                raise ActionError("broker_rejected")
            if not approved:
                raise _Declined
        _cancelled(stop)
        executed = _rpc(broker, "execute", request_id=request_id, ticket=prepared["ticket"])
        if (set(executed) != {"status", "tool", "details"} or executed["status"] != "executed"
                or executed["tool"] != tool or type(executed["details"]) is not dict):
            raise ActionError("broker_rejected")
        details = executed["details"]
        ActionAudit._validate_result(executed, tool=tool)
        for key, value in arguments.items():
            if key != "code" and key != "content" and details.get(key) != value:
                raise ActionError("broker_rejected")
        shapes = {
            "list_directory": {"path", "entries"}, "create_directory": {"path", "created"},
            "create_note": {"path", "bytes", "created"}, "copy_file": {"source", "destination", "bytes"},
            "delete_file": {"path", "bytes", "deleted"},
            "create_python_script": {"path", "bytes", "created"},
            "run_python_script": {"path", "bytes", "exit_code", "executed"},
        }
        if (set(details) != shapes[tool] or "created" in details and details["created"] is not True
                or "deleted" in details and details["deleted"] is not True
                or "executed" in details and details["executed"] is not True):
            raise ActionError("broker_rejected")
        terminal, result = "completed", Execution("completed", details=dict(details), effect_completed=needs_approval)
        if tool == "run_python_script":
            ephemeral = broker.scripts.take_output()
            if (type(ephemeral) is not dict or set(ephemeral) != {"stdout", "stderr"}
                    or any(type(value) is not str for value in ephemeral.values())
                    or sum(map(len, ephemeral.values())) > 16 * 1024):
                raise ActionError("broker_rejected")
            result.details.update(ephemeral)  # After audit validation; memory/display only.
            if details["exit_code"] != 0:
                terminal, result = "failed", Execution("failed", code="script_failed", details=result.details, effect_completed=True)
        observed = True
    except _Declined:
        terminal, result = "denied", Execution("declined")
    except KeyboardInterrupt:
        terminal, result = "cancelled", Execution("cancelled", code="cancelled")
    except ActionError as exc:
        status = "unknown" if exc.code == "outcome_unknown" else "cancelled" if exc.code in {"cancelled", "script_cancelled"} else "failed"
        terminal, result = ("cancelled" if status == "cancelled" else "failed"), Execution(status, code=exc.code)
    except (WorkspaceActionError, AuditError, KeyError, TypeError, ValueError, OSError, RuntimeError):
        terminal, result = "failed", Execution("failed", code="action_failed")
    finally:
        active = getattr(broker, "active", None)
        if active is not None and getattr(active, "execution_state", None) in {"started", "unknown"}:
            terminal, result = "failed", Execution("unknown", code="outcome_unknown")
        elif active is not None and getattr(active, "succeeded", False) and not observed:
            result = Execution("failed", code="audit_or_result_failed", effect_completed=True)
        if request_id is not None:
            try:
                ended = _rpc(broker, "end", request_id=request_id, status=terminal) == {"status": terminal}
            except (KeyboardInterrupt, ActionError, OSError, RuntimeError):
                ended = False
            if not ended:
                result = Execution("unknown" if result.status == "unknown" else "failed", code="audit_unavailable",
                                   effect_completed=result.effect_completed)
    return result


def _snapshot(actions: Any, path: str, *, directory: bool) -> Any:
    """Bind successful earlier creations to identities, not just reused names."""
    parts = actions.validate_path(path, allow_base=False)
    actions._check_base()
    if directory:
        return actions._directory_identity(parts)
    fd, name = actions._open_parent(parts)
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WorkspaceActionError("workspace_changed")
        return _file_identity(info)
    finally:
        os.close(fd)


def execute_proposal(root: Path, proposal: ToolCall | ActionPlan, *, stop_event: Any = None,
                     confirm: Callable = terminal_confirm, confirm_plan: Callable | None = None,
                     stdin: Any = None, stdout: Any = None, broker_factory: Any = Broker,
                     on_step: Callable | None = None, on_result: Callable | None = None) -> Execution:
    """Review once, then prepare/approve/audit/observe each individual effect."""
    inputs, output = stdin or sys.stdin, stdout or sys.stdout
    stop = threading.Event() if stop_event is None else stop_event
    proposal = validate_proposal(proposal)  # Private copy; never execute callback-mutated arguments.
    is_plan = type(proposal) is ActionPlan
    steps = proposal.steps if is_plan else (proposal,)
    broker = None
    completed = 0
    effects = False
    anchors: dict[str, tuple[bool, Any]] = {}
    try:
        _cancelled(stop)
        if is_plan:
            print("Proposed plan (not approved): " + json.dumps(proposal_value(proposal), ensure_ascii=True), file=output, flush=True)
            accepted = confirm_plan(validate_proposal(proposal)) if confirm_plan is not None else _confirm_plan(proposal, inputs, output)
            if type(accepted) is not bool:
                raise ActionError("invalid_confirmation")
            if not accepted:
                return Execution("declined", details={"completed_steps": 0, "total_steps": len(steps)})
        _cancelled(stop)
        broker = broker_factory(str(root), file_operations=True, script_operations=True, confirm_writes=True, stop_event=stop)
        workflow_id = str(uuid.uuid4()) if is_plan else None
        for index, call in enumerate(steps, 1):
            _cancelled(stop)
            for path, (directory, identity) in anchors.items():
                if _snapshot(broker.actions, path, directory=directory) != identity:
                    raise WorkspaceActionError("workspace_changed")
            print(f"Step {index}/{len(steps)}: " + json.dumps(proposal_value(call), ensure_ascii=True), file=output, flush=True)
            if on_step is not None:
                on_step(index, len(steps), validate_proposal(call))
                _cancelled(stop)
            outcome = _one_action(broker, call, stop=stop, confirm=confirm, workflow_id=workflow_id, index=index, output=output)
            effects = effects or outcome.effect_completed
            print("Step result: " + json.dumps({"status": outcome.status, "code": outcome.code,
                  "details": outcome.details, "effect_completed": outcome.effect_completed}, ensure_ascii=True), file=output, flush=True)
            if on_result is not None:
                # Observers receive a private copy only after durable finalization;
                # they cannot edit executable arguments, approvals or the result.
                on_result(index, deepcopy(outcome))
            if outcome.status != "completed":
                return Execution(outcome.status, code=outcome.code, effect_completed=effects,
                                 details={"completed_steps": completed, "total_steps": len(steps), "stopped_at": index})
            completed += 1
            if not is_plan:
                return outcome
            tool, arguments = action_arguments(call)
            if tool in {"create_directory", "create_note", "create_python_script", "copy_file"}:
                path = arguments.get("destination", arguments.get("path"))
                directory = tool == "create_directory"
                anchors[path] = (directory, _snapshot(broker.actions, path, directory=directory))
            elif tool == "delete_file":
                anchors.pop(arguments["path"], None)
        return Execution("completed", details={"completed_steps": completed, "total_steps": len(steps)}, effect_completed=effects)
    except KeyboardInterrupt:
        return Execution("cancelled", code="cancelled", effect_completed=effects,
                         details={"completed_steps": completed, "total_steps": len(steps)})
    except (WorkspaceActionError, ActionError) as exc:
        return Execution("failed", code=exc.code, effect_completed=effects,
                         details={"completed_steps": completed, "total_steps": len(steps)})
    except (OSError, RuntimeError, KeyError, ValueError, TypeError):
        return Execution("failed", code="action_failed", effect_completed=effects,
                         details={"completed_steps": completed, "total_steps": len(steps)})
    finally:
        anchors.clear()
        if broker is not None:
            try:
                broker.close()
            except BaseException:
                return Execution("unknown" if effects else "failed", code="cleanup_failed", effect_completed=effects)
