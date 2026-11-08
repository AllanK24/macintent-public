"""Mocked capture/ASR/cloud, real broker only on disposable synthetic roots."""

from copy import deepcopy
import hashlib
import io
import json
import threading
import time

import pytest

from macintent import contract, execution
from macintent.broker import Broker
from macintent.capture import CapturedCommand, CaptureError
from macintent.action_types import Execution, ActionError, ToolCall
from macintent.session import MacIntentSession, SessionError
from macintent import scripts, sandbox


CLIENT = "a" * 32
OTHER = "b" * 32
ORIGINAL = "A synthetic recorded request."


class Harness:
    def __init__(self, root, proposal, *, executor=execution.execute_proposal, capture_wait=False, capture_reason="silence", speech_fault=None, wait_at=None, cleanup_fault=False, **kwargs):
        self.calls = []
        self.messages = None
        self.capture_active = False
        self.speech_fault = speech_fault
        self.wait_at = wait_at
        self.entered = threading.Event()
        self.cancel_seen = threading.Event()
        self.release = threading.Event()
        owner = self

        class Model:
            def __init__(self, root, *, stop_event, advanced):
                assert advanced is True
                self.stop = stop_event
                self.closed = False
                owner.calls.append("planner_construct")
            def start(self):
                assert not self.closed
                owner.calls.append("planner_start")
            def generate(self, messages, tools):
                assert not owner.capture_active
                assert tools == contract.TOOLS
                owner.messages = deepcopy(messages)
                owner.calls.append("generate")
                owner.pause("planning", self.stop)
                return deepcopy(proposal)
            def close(self):
                self.closed = True
                owner.calls.append("planner_close")

        class Speech:
            def __init__(self, root, *, stop_event):
                self.stop = stop_event
                self.closed = False
                owner.calls.append("speech_construct")
            def start(self):
                assert not self.closed
                owner.calls.append("speech_start")
                owner.pause("starting", self.stop)
                if owner.speech_fault == "start": raise RuntimeError("PRIVATE synthetic startup")
            def transcribe(self, pcm):
                assert not self.closed
                assert not owner.capture_active
                owner.calls.append("transcribe")
                owner.pause("transcribing", self.stop)
                if owner.speech_fault == "transcribe": raise RuntimeError("PRIVATE synthetic ASR")
                return ORIGINAL
            def close(self):
                self.closed = True
                owner.calls.append("speech_close")
                if cleanup_fault:
                    raise RuntimeError("PRIVATE synthetic cleanup")

        class Microphone:
            def __init__(self, root, *, stop_event): self.stop = stop_event
            def check(self): owner.calls.append("capture_check")
            def capture(self, ready):
                owner.capture_active = True
                owner.calls.append("capture")
                try:
                    owner.pause("preparing", self.stop)
                    ready()
                    owner.pause("recording", self.stop)
                    if capture_wait:
                        assert self.stop.wait(3)
                        raise CaptureError("cancelled")
                    return CapturedCommand(b"synthetic PCM", 1.0, capture_reason)
                finally:
                    owner.capture_active = False
                    owner.calls.append("microphone_released")

        self.session = MacIntentSession(root, planner_factory=Model, speech_factory=Speech,
                                        capture_factory=Microphone, executor=executor, **kwargs)

    def pause(self, stage, stop):
        if self.wait_at == stage:
            self.entered.set()
            assert stop.wait(3)
            self.cancel_seen.set()
            assert self.release.wait(3)
            self.wait_at = None

    def wait(self, phase):
        with self.session._condition:
            assert self.session._condition.wait_for(lambda: self.session._phase == phase and (phase in {"approval", "plan", "recording"} or self.session._worker is None), timeout=5), self.session._phase
        return self.session.snapshot(CLIENT)

    def cmd(self, operation, **fields):
        state = self.session.snapshot(CLIENT)
        return self.session.command(CLIENT, operation, {"request_id": state["request_id"], **fields})

    def presence(self, operation, sequence):
        return self.session.command(CLIENT, operation, {"sequence": sequence})

    def connect(self):
        self.session.command(CLIENT, "connect", {"consent": True})
        return self.wait("idle")

    def send(self, notes=()):
        self.cmd("record")
        self.wait("review")
        self.cmd("send", corrections=list(notes))

    def decide(self, approval=True):
        state = self.wait("approval")
        self.cmd("decide", approval_id=state["pending"]["id"], approve=approval)
        return state


@pytest.fixture
def harnesses():
    instances = []
    def create(*args, **kwargs):
        instance = Harness(*args, **kwargs)
        instances.append(instance)
        return instance
    yield create
    for instance in instances:
        instance.release.set()
        instance.session.close()


def folder(name="Synthetic Bay"):
    return ToolCall("create_folder", {"name": name})


def test_inert_start_explicit_capture_and_original_plus_notes(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder())
    assert harness.calls == [] and not list(tmp_path.iterdir())
    with pytest.raises(SessionError, match="invalid_request"):
        harness.session.command(CLIENT, "connect", {"consent": "true"})
    harness.connect()
    assert "capture" not in harness.calls and "generate" not in harness.calls
    harness.cmd("record")
    review = harness.wait("review")
    assert review["transcript"] == ORIGINAL and "generate" not in harness.calls
    assert harness.calls.index("microphone_released") < harness.calls.index("transcribe")
    harness.cmd("send", corrections=["Use Synthetic Bay for the folder."])
    pending = harness.wait("approval")
    assert harness.messages == contract.transcript_messages(ORIGINAL, corrections=["Use Synthetic Bay for the folder."])
    assert pending["pending"]["action"] == {"tool": "create_directory", "arguments": {"path": "demo/workspace/Synthetic Bay"}}
    assert "ticket" not in json.dumps(pending)
    with pytest.raises(SessionError, match="stale_approval"):
        harness.cmd("decide", approval_id="\u2603", approve=True)
    assert not (tmp_path / "demo/workspace/Synthetic Bay").exists()
    assert harness.session.snapshot(OTHER)["phase"] == "occupied"
    assert harness.session.snapshot(OTHER)["transcript"] == ""
    with pytest.raises(SessionError, match="not_owner"):
        harness.session.command(OTHER, "decide", {"request_id": pending["request_id"], "approval_id": pending["pending"]["id"], "approve": True})
    harness.decide()
    done = harness.wait("completed")
    assert done["transcript"] == "" and done["pending"] is None
    assert (tmp_path / "demo/workspace/Synthetic Bay").is_dir()
    assert done["results"][0]["status"] == "completed"
    with pytest.raises(SessionError, match="stale_approval"):
        harness.cmd("decide", approval_id=pending["pending"]["id"], approve=True)
    old_id = done["request_id"]
    reset = harness.cmd("reset")
    assert reset["request_id"] != old_id
    with pytest.raises(SessionError, match="stale_request"):
        harness.session.command(CLIENT, "record", {"request_id": old_id})


def test_plan_review_is_not_approval_and_decline_preserves_first_effect(harnesses, tmp_path):
    proposal = contract.ActionPlan((folder(), ToolCall("create_text_file", {"filename": "readme.txt", "directory": "Synthetic Bay", "content": "Fixture only."})))
    harness = harnesses(tmp_path, proposal)
    harness.connect(); harness.send()
    plan = harness.wait("plan")
    assert not (tmp_path / "demo/workspace").exists()
    with pytest.raises(SessionError, match="stale_approval"):
        harness.cmd("decide", approval_id=plan["pending"]["id"], approve=True)
    harness.cmd("continue", approval_id=plan["pending"]["id"])
    first = harness.decide()
    second = harness.wait("approval")
    assert second["step"] == 2 and second["pending"]["id"] != first["pending"]["id"]
    assert second["results"][0]["status"] == "completed"
    harness.decide(False)
    done = harness.wait("declined")
    assert [result["status"] for result in done["results"]] == ["completed", "declined"]
    assert (tmp_path / "demo/workspace/Synthetic Bay").is_dir()
    assert not (tmp_path / "demo/workspace/Synthetic Bay/readme.txt").exists()


def test_plan_can_be_declined_without_broker_or_session_shutdown(harnesses, tmp_path):
    harness = harnesses(tmp_path, contract.ActionPlan((folder(), folder("Another Bay"))))
    harness.connect(); harness.send()
    plan = harness.wait("plan")
    with pytest.raises(SessionError, match="stale_approval"):
        harness.cmd("decide", approval_id=plan["pending"]["id"], approve=True)
    harness.cmd("decide", approval_id=plan["pending"]["id"], approve=False)
    done = harness.wait("declined")
    assert done["connected"] and not done["blocked"] and done["results"] == []
    assert not list(tmp_path.iterdir())


def test_known_second_step_failure_preserves_first_effect_without_block_latch(harnesses, tmp_path):
    harness = harnesses(tmp_path, contract.ActionPlan((folder(), folder())))
    harness.connect(); harness.send()
    plan = harness.wait("plan")
    harness.cmd("continue", approval_id=plan["pending"]["id"])
    harness.decide()
    failed = harness.wait("failed")
    assert failed["error"]["code"] == "destination_exists"
    assert not failed["blocked"] and failed["connected"]
    assert [result["status"] for result in failed["results"]] == ["completed", "failed"]
    assert (tmp_path / "demo/workspace/Synthetic Bay").is_dir()


def test_total_deadline_cancels_pending_approval_with_explicit_expiry_reason(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder())
    harness.connect(); harness.send(); harness.wait("approval")
    with harness.session._condition:
        harness.session._request_started = time.monotonic() - 301
    cancelled = harness.wait("cancelled")
    assert cancelled["error"]["code"] == "request_expired"
    assert cancelled["connected"]
    assert not (tmp_path / "demo/workspace/Synthetic Bay").exists()


@pytest.mark.parametrize("stage,code", [("start", "model_unavailable"), ("transcribe", "transcription_failed")])
def test_speech_failures_keep_closed_stage_without_private_exception(harnesses, tmp_path, stage, code):
    harness = harnesses(tmp_path, folder(), speech_fault=stage)
    if stage == "start":
        harness.session.command(CLIENT, "connect", {"consent": True})
    else:
        harness.connect(); harness.cmd("record")
    failed = harness.wait("failed")
    assert failed["error"]["code"] == code
    assert "PRIVATE" not in json.dumps(failed)
    assert "generate" not in harness.calls and not list(tmp_path.iterdir())
    assert failed["connected"] and not failed["blocked"]
    harness.speech_fault = None
    harness.cmd("record")
    harness.wait("review")


def test_script_creation_and_exact_captured_source_run_are_separately_bound(harnesses, tmp_path, monkeypatch):
    source = "a = 6\nb = 9\nprint(a + b)\n"
    ran = []
    def fake_sandbox(code, **kwargs):
        ran.append(code)
        return sandbox.SandboxResult("15\n", "", 0)
    monkeypatch.setattr(scripts, "run_script", fake_sandbox)
    proposal = contract.ActionPlan((ToolCall("create_python_script", {"filename": "total.py", "directory": ".", "code": source}), ToolCall("run_python_script", {"path": "total.py"})))
    harness = harnesses(tmp_path, proposal)
    harness.connect(); harness.send()
    plan = harness.wait("plan")
    harness.cmd("continue", approval_id=plan["pending"]["id"])
    creation = harness.decide()
    run = harness.wait("approval")
    assert ran == []
    assert run["pending"]["action"]["arguments"]["code"] == source
    assert run["pending"]["source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    with pytest.raises(SessionError, match="stale_approval"):
        harness.cmd("decide", approval_id=creation["pending"]["id"], approve=True)
    harness.decide()
    done = harness.wait("completed")
    assert ran == [source.encode()]
    assert done["results"][1]["details"]["stdout"] == "15\n"
    broker = Broker(tmp_path)
    try:
        events = broker.handle({"id": "audit-test", "op": "audit"})["result"]
        assert "print(a + b)" not in json.dumps(events) and "stdout" not in json.dumps(events)
    finally:
        broker.close()


def test_expiry_cannot_authorize_effect(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder())
    harness.connect(); harness.send()
    pending = harness.wait("approval")
    with harness.session._condition:
        harness.session._pending["expires_at"] = int(time.time()) - 1
        with pytest.raises(SessionError, match="approval_expired"):
            harness.cmd("decide", approval_id=pending["pending"]["id"], approve=True)
        harness.session._condition.notify_all()
    done = harness.wait("failed")
    assert not (tmp_path / "demo/workspace/Synthetic Bay").exists()
    assert done["connected"]


def test_capture_cancel_and_owner_lease_release_without_asr_or_planning(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder(), capture_wait=True, heartbeat_seconds=.1)
    harness.connect()
    harness.cmd("record")
    # Wait on lifecycle ownership rather than polling the owner heartbeat.
    with harness.session._condition:
        assert harness.session._condition.wait_for(lambda: harness.session._owner is None and harness.session._worker is None, timeout=4)
    assert not harness.capture_active
    assert "transcribe" not in harness.calls and "generate" not in harness.calls
    assert "microphone_released" in harness.calls
    snapshot = harness.session.snapshot(CLIENT)
    assert snapshot["phase"] == "disconnected" and snapshot["transcript"] == ""


def test_recording_limit_releases_capture_and_returns_control_without_asr_or_cloud(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder(), capture_reason="limit")
    harness.connect(); harness.cmd("record")
    failed = harness.wait("failed")
    assert failed["error"]["code"] == "recording_limit"
    assert failed["connected"] and not failed["working"] and not harness.capture_active
    assert "microphone_released" in harness.calls
    assert "transcribe" not in harness.calls and "generate" not in harness.calls
    assert failed["transcript"] == "" and failed["pending"] is None
    harness.cmd("reset")
    assert harness.session.snapshot(CLIENT)["phase"] == "idle"


def test_unknown_result_blocks_reconnect_and_has_no_hidden_retry(harnesses, tmp_path):
    calls = []
    def uncertain(*args, **kwargs):
        calls.append("attempt")
        return Execution("unknown", code="outcome_unknown")
    harness = harnesses(tmp_path, folder(), executor=uncertain)
    harness.connect(); harness.send()
    done = harness.wait("unknown")
    assert done["blocked"] and not done["connected"] and calls == ["attempt"]
    with pytest.raises(SessionError, match="session_blocked"):
        harness.session.command(CLIENT, "connect", {"consent": True})
    with pytest.raises(SessionError, match="session_blocked"):
        harness.cmd("reset")
    assert not list(tmp_path.iterdir())


def test_cancelled_connection_still_releases_abandoned_owner_lease(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder(), heartbeat_seconds=.5)
    harness.connect()
    harness.cmd("cancel")
    assert harness.wait("cancelled")["connected"]
    with harness.session._condition:
        assert harness.session._condition.wait_for(lambda: harness.session._owner is None and harness.session._worker is None, timeout=3)
    assert harness.session.snapshot(OTHER)["phase"] == "disconnected"
    harness.session.command(OTHER, "connect", {"consent": True})
    with harness.session._condition:
        assert harness.session._condition.wait_for(lambda: harness.session._phase == "idle" and harness.session._worker is None, timeout=3)
    assert harness.session.snapshot(OTHER)["connected"]


@pytest.mark.parametrize("stage", ["idle", "starting", "preparing", "recording", "transcribing", "review", "planning", "plan", "approval"])
def test_cancel_recovers_same_owner_only_after_cleanup_then_direct_record(harnesses, tmp_path, stage):
    gated = stage in {"starting", "preparing", "recording", "transcribing", "planning"}
    proposal = contract.ActionPlan((folder(), folder("Other Bay"))) if stage == "plan" else folder()
    harness = harnesses(tmp_path, proposal, wait_at=stage if gated else None)
    pending = None
    if stage == "starting":
        harness.session.command(CLIENT, "connect", {"consent": True})
    else:
        harness.connect()
        if stage in {"preparing", "recording", "transcribing", "review"}:
            harness.cmd("record")
            if stage == "review":
                harness.wait("review")
        elif stage in {"planning", "plan", "approval"}:
            harness.send(notes=["Synthetic review note."])
            if not gated:
                pending = harness.wait(stage)["pending"]
    if gated:
        assert harness.entered.wait(3)
    old_id = harness.session.snapshot(CLIENT)["request_id"]
    old_stop = harness.session._stop
    harness.cmd("cancel")
    if gated:
        assert harness.cancel_seen.wait(3)
        stopping = harness.session.snapshot(CLIENT)
        assert stopping["working"] and old_stop.is_set()
        assert harness.session._stop is old_stop
        captures = harness.calls.count("capture")
        for operation in ("record", "reset"):
            with pytest.raises(SessionError):
                harness.cmd(operation)
        assert harness.calls.count("capture") == captures
        harness.release.set()
    cancelled = harness.wait("cancelled")
    assert cancelled["connected"] and not cancelled["blocked"] and not cancelled["working"]
    assert cancelled["request_id"] == old_id and cancelled["pending"] is None
    assert cancelled["transcript"] == "" and harness.session._corrections == []
    assert harness.session._owner == CLIENT and old_stop.is_set()
    assert harness.session._stop is not old_stop and not harness.session._stop.is_set()
    assert harness.session._speech is harness.session._planner is harness.session._microphone is None
    assert not harness.capture_active
    assert not (tmp_path / "demo/workspace/Synthetic Bay").exists()
    if pending:
        with pytest.raises(SessionError, match="stale_approval"):
            harness.cmd("decide", approval_id=pending["id"], approve=True)
    # No reconnect/reset gesture is needed. Record itself creates a fresh request
    # and new resources; the permanently stopped old event is never cleared.
    harness.cmd("record")
    review = harness.wait("review")
    assert review["request_id"] != old_id and review["transcript"] == ORIGINAL
    assert harness.calls.count("planner_construct") == 2
    assert harness.calls.count("speech_construct") == 2
    with pytest.raises(SessionError, match="stale_request"):
        harness.session.command(CLIENT, "record", {"request_id": old_id})


def test_cancel_at_second_approval_preserves_completed_effect_and_allows_new_request(harnesses, tmp_path):
    harness = harnesses(tmp_path, contract.ActionPlan((folder(), folder("Other Bay"))))
    harness.connect(); harness.send()
    plan = harness.wait("plan")
    harness.cmd("continue", approval_id=plan["pending"]["id"])
    harness.decide()
    second = harness.wait("approval")
    assert second["step"] == 2
    harness.cmd("cancel")
    done = harness.wait("cancelled")
    assert done["connected"] and not done["blocked"]
    assert [result["status"] for result in done["results"]] == ["completed", "cancelled"]
    assert (tmp_path / "demo/workspace/Synthetic Bay").is_dir()
    assert not (tmp_path / "demo/workspace/Other Bay").exists()
    reset = harness.cmd("reset")
    assert reset["phase"] == "idle" and reset["request_id"] != done["request_id"]
    assert reset["results"] == []
    with pytest.raises(SessionError, match="stale_approval"):
        harness.cmd("decide", approval_id=second["pending"]["id"], approve=True)
    harness.cmd("record")
    harness.wait("review")


@pytest.mark.parametrize("reason", ["disconnect", "lease", "shutdown"])
def test_connection_stop_overrides_inflight_cancel_recovery(harnesses, tmp_path, reason):
    harness = harnesses(tmp_path, folder(), wait_at="recording")
    harness.connect(); harness.cmd("record")
    assert harness.entered.wait(3)
    harness.cmd("cancel")
    assert harness.cancel_seen.wait(3)
    old_stop = harness.session._stop
    closer = None
    if reason == "disconnect":
        harness.cmd("disconnect")
    elif reason == "lease":
        with harness.session._condition:
            harness.session._heartbeat = time.monotonic() - 16
            assert harness.session._condition.wait_for(lambda: harness.session._release, timeout=2)
    else:
        closer = threading.Thread(target=harness.session.close)
        closer.start()
        assert harness.session._shutdown.wait(2)
    harness.release.set()
    done = harness.wait("disconnected")
    if closer:
        closer.join(2)
        assert not closer.is_alive()
    assert not done["connected"] and harness.session._owner is None
    assert harness.session._stop is old_stop and old_stop.is_set()
    assert harness.calls.count("capture") == 1 and "transcribe" not in harness.calls


def test_cancel_cleanup_fault_cannot_unlock_recording_or_new_request(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder(), cleanup_fault=True)
    harness.connect(); harness.cmd("cancel")
    stopped = harness.wait("unknown")
    assert stopped["blocked"] and not stopped["connected"]
    assert stopped["error"]["code"] == "cleanup_failed"
    assert "PRIVATE" not in json.dumps(stopped)
    for operation in ("reset", "record"):
        with pytest.raises(SessionError, match="session_blocked"):
            harness.cmd(operation)


def test_cancel_keeps_controls_locked_until_resource_close_returns(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder())
    harness.connect()
    entered, release = threading.Event(), threading.Event()
    speech = harness.session._speech
    original_close = speech.close
    old_stop = harness.session._stop
    def close():
        entered.set()
        assert release.wait(3)
        original_close()
    speech.close = close
    try:
        harness.cmd("cancel")
        assert entered.wait(3)
        assert harness.session.snapshot(CLIENT)["working"]
        assert harness.session._stop is old_stop and old_stop.is_set()
        for operation in ("record", "reset"):
            with pytest.raises(SessionError):
                harness.cmd(operation)
        assert harness.calls.count("speech_construct") == 1
    finally:
        release.set()
    assert harness.wait("cancelled")["connected"]


@pytest.mark.parametrize("status,code", [("completed", None), ("cancelled", "cancelled"), ("unknown", "outcome_unknown"), ("failed", "audit_unavailable"), ("failed", "cleanup_failed")])
def test_cancel_during_execution_preserves_outcomes_and_blocks_late_safety_faults(harnesses, tmp_path, status, code):
    entered, released = threading.Event(), threading.Event()
    def executor(root, proposal, **kwargs):
        kwargs["on_step"](1, 1, proposal)
        entered.set()
        assert kwargs["stop_event"].wait(3)
        assert released.wait(3)
        if status == "completed":
            kwargs["on_result"](1, Execution("completed", effect_completed=True))
        return Execution(status, code=code)
    harness = harnesses(tmp_path, folder(), executor=executor)
    harness.connect(); harness.send()
    assert entered.wait(3)
    harness.cmd("cancel")
    released.set()
    fault = status not in {"completed", "cancelled"}
    done = harness.wait("unknown" if fault else "cancelled")
    assert done["blocked"] == fault and done["connected"] != fault
    if fault:
        assert harness.session._stop.is_set()
        with pytest.raises(SessionError, match="session_blocked"):
            harness.cmd("reset")
    elif status == "completed":
        assert done["results"][0]["status"] == "completed"


def test_terminal_result_drops_request_deadline_but_keeps_connected_review(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder())
    harness.connect(); harness.send(); harness.decide()
    done = harness.wait("completed")
    assert done["connected"] and len(done["results"]) == 1
    with harness.session._condition:
        assert harness.session._request_started == 0.0
    assert harness.session.snapshot(CLIENT)["phase"] == "completed"


def test_observed_nonzero_script_exit_allows_fresh_request_without_unknown_latch(harnesses, tmp_path, monkeypatch):
    workspace = tmp_path / "demo/workspace"
    workspace.mkdir(parents=True)
    (workspace / "calculation.py").write_text("print(15)\n")
    monkeypatch.setattr(scripts, "run_script", lambda *args, **kwargs: sandbox.SandboxResult("", "Synthetic calculation error.\n", 1))
    harness = harnesses(tmp_path, ToolCall("run_python_script", {"path": "calculation.py"}))
    harness.connect(); harness.send(); harness.decide()
    failed = harness.wait("failed")
    assert failed["error"]["code"] == "script_failed"
    assert not failed["blocked"] and failed["connected"]
    assert failed["results"][0]["details"]["exit_code"] == 1
    harness.cmd("record")
    assert harness.wait("review")["connected"]


def test_no_action_explanation_and_bad_corrections_never_construct_broker(harnesses, tmp_path):
    harness = harnesses(tmp_path, "What text should the note contain?")
    harness.connect(); harness.cmd("record"); harness.wait("review")
    with pytest.raises(SessionError, match="invalid_request"):
        harness.cmd("send", corrections=["x"] * 9)
    assert "generate" not in harness.calls
    harness.cmd("send", corrections=[])
    done = harness.wait("clarification")
    assert done["explanation"] == "What text should the note contain?"
    assert done["results"] == [] and done["pending"] is None and done["transcript"] == ""
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("cancel_first", [False, True])
def test_unexpected_dispatch_exception_is_sanitized_and_blocks_new_actions(harnesses, tmp_path, cancel_first):
    entered, release = threading.Event(), threading.Event()
    def broken(root, proposal, **kwargs):
        kwargs["on_step"](1, 1, proposal)
        if cancel_first:
            entered.set()
            assert kwargs["stop_event"].wait(3)
            assert release.wait(3)
        raise RuntimeError("PRIVATE synthetic exception detail")
    harness = harnesses(tmp_path, folder(), executor=broken)
    harness.connect(); harness.send()
    if cancel_first:
        assert entered.wait(3)
        harness.cmd("cancel")
        release.set()
    done = harness.wait("unknown")
    assert done["blocked"] and not done["connected"]
    assert "PRIVATE" not in json.dumps(done)
    assert done["error"]["code"] == "outcome_unknown"


def test_execution_observers_receive_copies_only_after_audit_end(tmp_path):
    events = []
    class ObservedBroker(Broker):
        def handle(self, message):
            reply = super().handle(message)
            events.append(message["op"])
            return reply
    def on_step(index, total, call):
        assert index == total == 1
        call.arguments["name"] = "Observer cannot replace target"
    def on_result(index, result):
        assert events[-1] == "end"
        result.details.clear()
        events.append("observed")
    outcome = execution.execute_proposal(tmp_path, folder(), confirm=lambda *args: True,
                                         stdout=io.StringIO(), broker_factory=ObservedBroker,
                                         on_step=on_step, on_result=on_result)
    assert outcome.status == "completed" and outcome.details["created"] is True
    assert (tmp_path / "demo/workspace/Synthetic Bay").is_dir()
    assert not (tmp_path / "demo/workspace/Observer cannot replace target").exists()
    assert events[-1] == "observed"


@pytest.mark.parametrize("code", ["invalid_output", "timeout", "rate_limited", "request_rejected", "credential_unavailable", "capture_timeout", "permission_denied"])
def test_ordinary_errors_allow_record_and_reset_without_reconnect(harnesses, tmp_path, code):
    harness = harnesses(tmp_path, folder())
    harness.connect()
    old_stop = harness.session._stop
    def fail(*args):
        raise ActionError(code)
    if code in {"capture_timeout", "permission_denied"}:
        harness.session._microphone.capture = fail
        harness.cmd("record")
    else:
        harness.session._planner.generate = fail
        harness.send()
    failed = harness.wait("failed")
    assert failed["error"]["code"] == code and failed["connected"]
    assert not failed["blocked"] and failed["pending"] is None
    assert old_stop.is_set() and harness.session._stop is not old_stop
    old_id = failed["request_id"]
    harness.cmd("record")
    assert harness.wait("review")["request_id"] != old_id
    harness.cmd("reset")
    assert harness.session.snapshot(CLIENT)["phase"] == "idle"
    assert not list(tmp_path.iterdir())


def test_error_recovery_waits_for_resource_cleanup(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder())
    harness.connect()
    entered, release = threading.Event(), threading.Event()
    close = harness.session._speech.close
    def delayed_close():
        entered.set()
        assert release.wait(3)
        close()
    def fail(*args):
        raise ActionError("invalid_output")
    harness.session._speech.close = delayed_close
    harness.session._planner.generate = fail
    old_stop = harness.session._stop
    try:
        harness.send()
        assert entered.wait(3)
        assert harness.session.snapshot(CLIENT)["working"]
        for operation in ("record", "reset"):
            with pytest.raises(SessionError):
                harness.cmd(operation)
        assert harness.session._stop is old_stop and old_stop.is_set()
    finally:
        release.set()
    assert harness.wait("failed")["connected"]


@pytest.mark.parametrize("stage", ["idle", "review"])
def test_tab_switch_preserves_settled_request_without_new_startup(harnesses, tmp_path, stage):
    harness = harnesses(tmp_path, folder())
    harness.connect()
    if stage == "review":
        harness.cmd("record"); harness.wait("review")
    before = harness.session.snapshot(CLIENT)
    calls = list(harness.calls)
    paused = harness.presence("pause", 1)
    assert paused["backgrounded"] and paused["phase"] == stage
    with pytest.raises(SessionError, match="invalid_state"):
        harness.cmd("record" if stage == "idle" else "send", **({"corrections": []} if stage == "review" else {}))
    returned = harness.presence("resume", 2)
    assert returned["connected"] and not returned["backgrounded"]
    assert returned["reclaimed"] is False
    assert returned["request_id"] == before["request_id"] and returned["transcript"] == before["transcript"]
    assert harness.calls == calls
    harness.presence("pause", 3)
    assert harness.presence("resume", 2)["backgrounded"]
    harness.presence("resume", 4)
    if stage == "review":
        harness.cmd("reset")
    harness.cmd("record"); harness.wait("review")


def test_tab_switch_cancels_active_capture_but_return_never_restarts_it(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder(), wait_at="recording")
    harness.connect(); harness.cmd("record")
    assert harness.entered.wait(3)
    harness.presence("pause", 1)
    assert harness.cancel_seen.wait(3)
    returned = harness.presence("resume", 2)
    assert returned["working"]
    with pytest.raises(SessionError):
        harness.cmd("record")
    harness.release.set()
    stopped = harness.wait("cancelled")
    assert stopped["connected"] and "transcribe" not in harness.calls
    assert harness.calls.count("capture") == 1
    # A delayed hidden-page request must not cancel a fresh visible request.
    harness.presence("pause", 1)
    assert not harness.session.snapshot(CLIENT)["backgrounded"]
    harness.cmd("record"); harness.wait("review")
    assert harness.calls.count("capture") == 2


def test_return_after_background_lease_expiry_reclaims_consent_not_work(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder())
    harness.connect(); harness.presence("pause", 1)
    with harness.session._condition:
        harness.session._heartbeat = time.monotonic() - 16
        assert harness.session._condition.wait_for(lambda: harness.session._owner is None and harness.session._worker is None, timeout=3)
    calls = list(harness.calls)
    with pytest.raises(SessionError, match="not_connected"):
        harness.session.command(OTHER, "resume", {"sequence": 2})
    returned = harness.presence("resume", 2)
    assert returned["phase"] == "idle" and returned["connected"] and not returned["working"]
    assert returned["reclaimed"] is True
    assert harness.calls == calls and harness.session._microphone is None
    assert "capture" not in calls and "generate" not in calls
    harness.cmd("record"); harness.wait("review")


def test_return_waits_for_expired_lease_cleanup_and_cannot_revive_approvals(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder(), wait_at="planning")
    harness.connect(); harness.send()
    assert harness.entered.wait(3)
    harness.presence("pause", 1)
    assert harness.cancel_seen.wait(3)
    with harness.session._condition:
        harness.session._heartbeat = time.monotonic() - 16
        assert harness.session._condition.wait_for(lambda: harness.session._release, timeout=3)
    with pytest.raises(SessionError, match="busy"):
        harness.presence("resume", 2)
    harness.release.set(); harness.wait("disconnected")
    harness.presence("resume", 3)
    assert harness.session.snapshot(CLIENT)["pending"] is None
    assert not (tmp_path / "demo/workspace").exists()


def test_returning_page_cannot_displace_new_owner_after_lease_expiry(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder())
    harness.connect(); harness.presence("pause", 1)
    with harness.session._condition:
        harness.session._heartbeat = time.monotonic() - 16
        assert harness.session._condition.wait_for(lambda: harness.session._owner is None and harness.session._worker is None, timeout=3)
    harness.session.command(OTHER, "connect", {"consent": True})
    with harness.session._condition:
        assert harness.session._condition.wait_for(lambda: harness.session._phase == "idle" and harness.session._worker is None, timeout=3)
    calls = list(harness.calls)
    with pytest.raises(SessionError, match="not_connected"):
        harness.presence("resume", 2)
    assert harness.session.snapshot(CLIENT)["phase"] == "occupied"
    assert harness.session._owner == OTHER and harness.calls == calls


def test_hidden_approval_is_invalidated_and_disconnect_revokes_return_consent(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder())
    harness.connect(); harness.send()
    pending = harness.wait("approval")
    harness.presence("pause", 1)
    harness.wait("cancelled")
    harness.presence("resume", 2)
    with pytest.raises(SessionError, match="stale_approval"):
        harness.cmd("decide", approval_id=pending["pending"]["id"], approve=True)
    harness.cmd("disconnect"); harness.wait("disconnected")
    with pytest.raises(SessionError, match="not_connected"):
        harness.presence("resume", 3)
    assert not (tmp_path / "demo/workspace/Synthetic Bay").exists()


def test_return_never_unlocks_cleanup_fault(harnesses, tmp_path):
    harness = harnesses(tmp_path, folder(), cleanup_fault=True, wait_at="recording")
    harness.connect(); harness.cmd("record")
    assert harness.entered.wait(3)
    harness.presence("pause", 1)
    assert harness.cancel_seen.wait(3)
    harness.release.set(); harness.wait("unknown")
    with pytest.raises(SessionError, match="session_blocked"):
        harness.presence("resume", 2)
    for operation in ("record", "reset"):
        with pytest.raises(SessionError, match="session_blocked"):
            harness.cmd(operation)


@pytest.mark.parametrize("sequence", [True, 0, -1, 1.5, "1", 9007199254740992])
def test_presence_sequence_is_closed_bounded_and_requires_consent(harnesses, tmp_path, sequence):
    harness = harnesses(tmp_path, folder())
    harness.connect()
    with pytest.raises(SessionError, match="invalid_request"):
        harness.presence("pause", sequence)


@pytest.mark.parametrize("code", ["audit_unavailable", "broker_rejected", "invalid_confirmation"])
def test_safety_contract_errors_never_use_ordinary_error_recovery(harnesses, tmp_path, code):
    harness = harnesses(tmp_path, folder())
    harness.connect()
    def fail(*args):
        raise ActionError(code)
    harness.session._planner.generate = fail
    harness.send()
    failed = harness.wait("unknown")
    assert failed["blocked"] and not failed["connected"]
    for operation in ("record", "reset"):
        with pytest.raises(SessionError, match="session_blocked"):
            harness.cmd(operation)
    with pytest.raises(SessionError, match="session_blocked"):
        harness.presence("resume", 1)
