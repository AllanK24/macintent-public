"""Disposable synthetic roots only: no cloud, microphone, or live workspace."""

from copy import deepcopy
import io
import json
import os
import threading
import uuid

import pytest

from macintent import contract, execution
from macintent import planner, groq_contract
from macintent import scripts, sandbox
from macintent.audit import ActionAudit, AuditError
from macintent.broker import Broker
from macintent.action_types import Execution, ActionError, ToolCall
from macintent.workspace import WorkspaceActionError, WorkspaceActions


CODE = 'import math\nprint(math.factorial(5))\n'


def rpc(broker, op, **fields):
    return broker.handle({"id": uuid.uuid4().hex, "op": op, **fields})


def plan():
    return contract.ActionPlan((ToolCall("create_folder", {"name": "Cedar Lab"}),
                               ToolCall("create_text_file", {"filename": "readme.txt", "directory": "Cedar Lab", "content": "Synthetic text"})))


def run(root, proposal, *, decisions=(True, True), **kwargs):
    approvals = []
    choices = iter(decisions)
    def confirm(action, expiry):
        approvals.append(deepcopy(action))
        return next(choices)
    result = execution.execute_proposal(root, proposal, confirm_plan=lambda _: True, confirm=confirm,
                                        stdout=io.StringIO(), **kwargs)
    return result, approvals


def native_response(name, arguments):
    return json.dumps({"model": planner.MODEL_ID, "choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None, "tool_calls": [{"id": "synthetic", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}}]}}]}).encode()


def test_current_contract_and_plans_are_closed():
    original = deepcopy(groq_contract.TOOLS)
    messages = contract.transcript_messages("Create a synthetic folder and a note.", corrections=["The folder is Cedar Lab."])
    planner._validate_request(messages, contract.TOOLS, advanced=True)
    assert planner._validate_request(messages, contract.TOOLS) == messages[1]["content"]
    value = contract.proposal_value(plan())
    assert planner._parse_response(native_response(value["tool"], value["arguments"]), advanced=True) == plan()
    assert planner._parse_response(native_response(value["tool"], value["arguments"])) == plan()
    assert groq_contract.TOOLS == original and len(original) == 6
    assert len(contract.TOOLS) == 9
    assert not any("approve" in t["parameters"]["properties"] for t in contract.TOOLS)


@pytest.mark.parametrize("arguments", [
    {"steps": []}, {"steps": [{"tool": "create_folder", "arguments": {"name": "Cedar"}}]},
    {"steps": [{"tool": "list_files", "arguments": {"directory": "."}}] * 5},
    {"steps": [{"tool": "plan_actions", "arguments": {"steps": []}}] * 2},
    {"steps": [{"tool": "delete_file", "arguments": {"path": "../outside"}}] * 2},
    {"steps": [{"tool": "create_folder", "arguments": {"name": "Cedar", "approve": "yes"}}] * 2},
])
def test_malformed_nested_excessive_and_unsafe_plans_fail(arguments):
    with pytest.raises(ActionError):
        contract.parse_native_call(contract.PLAN_TOOL, arguments)


@pytest.mark.parametrize("code", ["", "#\x00", "print(", "from . import helper", "import synthetic_third_party", "#" * 8193])
def test_script_validation_never_evaluates_bad_source(code):
    with pytest.raises(WorkspaceActionError):
        scripts.validate_script_code(code)


def test_script_capability_requires_trusted_broker_configuration(tmp_path):
    broker = Broker(tmp_path)
    try:
        assert not {"create_python_script", "run_python_script"} & {t["name"] for t in rpc(broker, "hello")["result"]["tools"]}
        request = rpc(broker, "begin")["result"]["request_id"]
        assert rpc(broker, "prepare", request_id=request, tool="create_python_script", arguments={"path": "demo/workspace/a.py", "code": CODE})["error"]["code"] == "unknown_tool"
    finally:
        broker.close()


def test_directory_then_note_has_two_approvals_and_correlated_audit(tmp_path):
    result, approvals = run(tmp_path, plan())
    assert result.status == "completed" and result.details["completed_steps"] == 2
    assert [a["tool"] for a in approvals] == ["create_directory", "create_note"]
    assert (tmp_path / "demo/workspace/Cedar Lab/readme.txt").read_text() == "Synthetic text"
    broker = Broker(tmp_path)
    try:
        events = rpc(broker, "audit")["result"]["events"]
        starts = [e for e in events if e["event"] == "execution_started"]
        assert [e["result"]["step_index"] for e in starts] == [1, 2]
        assert len({e["result"]["workflow_id"] for e in starts}) == 1
        assert "Synthetic text" not in json.dumps(events)
    finally:
        broker.close()


@pytest.mark.parametrize("decisions,completed", [((False,), 0), ((True, False), 1)])
def test_decline_stops_plan_without_rollback(tmp_path, decisions, completed):
    result, approvals = run(tmp_path, plan(), decisions=decisions)
    assert result.status == "declined" and result.details["completed_steps"] == completed
    assert len(approvals) == completed + 1
    assert not (tmp_path / "demo/workspace/Cedar Lab/readme.txt").exists()
    assert (tmp_path / "demo/workspace/Cedar Lab").exists() == bool(completed)


def test_failed_step_stops_and_prior_created_files_remain(tmp_path):
    (tmp_path / "demo/workspace/Cedar Lab").mkdir(parents=True)
    result, approvals = run(tmp_path, plan())
    assert result.status == "failed" and result.details["completed_steps"] == 0
    assert approvals == [] and not (tmp_path / "demo/workspace/Cedar Lab/readme.txt").exists()


def test_script_create_then_run_has_distinct_exact_approvals_no_audit_code_or_output(tmp_path, monkeypatch):
    calls = []
    def fake_sandbox(code, **kwargs):
        calls.append(code)
        return sandbox.SandboxResult("synthetic-output\n", "", 0)
    monkeypatch.setattr(scripts, "run_script", fake_sandbox)
    proposal = contract.ActionPlan((ToolCall("create_python_script", {"filename": "calc.py", "directory": ".", "code": CODE}),
                                   ToolCall("run_python_script", {"path": "calc.py"})))
    result, approvals = run(tmp_path, proposal)
    assert result.status == "completed" and len(approvals) == 2 and calls == [CODE.encode()]
    assert approvals[1] == {"tool": "run_python_script", "arguments": {"path": "demo/workspace/calc.py", "code": CODE}}
    assert (tmp_path / "demo/workspace/calc.py").stat().st_mode & 0o777 == 0o600
    broker = Broker(tmp_path)
    try:
        audit = json.dumps(rpc(broker, "audit")["result"])
        assert "factorial" not in audit and "synthetic-output" not in audit
        for event in rpc(broker, "audit")["result"]["events"]:
            if event["result"] is not None:
                ActionAudit._validate_result(event["result"], tool=event["tool"])
    finally:
        broker.close()


def test_creation_is_exclusive_and_run_binding_refuses_changed_source(tmp_path, monkeypatch):
    monkeypatch.setattr(scripts, "run_script", lambda *a, **kw: pytest.fail("changed source ran"))
    workspace = WorkspaceActions(tmp_path)
    script_tools = scripts.ScriptActions(workspace)
    path = tmp_path / "demo/workspace/calc.py"
    try:
        create = script_tools.prepare("create_python_script", {"path": "demo/workspace/calc.py", "code": CODE})
        script_tools.execute(create)
        with pytest.raises(WorkspaceActionError, match="destination_exists"):
            script_tools.prepare("create_python_script", {"path": "demo/workspace/calc.py", "code": CODE})
        prepared = script_tools.prepare("run_python_script", {"path": "demo/workspace/calc.py"})
        path.write_text("print(6)\n")
        with pytest.raises(WorkspaceActionError, match="script_source_changed"):
            script_tools.execute(prepared)
    finally:
        workspace.close()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_linked_script_sources_refused(tmp_path, kind):
    workspace = WorkspaceActions(tmp_path)
    try:
        source = tmp_path / "demo/workspace/source.py"
        source.write_text(CODE)
        target = source.with_name("target.py")
        target.symlink_to(source) if kind == "symlink" else os.link(source, target)
        with pytest.raises(WorkspaceActionError):
            scripts.ScriptActions(workspace).prepare("run_python_script", {"path": "demo/workspace/target.py"})
    finally:
        workspace.close()


def test_script_run_needs_fresh_approval_and_expiring_ticket(tmp_path, monkeypatch):
    broker = Broker(tmp_path, script_operations=True)
    monkeypatch.setattr(scripts, "run_script", lambda *a, **kw: pytest.fail("unapproved script ran"))
    try:
        (tmp_path / "demo/workspace/a.py").write_text(CODE)
        request = rpc(broker, "begin")["result"]["request_id"]
        prepared = rpc(broker, "prepare", request_id=request, tool="run_python_script", arguments={"path": "demo/workspace/a.py"})["result"]
        broker.active.ticket_deadline = 0
        assert rpc(broker, "decide", request_id=request, ticket=prepared["ticket"], approve=True)["error"]["code"] == "expired"
        assert rpc(broker, "execute", request_id=request, ticket=prepared["ticket"])["ok"] is False
    finally:
        broker.close()


def test_audit_rejects_code_output_and_bad_script_metadata():
    for result in (
        {"status": "planned", "arguments": {"path": "demo/workspace/a.py", "code": CODE}},
        {"status": "executed", "details": {"path": "demo/workspace/a.py", "bytes": 1, "exit_code": 0, "executed": True, "stdout": "private"}},
        {"status": "executed", "details": {"path": "demo/workspace/a.py", "bytes": 1, "exit_code": True, "executed": True}},
    ):
        with pytest.raises(AuditError):
            ActionAudit._validate_result(result, tool="run_python_script")


def test_audit_fault_and_cancellation_stop_before_effect(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        if kwargs.get("event") == "execution_started":
            raise AuditError("audit_unavailable")
        return original(*args, **kwargs)
    original = ActionAudit.record
    monkeypatch.setattr(ActionAudit, "record", broken)
    result, _ = run(tmp_path, plan())
    assert result.status == "failed" and not (tmp_path / "demo/workspace/Cedar Lab").exists()
    stop = threading.Event()
    stop.set()
    result, approvals = run(tmp_path, plan(), stop_event=stop)
    assert result.status == "cancelled" and approvals == []


def test_nonzero_script_run_stops_remaining_actions(tmp_path, monkeypatch):
    (tmp_path / "demo/workspace").mkdir(parents=True)
    (tmp_path / "demo/workspace/a.py").write_text(CODE)
    monkeypatch.setattr(scripts, "run_script", lambda *a, **kw: sandbox.SandboxResult("", "synthetic error", 1))
    proposal = contract.ActionPlan((ToolCall("run_python_script", {"path": "a.py"}), ToolCall("create_folder", {"name": "Never"})))
    result, approvals = run(tmp_path, proposal)
    assert result.status == "failed" and result.effect_completed and len(approvals) == 1
    assert not (tmp_path / "demo/workspace/Never").exists()


def test_unavailable_os_sandbox_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Unsupported")
    with pytest.raises(sandbox.SandboxError, match="script_execution_unavailable"):
        sandbox.run_script(CODE.encode(), root=tmp_path)


def test_setup_interpreter_matches_the_sandbox_runtime_pin():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    version = (root / ".python-version").read_text().strip()
    assert sandbox._RUNTIME_NAME == f"cpython-{version}-macos-aarch64-none"
    bootstrap = (root / "scripts/bootstrap.sh").read_text()
    assert f"\nuv python install {version}\n" in bootstrap
    assert f"\nuv python find {version}\n" in bootstrap


def test_interrupt_during_audit_finalization_preserves_observed_effect_and_stops(tmp_path):
    class InterruptedEnd(Broker):
        def handle(self, message):
            if message.get("op") == "end":
                raise KeyboardInterrupt
            return super().handle(message)
    result, approvals = run(tmp_path, plan(), broker_factory=InterruptedEnd)
    assert result.status == "failed" and result.code == "audit_unavailable" and result.effect_completed
    assert len(approvals) == 1 and (tmp_path / "demo/workspace/Cedar Lab").is_dir()
    assert not (tmp_path / "demo/workspace/Cedar Lab/readme.txt").exists()
