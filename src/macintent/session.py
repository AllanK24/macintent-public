"""Single-operator web session over the unchanged Groq/native/broker pipeline.

All speech, corrections, source and output are request-local memory. This module
never parses natural language, repairs proposals, or issues model approvals.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any
import uuid

from .contract import ActionPlan, TOOLS, action_arguments, transcript_messages, validate_proposal
from .execution import execute_proposal
from .broker import _STATIC_MESSAGES
from .planner import GroqPlanner, output_error_help
from .speech import SpeechEngine
from .capture import CaptureError, CommandMicrophone
from .action_types import Execution, ActionError, ToolCall


ERROR_CODES = frozenset({
    "invalid_request", "invalid_state", "busy", "not_owner", "stale_request",
    "stale_approval", "not_connected", "approval_expired", "session_blocked", "unavailable",
})
_KNOWN_FAILURES = frozenset(_STATIC_MESSAGES) | frozenset({
    "invalid_capture", "permission_denied", "unsupported_device", "native_unavailable",
    "capture_timeout", "capture_busy", "helper_outdated", "cleanup_failed", "cancelled",
    "credential_unavailable", "timeout", "unauthorized", "rate_limited", "unavailable",
    "request_rejected", "invalid_output", "invalid_command", "transcription_failed",
    "no_speech", "no_words", "recording_limit", "request_expired", "connection_expired",
    "action_failed", "audit_or_result_failed", "script_failed", "runtime_error",
    "broker_rejected", "closed", "invalid_confirmation", "invalid_model_output",
    "invalid_tool_arguments", "model_unavailable",
})
_CLIENT = re.compile(r"[0-9a-f]{32}\Z")
_TERMINAL = {"completed", "declined", "cancelled", "failed", "unknown", "clarification"}


class SessionError(Exception):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code = code if code in ERROR_CODES else "unavailable"
        self.status = status
        super().__init__(self.code)


class _DiscardOutput:
    """Existing CLI executor prints are not a web protocol or a log sink."""
    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


def _display_action(call: ToolCall) -> dict[str, Any]:
    tool, arguments = action_arguments(call)
    return {"tool": tool, "arguments": arguments}


class MacIntentSession:
    def __init__(self, root: Path, *, planner_factory: Any = GroqPlanner,
                 speech_factory: Any = SpeechEngine, capture_factory: Any = CommandMicrophone,
                 executor: Any = execute_proposal, heartbeat_seconds: float = 15.0,
                 request_seconds: float = 300.0) -> None:
        # Construction is inert: no key/model/helper/audit/workspace access.
        self.root = Path(root)
        self._planner_factory, self._speech_factory = planner_factory, speech_factory
        self._capture_factory, self._executor = capture_factory, executor
        self._condition = threading.Condition(threading.RLock())
        self._shutdown = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._planner = self._speech = self._microphone = None
        self._owner: str | None = None
        self._connected = False
        self._release = False
        self._consented_client: str | None = None
        self._background = False
        self._presence_sequence = 0
        self._blocked = False
        self._phase = "disconnected"
        self._request_id = ""
        self._transcript = ""
        self._corrections: list[str] = []
        self._steps: list[dict[str, Any]] = []
        self._results: list[dict[str, Any]] = []
        self._pending: dict[str, Any] | None = None
        self._decision: bool | None = None
        self._step = 0
        self._version = 0
        self._error: dict[str, Any] | None = None
        self._explanation = ""
        self._heartbeat = 0.0
        self._request_started = 0.0
        self._heartbeat_seconds, self._request_seconds = heartbeat_seconds, request_seconds
        self._watcher = threading.Thread(target=self._watch, daemon=True, name="macintent-lease")
        self._watcher.start()

    def _check_client(self, client: Any) -> None:
        if type(client) is not str or not _CLIENT.fullmatch(client):
            raise SessionError("invalid_request", 400)

    def snapshot(self, client: str) -> dict[str, Any]:
        self._check_client(client)
        with self._condition:
            self._version += 1
            if self._owner is not None and self._owner != client:
                return {"phase": "occupied", "connected": False, "blocked": self._blocked,
                        "backgrounded": False,
                        "request_id": "", "transcript": "", "steps": [], "results": [],
                        "pending": None, "error": None, "explanation": "", "step": 0, "working": True,
                        "version": self._version}
            if self._owner == client:
                self._heartbeat = time.monotonic()
            return deepcopy({
                "phase": self._phase, "connected": self._connected, "blocked": self._blocked,
                "backgrounded": self._background if self._owner == client else False,
                "request_id": self._request_id, "transcript": self._transcript,
                "steps": self._steps, "results": self._results, "pending": self._pending,
                "error": self._error, "explanation": self._explanation,
                "step": self._step, "working": self._worker is not None,
                "version": self._version,
            })

    def command(self, client: str, operation: str, payload: Any) -> dict[str, Any]:
        self._check_client(client)
        if type(payload) is not dict:
            raise SessionError("invalid_request", 400)
        with self._condition:
            if self._shutdown.is_set():
                raise SessionError("unavailable", 503)
            if operation == "connect":
                if set(payload) != {"consent"} or payload["consent"] is not True:
                    raise SessionError("invalid_request", 400)
                if self._blocked:
                    raise SessionError("session_blocked")
                if self._owner not in (None, client):
                    raise SessionError("busy")
                if self._worker is not None or self._connected:
                    raise SessionError("invalid_state")
                self._owner, self._release = client, False
                self._consented_client = client
                self._background = False
                self._presence_sequence = 0
                self._stop = threading.Event()
                self._clear_request()
                self._phase = "starting"
                self._heartbeat = time.monotonic()
                self._launch(self._start)
                return self.snapshot(client)
            if operation in {"pause", "resume"}:
                return self._presence(client, operation, payload)
            # A closed/sleeping page can revoke its own remembered consent even
            # after the runtime lease has released its request and ownership.
            if operation == "disconnect" and self._owner is None and self._consented_client == client:
                if set(payload) != {"request_id"} or type(payload["request_id"]) is not str:
                    raise SessionError("invalid_request", 400)
                self._consented_client = None
                return self.snapshot(client)
            if self._owner != client:
                raise SessionError("not_owner", 403)
            expected = {"request_id"}
            if operation == "send":
                expected.add("corrections")
            elif operation == "continue":
                expected.add("approval_id")
            elif operation == "decide":
                expected.update({"approval_id", "approve"})
            elif operation not in {"record", "cancel", "reset", "disconnect"}:
                raise SessionError("invalid_request", 400)
            if set(payload) != expected:
                raise SessionError("invalid_request", 400)
            if type(payload["request_id"]) is not str or payload["request_id"] != self._request_id:
                raise SessionError("stale_request")
            self._heartbeat = time.monotonic()
            if self._blocked and operation not in {"cancel", "disconnect"}:
                raise SessionError("session_blocked")
            if self._background and operation not in {"cancel", "disconnect"}:
                raise SessionError("invalid_state")
            if operation in {"cancel", "disconnect"}:
                if operation == "disconnect":
                    self._consented_client = None
                self._request_stop(release=operation == "disconnect")
            elif operation == "reset":
                if self._worker is not None:
                    raise SessionError("busy")
                if self._blocked:
                    raise SessionError("session_blocked")
                if not self._connected:
                    raise SessionError("not_connected")
                self._clear_request()
                self._phase = "idle"
            elif operation == "record":
                self._ready(self._phase if self._phase in {"cancelled", "failed"} else "idle")
                if self._phase in {"cancelled", "failed"}:
                    self._clear_request()
                self._request_started = time.monotonic()
                self._phase = "preparing"
                self._launch(self._capture)
            elif operation == "send":
                self._ready("review")
                try:
                    transcript_messages(self._transcript, corrections=payload["corrections"])
                except (ActionError, TypeError, ValueError, UnicodeError):
                    raise SessionError("invalid_request", 400) from None
                self._corrections = list(payload["corrections"])
                self._phase = "planning"
                self._launch(self._plan_execute)
            elif operation in {"continue", "decide"}:
                pending = self._pending
                # Plan rejection is permitted, but only continue can acknowledge
                # plan review; decide(True) can never approve a whole plan.
                kind = "plan" if operation == "continue" or (operation == "decide" and pending is not None and pending["kind"] == "plan" and payload["approve"] is False) else "action"
                if (pending is None or pending["kind"] != kind or self._decision is not None
                        or type(payload["approval_id"]) is not str
                        or not payload["approval_id"].isascii()
                        or not secrets.compare_digest(payload["approval_id"], pending["id"])):
                    raise SessionError("stale_approval")
                if time.time() >= pending["expires_at"]:
                    raise SessionError("approval_expired", 410)
                if operation == "decide" and type(payload["approve"]) is not bool:
                    raise SessionError("invalid_request", 400)
                self._decision = True if operation == "continue" else payload["approve"]
                self._phase = "executing"
                self._condition.notify_all()
            return self.snapshot(client)

    def _presence(self, client: str, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Presence is session-scoped, never an approval or a retry. Sequence
        # numbers make a late hidden-page POST inert after a newer return POST.
        if (set(payload) != {"sequence"} or type(payload["sequence"]) is not int
                or not 1 <= payload["sequence"] <= 9007199254740991):
            raise SessionError("invalid_request", 400)
        if self._blocked:
            raise SessionError("session_blocked")
        if self._consented_client != client:
            raise SessionError("not_connected")
        if self._owner not in (None, client):
            raise SessionError("busy")
        if payload["sequence"] <= self._presence_sequence:
            return self.snapshot(client)
        if operation == "resume" and self._release and self._worker is not None:
            raise SessionError("busy")
        self._presence_sequence = payload["sequence"]
        reclaimed = False
        if operation == "pause":
            self._background = True
            if self._owner == client and self._worker is not None:
                self._request_stop()
        else:
            if self._owner is None:
                # Lease expiry already confirmed cleanup. Reclaim only this
                # page's consent, without loading models or starting capture.
                self._owner, self._release = client, False
                self._stop = threading.Event()
                self._clear_request()
                self._connected, self._phase = True, "idle"
                reclaimed = True
            self._background = False
        self._heartbeat = time.monotonic()
        return {**self.snapshot(client), "reclaimed": reclaimed}

    def _clear_request(self) -> None:
        self._request_id = uuid.uuid4().hex
        self._transcript = self._explanation = ""
        self._corrections.clear()
        self._steps.clear()
        self._results.clear()
        self._pending = None
        self._decision = None
        self._step = 0
        self._error = None
        self._request_started = 0.0

    def _ready(self, phase: str) -> None:
        if self._blocked:
            raise SessionError("session_blocked")
        if not self._connected or self._stop.is_set():
            raise SessionError("not_connected")
        if self._worker is not None or self._phase != phase:
            raise SessionError("invalid_state")

    def _launch(self, job: Any) -> None:
        if self._worker is not None:
            raise SessionError("busy")
        self._worker = threading.Thread(target=self._run, args=(job,), daemon=True, name="macintent-request")
        self._worker.start()

    def _run(self, job: Any) -> None:
        try:
            job()
        except KeyboardInterrupt:
            with self._condition:
                if self._phase not in {"unknown", "failed"}:
                    self._phase = "cancelled"
        except (ActionError, CaptureError) as exc:
            with self._condition:
                uncertain = self._phase in {"approval", "executing", "cancelling"} and bool(self._steps)
                self._failure(exc.code, output_error_help(exc) if isinstance(exc, ActionError) else None)
                if uncertain:
                    self._blocked = True
                    self._phase = "unknown"
                # Capture/transport/policy failures never trigger automatic retry.
                if exc.code not in {"no_speech", "no_words", "recording_limit"}:
                    self._stop.set()
        except BaseException:
            with self._condition:
                uncertain = self._phase in {"approval", "executing", "cancelling"} and bool(self._steps)
                self._failure("outcome_unknown" if uncertain else "cancelled" if self._stop.is_set() else "runtime_error")
                self._stop.set()
        finally:
            if self._stop.is_set():
                cleaned = self._close_resources()
                with self._condition:
                    self._connected = False
                    if not cleaned:
                        self._blocked = True
                        self._phase = "unknown"
                        self._error = {"code": "cleanup_failed", "help": None}
                    elif self._phase == "cancelling":
                        self._phase = "cancelled"
            with self._condition:
                if self._phase in _TERMINAL or self._release:
                    self._request_started = 0.0
                    self._transcript = ""
                    self._corrections.clear()
                    self._steps.clear()
                    self._pending = None
                    self._decision = None
                self._worker = None
                # Never clear an event still retained by a stopped resource.
                # The job has returned and all owned cleanup has completed;
                # fresh resources are loaded only on the next explicit Record.
                if (self._stop.is_set() and self._owner == self._consented_client
                        and self._owner is not None and not self._blocked and not self._release
                        and not self._shutdown.is_set()
                        and self._phase in {"cancelled", "failed", "completed", "declined"}):
                    self._stop = threading.Event()
                    self._connected = True
                    if self._phase in {"completed", "declined"}:
                        self._phase = "cancelled"
                if self._release:
                    self._owner = None
                    self._clear_request()
                    self._phase = "blocked" if self._blocked else "disconnected"
                self._condition.notify_all()

    def _start(self) -> None:
        self._open_resources()
        with self._condition:
            self._check_stopped()
            self._connected, self._phase = True, "idle"

    def _open_resources(self) -> None:
        self._check_stopped()
        self._microphone = self._capture_factory(self.root, stop_event=self._stop)
        self._microphone.check()  # Validation only; no device query or recording.
        self._planner = self._planner_factory(self.root, stop_event=self._stop, advanced=True)
        self._speech = self._speech_factory(self.root, stop_event=self._stop)
        self._planner.start()  # Credential readiness only; no HTTP.
        try:
            self._speech.start()
        except RuntimeError:
            self._check_stopped()
            raise ActionError("model_unavailable") from None
        self._check_stopped()

    def _check_stopped(self) -> None:
        if self._stop.is_set():
            raise KeyboardInterrupt

    def _capture(self) -> None:
        captured = pcm = transcript = None
        try:
            if self._microphone is None:
                self._open_resources()
            self._check_stopped()
            def ready() -> None:
                with self._condition:
                    self._check_stopped()
                    self._phase = "recording"
                    self._condition.notify_all()
            captured = self._microphone.capture(ready)
            # capture() returns only after owned helper exit and microphone release.
            self._check_stopped()
            if captured.reason == "limit":
                raise ActionError("recording_limit")
            if captured.reason == "no_speech" or captured.pcm is None:
                raise ActionError("no_speech")
            pcm, captured = captured.pcm, None
            with self._condition:
                self._check_stopped()
                self._phase = "transcribing"
            try:
                transcript = self._speech.transcribe(pcm)
            except RuntimeError:
                self._check_stopped()
                raise ActionError("transcription_failed") from None
            pcm = None
            self._check_stopped()
            if type(transcript) is not str or not transcript.strip():
                raise ActionError("no_words")
            transcript_messages(transcript)  # Same bounds/Unicode contract, no grammar.
            with self._condition:
                self._check_stopped()
                self._transcript, self._phase = transcript, "review"
        finally:
            captured = pcm = transcript = None

    def _wait(self, kind: str, *, action: Any = None, expires_at: int | None = None) -> bool:
        with self._condition:
            self._check_stopped()
            self._pending = {"kind": kind, "id": secrets.token_urlsafe(24),
                             "expires_at": expires_at or int(time.time()) + 60,
                             "action": deepcopy(action)}
            if action is not None and action.get("tool") == "run_python_script":
                self._pending["source_sha256"] = hashlib.sha256(action["arguments"]["code"].encode("utf-8")).hexdigest()
            self._decision = None
            self._phase = "plan" if kind == "plan" else "approval"
            self._condition.notify_all()
            # Wall expiry is the broker's bound; monotonic also bounds clock changes.
            deadline = time.monotonic() + min(60, max(0, self._pending["expires_at"] - time.time()))
            try:
                while self._decision is None:
                    self._check_stopped()
                    if time.monotonic() >= deadline or time.time() >= self._pending["expires_at"]:
                        raise ActionError("expired")
                    self._condition.wait(timeout=min(.1, max(.001, deadline - time.monotonic())))
                self._check_stopped()
                return self._decision
            finally:
                self._pending = None
                self._decision = None

    def _confirm_plan(self, proposal: ActionPlan) -> bool:
        return self._wait("plan")

    def _confirm(self, action: dict[str, Any], expires_at: int) -> bool:
        # Exact action comes from the broker, including freshly captured run source.
        return self._wait("action", action=action, expires_at=expires_at)

    def _on_step(self, index: int, total: int, call: ToolCall) -> None:
        with self._condition:
            self._check_stopped()
            self._step, self._phase = index, "executing"

    def _on_result(self, index: int, outcome: Execution) -> None:
        with self._condition:
            # Called only after _one_action's audit end. Never infer results from prose.
            action = deepcopy(self._steps[index - 1])
            for name in ("code", "content"):
                action["arguments"].pop(name, None)
            self._results.append({"index": index, "action": action, "status": outcome.status,
                                  "code": outcome.code, "details": deepcopy(outcome.details),
                                  "effect_completed": outcome.effect_completed})

    def _plan_execute(self) -> None:
        proposal = messages = None
        try:
            with self._condition:
                messages = transcript_messages(self._transcript, corrections=list(self._corrections))
            proposal = self._planner.generate(messages, TOOLS)
            messages = None
            self._check_stopped()
            if isinstance(proposal, str):
                # The planner's validated response-only explanation is display data.
                with self._condition:
                    self._explanation = proposal
                    self._phase = "clarification"
                return
            proposal = validate_proposal(proposal)
            steps = proposal.steps if type(proposal) is ActionPlan else (proposal,)
            with self._condition:
                self._steps = [_display_action(call) for call in steps]
            outcome = self._executor(self.root, proposal, stop_event=self._stop,
                                     confirm=self._confirm, confirm_plan=self._confirm_plan,
                                     on_step=self._on_step, on_result=self._on_result,
                                     stdout=_DiscardOutput())
            with self._condition:
                if not isinstance(outcome, Execution) or outcome.status not in {"completed", "declined", "cancelled", "failed", "unknown"}:
                    self._blocked = True
                    self._failure("outcome_unknown")
                    self._stop.set()
                    return
                self._phase = outcome.status if outcome.status in _TERMINAL else "unknown"
                if outcome.code:
                    self._failure(outcome.code, preserve_phase=True)
                # effect_completed is cumulative across a plan: a known failure
                # after an earlier success is not an uncertain current effect.
                if outcome.status == "unknown" or outcome.code in {"audit_unavailable", "cleanup_failed", "audit_or_result_failed", "broker_rejected", "invalid_confirmation"} or outcome.effect_completed and outcome.status == "failed" and outcome.code == "action_failed":
                    self._blocked = True
                    self._stop.set()
                elif outcome.status in {"failed", "cancelled"}:
                    self._stop.set()
        finally:
            messages = proposal = None

    def _failure(self, code: Any, help_text: str | None = None, *, preserve_phase: bool = False) -> None:
        safe = code if type(code) is str and code in _KNOWN_FAILURES else "runtime_error"
        if not (safe == "cancelled" and self._error and self._error["code"] == "request_expired"):
            self._error = {"code": safe, "help": help_text}
        if not preserve_phase:
            self._phase = "cancelled" if self._stop.is_set() or safe == "cancelled" else "failed"
        if safe in {"cleanup_failed", "audit_unavailable", "audit_or_result_failed", "outcome_unknown", "broker_rejected", "invalid_confirmation"}:
            self._blocked = True
            self._phase = "unknown"

    def _request_stop(self, *, release: bool = False) -> None:
        self._release = self._release or release
        self._stop.set()
        self._transcript = ""
        self._corrections.clear()
        self._pending = None
        self._decision = None
        if self._phase not in {"unknown", "failed", "blocked"}:
            self._phase = "cancelling"
        self._condition.notify_all()
        if self._worker is None:
            self._launch(lambda: None)

    def _close_resources(self) -> bool:
        clean = True
        for resource in (self._speech, self._planner):
            if resource is not None:
                try:
                    resource.close()
                    process = getattr(resource, "process", None)
                    if process is not None and process.is_alive():
                        clean = False
                except BaseException:
                    clean = False
        if clean:
            self._speech = self._planner = self._microphone = None
        return clean

    def _watch(self) -> None:
        while not self._shutdown.wait(.25):
            with self._condition:
                if self._owner is None:
                    continue
                now = time.monotonic()
                if now - self._heartbeat > self._heartbeat_seconds:
                    self._request_stop(release=True)
                elif not self._stop.is_set() and self._request_started and now - self._request_started > self._request_seconds:
                    self._error = {"code": "request_expired", "help": None}
                    self._request_stop()

    def close(self) -> None:
        self._shutdown.set()
        with self._condition:
            self._consented_client = None
            self._request_stop(release=True)
            worker = self._worker
        if worker is not None:
            worker.join(timeout=15)
            if worker.is_alive():
                raise SessionError("unavailable", 503)
        self._watcher.join(timeout=2)
