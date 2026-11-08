"""Single-request Groq native-tool planner for the existing closed file tools.

This module sends only a fresh transcript plus optional same-recording review
notes under the Groq-only contract. It does not execute tools or reinterpret prose.
"""

from __future__ import annotations

from copy import deepcopy
import http.client
import json
from pathlib import Path
import re
import socket
import ssl
import threading
import time
from types import MappingProxyType
from typing import Any

from .groq_contract import EXPLANATION_TOOL, MAX_EXPLANATION_CHARS, MAX_CORRECTION_CONTEXT_BYTES
from .credentials import read_api_key
from .contract import TOOLS, ActionArgumentError, parse_native_call, transcript_messages
from .action_types import ActionError, ToolCall


MODEL_ID = "qwen/qwen3.8-27b"
API_HOST = "api.groq.com"
API_PATH = "/openai/v1/chat/completions"
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 64 * 1024

# These are deliberately the complete set of
# optional generation controls used by this transport.
REQUEST_SETTINGS = MappingProxyType({
    "tool_choice": "required",
    "parallel_tool_calls": False,
    "reasoning_effort": "none",
    "reasoning_format": "hidden",
    "temperature": 0,
    "max_completion_tokens": 1024,
    "stream": False,
})

_EXPECTED_TOOLS = deepcopy(TOOLS)
_ERROR_CODES = frozenset({
    "credential_unavailable", "invalid_request", "not_started", "closed", "cancelled",
    "timeout", "unauthorized", "rate_limited", "unavailable", "request_rejected", "invalid_output",
})
_REASONING_MARKERS = ("<think", "</think", "<analysis", "<|analysis")
_RETRY_AFTER = re.compile(r"(?:0|[1-9][0-9]{0,4})(?:\.[0-9]{1,3})?\Z")
_OUTPUT_DETAILS = MappingProxyType({
    "response_structure": "The response did not match the required native-call structure.",
    "response_truncated": "The response hit its completion limit; it was not repaired or retried.",
    "tool_call_count": "Exactly one native action, plan or explanation call is required.",
    "tool_arguments_json": "Native tool arguments were not a valid closed JSON object.",
    "tool_arguments": "The proposed tool or its argument names/types/values failed validation.",
    "python_filename": "The Python filename or workspace-relative path failed validation.",
    "python_source": "Python source failed syntax, text or size checks; no source was executed.",
    "python_imports": "Python source used unsupported imports; only standard-library imports are allowed.",
})
_REQUEST_PHASES = frozenset({
    "setup", "connecting", "sending", "awaiting_headers", "response_headers",
    "reading_body", "response_received", "validating", "complete",
})


class _RequestTiming:
    """Per-request numeric milestones only; late workers cannot change a frozen result."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._data: dict[str, Any] = {"phase": "setup", "http_status": None,
                                     "headers_seconds": None, "body_seconds": None}
        self._frozen: dict[str, Any] | None = None

    def mark(self, phase: str, *, status: Any = None) -> None:
        with self._lock:
            if self._frozen is not None or phase not in _REQUEST_PHASES:
                return
            self._data["phase"] = phase
            if type(status) is int and 100 <= status <= 599:
                self._data["http_status"] = status
            if phase == "response_headers":
                self._data["headers_seconds"] = round(max(0, time.monotonic() - self._started), 3)
            elif phase == "response_received":
                self._data["body_seconds"] = round(max(0, time.monotonic() - self._started), 3)

    def finish(self) -> dict[str, Any]:
        with self._lock:
            if self._frozen is None:
                self._frozen = {**self._data, "elapsed_seconds": round(max(0, time.monotonic() - self._started), 3)}
            return dict(self._frozen)


class GroqError(ActionError):
    """Closed, sanitized Groq failure; provider text is never retained."""

    def __init__(self, code: str, *, retry_after: int | float | None = None,
                 output_detail: str | None = None) -> None:
        if code not in _ERROR_CODES:
            code = "unavailable"
        self.retry_after = retry_after if code == "rate_limited" else None
        self.output_detail = (output_detail if code == "invalid_output" and type(output_detail) is str
                              and output_detail in _OUTPUT_DETAILS else None)
        self.request_timing: dict[str, Any] | None = None
        super().__init__(code)


def _error(code: str, *, retry_after: int | float | None = None, output_detail: str | None = None) -> GroqError:
    return GroqError(code, retry_after=retry_after, output_detail=output_detail)


def output_error_help(error: ActionError) -> str | None:
    """Local terminal guidance from closed constants, not provider/source text."""
    if not isinstance(error, GroqError):
        return None
    detail = error.output_detail
    message = _OUTPUT_DETAILS.get(detail) if error.code == "invalid_output" and type(detail) is str else None
    parts = ["Planner rejection detail: " + detail + ". " + message] if message is not None else []
    data = error.request_timing
    keys = {"phase", "http_status", "headers_seconds", "body_seconds", "elapsed_seconds"}
    if (type(data) is dict and set(data) == keys and type(data["phase"]) is str and data["phase"] in _REQUEST_PHASES
            and (data["http_status"] is None or type(data["http_status"]) is int and 100 <= data["http_status"] <= 599)
            and type(data["elapsed_seconds"]) in (int, float) and 0 <= data["elapsed_seconds"] <= 86400
            and all(value is None or type(value) in (int, float) and 0 <= value <= 86400
                    for value in (data["headers_seconds"], data["body_seconds"]))):
        def seconds(value: Any) -> str:
            return "not observed" if value is None else f"{value:.3f}s"
        parts.append("Planner timing (last observed phase): " + data["phase"] + "; elapsed=" + seconds(data["elapsed_seconds"])
                     + "; HTTP=" + (str(data["http_status"]) if data["http_status"] is not None else "not observed")
                     + "; headers=" + seconds(data["headers_seconds"]) + "; body=" + seconds(data["body_seconds"]))
    return "\n".join(parts) or None


def _abort_reason(planner: "GroqPlanner") -> str | None:
    with planner._state_lock:
        if planner._closed:
            return "closed"
    try:
        if planner._stop_event.is_set():
            return "cancelled"
    except Exception:
        return "cancelled"
    return None


def _same_schema(actual: Any, expected: Any) -> bool:
    if type(expected) is dict:
        return (type(actual) is dict and set(actual) == set(expected)
                and all(_same_schema(actual[key], value) for key, value in expected.items()))
    if type(expected) is list:
        return type(actual) is list and len(actual) == len(expected) and all(
            _same_schema(value, expected[index]) for index, value in enumerate(actual)
        )
    return type(actual) is type(expected) and actual == expected


def _validate_request(messages: Any, tools: Any, *, advanced: bool = True) -> str:
    if advanced is not True:
        raise _error("invalid_request")
    if type(messages) is not list or len(messages) not in (2, 3):
        raise _error("invalid_request")
    if any(type(message) is not dict or set(message) != {"role", "content"}
           or type(message.get("role")) is not str or type(message.get("content")) is not str
           for message in messages):
        raise _error("invalid_request")
    transcript = messages[1]["content"]
    invalid = False
    expected_messages = None
    try:
        corrections = None
        if len(messages) == 3:
            context = messages[2]["content"]
            if len(context.encode("utf-8")) > MAX_CORRECTION_CONTEXT_BYTES:
                raise ValueError("invalid_context")
            data = json.loads(context, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_nonfinite)
            if type(data) is not dict or set(data) != {"review_corrections"} or type(data["review_corrections"]) is not list:
                raise ValueError("invalid_context")
            corrections = data["review_corrections"]
        expected_messages = transcript_messages(transcript, corrections=corrections)
    except Exception:
        invalid = True
    expected_tools = _EXPECTED_TOOLS
    if invalid or messages != expected_messages or not _same_schema(tools, expected_tools):
        raise _error("invalid_request")
    return transcript


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _parse_arguments(value: Any) -> dict[str, Any]:
    if type(value) is not str:
        raise _error("invalid_output")
    invalid = False
    decoded = None
    try:
        if len(value.encode("utf-8")) > MAX_RESPONSE_BYTES:
            invalid = True
        else:
            decoded = json.loads(value, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_nonfinite)
    except Exception:
        invalid = True
    if invalid:
        raise _error("invalid_output")
    if type(decoded) is not dict:
        raise _error("invalid_output")
    return decoded


def _has_reasoning_marker(text: str) -> bool:
    folded = text.casefold()
    return any(marker in folded for marker in _REASONING_MARKERS)


def _parse_response(raw: bytes, *, advanced: bool = True) -> Any:
    if advanced is not True:
        raise _error("invalid_request")
    def reject(detail: str = "response_structure") -> GroqError:
        return _error("invalid_output", output_detail=detail)

    if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
        raise reject()
    invalid = False
    payload = None
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs,
                             parse_constant=_reject_nonfinite)
    except Exception:
        invalid = True
    if invalid or type(payload) is not dict or payload.get("model") != MODEL_ID:
        raise reject()
    choices = payload.get("choices")
    if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
        raise reject()
    choice = choices[0]
    message = choice.get("message")
    finish_reason = choice.get("finish_reason")
    if type(message) is not dict or message.get("role") != "assistant" or type(finish_reason) is not str:
        raise reject()
    if finish_reason == "length":
        raise reject("response_truncated")
    if any(name in message for name in ("reasoning", "reasoning_content", "analysis")):
        raise reject()

    content = message.get("content")
    invalid_content = False
    if content is not None:
        if type(content) is not str:
            raise reject()
        try:
            if len(content.encode("utf-8")) > MAX_RESPONSE_BYTES:
                invalid_content = True
        except UnicodeError:
            invalid_content = True
    if invalid_content:
        raise reject()
    if "function_call" in message or message.get("refusal") not in (None, ""):
        raise reject()

    tool_calls = message.get("tool_calls", [])
    if type(tool_calls) is not list:
        raise reject()
    if tool_calls:
        if len(tool_calls) != 1:
            raise reject("tool_call_count")
        if finish_reason != "tool_calls":
            raise reject()
        call_value = tool_calls[0]
        if type(call_value) is not dict or set(call_value) != {"id", "type", "function"}:
            raise reject()
        if (type(call_value["id"]) is not str or not 1 <= len(call_value["id"]) <= 256
                or call_value["type"] != "function" or type(call_value["function"]) is not dict
                or set(call_value["function"]) != {"name", "arguments"}):
            raise reject()
        invalid_id = False
        try:
            call_value["id"].encode("ascii")
        except UnicodeError:
            invalid_id = True
        if invalid_id:
            raise reject()
        name = call_value["function"]["name"]
        if type(name) is not str:
            raise reject()
        arguments = None
        try:
            arguments = _parse_arguments(call_value["function"]["arguments"])
        except GroqError:
            pass
        if arguments is None:
            raise reject("tool_arguments_json")
        if name == EXPLANATION_TOOL:
            reason = arguments.get("reason")
            if (set(arguments) != {"reason"} or type(reason) is not str
                    or not reason.strip() or len(reason) > MAX_EXPLANATION_CHARS
                    or _has_reasoning_marker(reason)):
                raise reject("tool_arguments")
            invalid_reason = False
            try:
                reason.encode("utf-8")
            except UnicodeError:
                invalid_reason = True
            if invalid_reason:
                raise reject("tool_arguments")
            # This response-only function never reaches the action broker.
            # Tool-like text inside the explanation remains inert display data.
            return reason.strip()
        call = ToolCall(name, arguments)
        invalid_arguments = False
        argument_detail = "tool_arguments"
        try:
            call = parse_native_call(name, arguments)
        except Exception as exc:
            invalid_arguments = True
            if isinstance(exc, ActionArgumentError):
                argument_detail = exc.detail
        if invalid_arguments:
            raise reject(argument_detail)
        return call

    # Required tool use is also enforced locally: only explain_no_action may
    # produce display text. No prose fallback, repair, or automatic retry.
    raise reject("tool_call_count")


def _retry_after(value: str | None) -> int | float | None:
    if not isinstance(value, str) or len(value) > 16 or not _RETRY_AFTER.fullmatch(value):
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if not 0 <= seconds <= 3600:
        return None
    return int(seconds) if seconds.is_integer() else seconds


def _close_connection(connection: http.client.HTTPSConnection) -> None:
    try:
        sock = connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    except Exception:
        pass
    try:
        connection.close()
    except Exception:
        pass


class GroqPlanner:
    """A fixed-host, fixed-model native-tool client; no action execution."""

    def __init__(self, root: Path, *, stop_event: Any = None, advanced: bool = True) -> None:
        if advanced is not True:
            raise _error("invalid_request")
        self.root = root
        self.advanced = True
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._api_key: str | None = None
        self._closed = False
        self._state_lock = threading.RLock()
        self._generation_lock = threading.Lock()
        self._connection: http.client.HTTPSConnection | None = None
        self._request_worker: threading.Thread | None = None
        self._last_request_timing: dict[str, Any] | None = None

    @property
    def request_timing(self) -> dict[str, Any] | None:
        """Newest generate attempt's timing; None before advanced transport setup. Never payloads."""
        with self._state_lock:
            return dict(self._last_request_timing) if self._last_request_timing is not None else None

    def start(self) -> None:
        """Check and cache the local key only; this method never contacts Groq."""
        with self._state_lock:
            if self._closed:
                raise _error("closed")
            if self._api_key is not None:
                return
            if _abort_reason(self) == "cancelled":
                raise _error("cancelled")
            try:
                key = read_api_key(self.root)
            except Exception:
                key = None
            if key is None:
                raise _error("credential_unavailable")
            self._api_key = key

    def generate(self, messages: Any, tools: Any) -> ToolCall | str:
        """Generate one action proposal or a validated non-action explanation."""
        with self._generation_lock:
            with self._state_lock:
                if self._request_worker is not None and self._request_worker.is_alive():
                    raise _error("unavailable")
                self._last_request_timing = None
            if _abort_reason(self) is not None:
                reason = _abort_reason(self) or "cancelled"
                raise _error(reason)
            _validate_request(messages, tools, advanced=self.advanced)
            if self._api_key is None:
                self.start()
            with self._state_lock:
                if self._closed:
                    raise _error("closed")
                key = self._api_key
            if key is None:
                raise _error("credential_unavailable")
            reason = _abort_reason(self)
            if reason is not None:
                raise _error(reason)

            expected_tools = _EXPECTED_TOOLS
            request = {
                "model": MODEL_ID,
                "messages": deepcopy(messages),
                "tools": [{"type": "function", "function": deepcopy(schema)} for schema in expected_tools],
                **dict(REQUEST_SETTINGS),
            }
            body = b""
            invalid_request = False
            try:
                body = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            except Exception:
                invalid_request = True
            if invalid_request:
                raise _error("invalid_request")

            timing = _RequestTiming()

            def record_timing(error: GroqError | None = None) -> None:
                if timing is not None:
                    snapshot = timing.finish()
                    with self._state_lock:
                        self._last_request_timing = snapshot
                    if error is not None:
                        error.request_timing = dict(snapshot)

            def fail(code: str, *, retry_after: int | float | None = None) -> GroqError:
                error = _error(code, retry_after=retry_after)
                record_timing(error)
                return error

            connection = None
            try:
                connection = http.client.HTTPSConnection(
                    API_HOST, timeout=REQUEST_TIMEOUT_SECONDS, context=ssl.create_default_context()
                )
            except Exception:
                connection = None
            if connection is None:
                raise fail("unavailable")
            with self._state_lock:
                if self._closed:
                    _close_connection(connection)
                    raise fail("closed")
                self._connection = connection
            reason = _abort_reason(self)
            if reason is not None:
                with self._state_lock:
                    if self._connection is connection:
                        self._connection = None
                _close_connection(connection)
                raise fail(reason)

            outcome: dict[str, Any] = {}
            finished = threading.Event()
            abort = threading.Event()
            original_connect = getattr(connection, "connect", None)
            if callable(original_connect):
                def guarded_connect() -> None:
                    if abort.is_set() or _abort_reason(self) is not None:
                        raise socket.timeout()
                    if timing is not None:
                        timing.mark("connecting")
                    original_connect()
                    if abort.is_set() or _abort_reason(self) is not None:
                        _close_connection(connection)
                        raise socket.timeout()
                    if timing is not None:
                        timing.mark("sending")

                connection.connect = guarded_connect

            def request_once() -> None:
                response = None
                try:
                    if abort.is_set() or _abort_reason(self) is not None:
                        return
                    if timing is not None:
                        timing.mark("sending")
                    connection.request(
                        "POST", API_PATH, body=body,
                        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                 "Accept": "application/json", "Connection": "close"},
                    )
                    if timing is not None:
                        timing.mark("awaiting_headers")
                    response = connection.getresponse()
                    status = response.status
                    if timing is not None:
                        timing.mark("response_headers", status=status)
                    retry = _retry_after(response.getheader("Retry-After")) if status == 429 else None
                    if 200 <= status < 300:
                        if timing is not None:
                            timing.mark("reading_body")
                        raw = response.read(MAX_RESPONSE_BYTES + 1)
                        if timing is not None:
                            timing.mark("response_received")
                        outcome.update(status=status, body=raw)
                    else:
                        outcome.update(status=status, retry_after=retry)
                except socket.timeout:
                    outcome["network_error"] = "timeout"
                except BaseException as exc:
                    # Never keep exception strings, response bodies, or headers.
                    outcome["network_error"] = "unavailable"
                finally:
                    if response is not None:
                        try:
                            response.close()
                        except Exception:
                            pass
                    _close_connection(connection)
                    finished.set()

            worker = threading.Thread(target=request_once, name="groq-request", daemon=True)
            started_at = time.monotonic()
            request_failed = False
            try:
                with self._state_lock:
                    if self._closed:
                        raise fail("closed")
                    self._request_worker = worker
                    worker.start()
                while not finished.wait(min(0.05, max(0.0, REQUEST_TIMEOUT_SECONDS - (time.monotonic() - started_at)))):
                    reason = _abort_reason(self)
                    if reason is not None:
                        abort.set()
                        error = fail(reason)
                        _close_connection(connection)
                        raise error
                    if time.monotonic() - started_at >= REQUEST_TIMEOUT_SECONDS:
                        abort.set()
                        error = fail("timeout")
                        _close_connection(connection)
                        raise error
                reason = _abort_reason(self)
                if reason is not None:
                    raise fail(reason)
            except GroqError:
                raise
            except KeyboardInterrupt:
                abort.set()
                record_timing()
                _close_connection(connection)
                raise
            except Exception:
                abort.set()
                _close_connection(connection)
                request_failed = True
            finally:
                if not finished.is_set():
                    abort.set()
                    _close_connection(connection)
                with self._state_lock:
                    if self._connection is connection:
                        self._connection = None
                    if not worker.is_alive() and self._request_worker is worker:
                        self._request_worker = None

            if request_failed:
                raise fail("unavailable")

            if "network_error" in outcome:
                raise fail(outcome["network_error"])
            status = outcome.get("status")
            if status in (401, 403):
                raise fail("unauthorized")
            if status == 429:
                raise fail("rate_limited", retry_after=outcome.get("retry_after"))
            if status == 404 or type(status) is int and status >= 500 or type(status) is int and 300 <= status < 400:
                raise fail("unavailable")
            if type(status) is not int or not 200 <= status < 300:
                raise fail("request_rejected")
            if timing is not None:
                timing.mark("validating")
            try:
                proposal = _parse_response(outcome.get("body"), advanced=self.advanced)
            except GroqError as error:
                record_timing(error)
                raise
            if timing is not None:
                timing.mark("complete")
            record_timing()
            return proposal

    def plan(self, text: str, *, corrections: list[str] | None = None) -> ToolCall | str:
        """Make a fresh request with optional review notes, never earlier history."""
        invalid = False
        messages = None
        try:
            messages = transcript_messages(text, corrections=corrections)
        except Exception:
            invalid = True
        if invalid or messages is None:
            raise _error("invalid_request")
        return self.generate(messages, TOOLS)

    def close(self) -> None:
        """Disarm and confirm owned transport cleanup before resource reuse."""
        with self._state_lock:
            self._closed = True
            self._api_key = None
            connection = self._connection
            self._connection = None
            worker = self._request_worker
        if connection is not None:
            _close_connection(connection)
        if worker is not None:
            if worker is not threading.current_thread() and worker.ident is not None:
                worker.join(timeout=3)
            if worker.is_alive():
                raise ActionError("cleanup_failed")
            with self._state_lock:
                if self._request_worker is worker:
                    self._request_worker = None
