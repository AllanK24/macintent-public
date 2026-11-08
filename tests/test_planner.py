"""Synthetic native-call and mocked HTTPS checks; no cloud request or actions."""

from copy import deepcopy
import json
import threading

import pytest

from macintent import contract, planner as groq
from macintent.action_types import ActionError, ToolCall
from macintent.credentials import import_api_key

SYNTHETIC_KEY = "gsk_0123456789abcdefghijklmnopqrstuv"


def install_key(root):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "api.txt"
    source.write_bytes(SYNTHETIC_KEY.encode("ascii") + b"\n")
    source.chmod(0o600)
    import_api_key(root)


def native_response(*, name="list_files", arguments='{"directory":"."}', content=None,
                    finish="tool_calls", model=groq.MODEL_ID, calls=None, choices=None, extra_message=None):
    if calls is None:
        calls = [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}]
    message = {"role": "assistant", "content": content, "tool_calls": calls}
    if extra_message:
        message.update(extra_message)
    if choices is None:
        choices = [{"index": 0, "message": message, "finish_reason": finish}]
    return {"id": "chatcmpl_synthetic", "object": "chat.completion", "model": model, "choices": choices}


class FakeResponse:
    def __init__(self, payload=None, *, status=200, headers=None):
        self.status = status
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.headers = headers or {}

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, limit):
        return self.payload[:limit]

    def close(self):
        pass


class FakeHTTPSConnection:
    def __init__(self, _host, *, timeout, context, response=None, status=200, headers=None,
                 request_error=None, wait=False):
        self.timeout, self.context = timeout, context
        self.response, self.status, self.headers = response, status, headers
        self.request_error = request_error
        self.requests = []
        self.sock = None
        self.closed = False
        self.started = threading.Event()
        self.release = threading.Event()
        self.wait = wait

    def request(self, method, path, *, body, headers):
        self.requests.append((method, path, body, headers))
        self.started.set()
        if self.wait:
            self.release.wait(2)
        if self.request_error is not None:
            raise self.request_error

    def getresponse(self):
        if self.response is not None:
            return self.response
        return FakeResponse(native_response() if self.status == 200 else None, status=self.status, headers=self.headers)

    def close(self):
        self.closed = True
        self.release.set()


def setup_planner(tmp_path, monkeypatch, *, response=None, status=200, wait=False):
    root = tmp_path / "project"
    install_key(root)
    connections = []
    def connect(host, *, timeout, context):
        assert host == "api.groq.com"
        connection = FakeHTTPSConnection(host, timeout=timeout, context=context, response=response, status=status, wait=wait)
        connections.append(connection)
        return connection
    monkeypatch.setattr(groq.http.client, "HTTPSConnection", connect)
    return groq.GroqPlanner(root), connections


def test_fixed_native_payload_and_explicit_original_plus_corrections(tmp_path, monkeypatch):
    planner, connections = setup_planner(tmp_path, monkeypatch)
    try:
        messages = contract.transcript_messages("A synthetic request.", corrections=["Use the current workspace."])
        assert planner.generate(messages, deepcopy(contract.TOOLS)) == ToolCall("list_files", {"directory": "."})
        assert len(connections) == 1 and len(connections[0].requests) == 1
        method, path, body, headers = connections[0].requests[0]
        payload = json.loads(body)
        assert (method, path) == ("POST", groq.API_PATH)
        assert payload["model"] == groq.MODEL_ID and payload["messages"] == messages
        assert payload["tools"] == [{"type": "function", "function": schema} for schema in contract.TOOLS]
        assert {name: payload[name] for name in groq.REQUEST_SETTINGS} == dict(groq.REQUEST_SETTINGS)
        assert SYNTHETIC_KEY not in body.decode() and headers["Authorization"] == "Bearer " + SYNTHETIC_KEY
        assert connections[0].context.check_hostname and connections[0].context.verify_mode
    finally:
        planner.close()


@pytest.mark.parametrize("payload", [
    native_response(model="wrong/model"), native_response(choices=[]), native_response(finish="length"),
    native_response(calls=[]), native_response(arguments='{"directory":"../outside"}'),
    native_response(arguments='{"directory":".","approved":true}'),
    native_response(arguments='{"directory":".","directory":"other"}'),
    native_response(extra_message={"reasoning_content": "PRIVATE synthetic marker"}),
])
def test_malformed_or_unsafe_output_is_rejected_without_raw_content(payload):
    with pytest.raises(groq.GroqError, match="invalid_output") as error:
        groq._parse_response(json.dumps(payload).encode())
    assert "PRIVATE" not in str(error.value)


def test_changed_schema_or_bad_notes_are_rejected_before_http(tmp_path, monkeypatch):
    planner, connections = setup_planner(tmp_path, monkeypatch)
    try:
        messages = contract.transcript_messages("A synthetic request.")
        changed = deepcopy(contract.TOOLS)
        changed[0]["description"] = "more authority"
        with pytest.raises(groq.GroqError, match="invalid_request"):
            planner.generate(messages, changed)
        with pytest.raises(ActionError, match="invalid_command"):
            contract.transcript_messages("A synthetic request.", corrections=["x"] * 9)
        assert connections == []
    finally:
        planner.close()


def test_explanation_is_inert_and_plans_are_closed():
    payload = native_response(name="explain_no_action", arguments=json.dumps({"reason": "Which filename?"}))
    assert groq._parse_response(json.dumps(payload).encode()) == "Which filename?"
    with pytest.raises(ActionError):
        contract.parse_native_call("plan_actions", {"steps": [{"tool": "plan_actions", "arguments": {}}] * 2})


def test_cancel_closes_owned_transport_without_retry(tmp_path, monkeypatch):
    planner, connections = setup_planner(tmp_path, monkeypatch, wait=True)
    def cancel_when_started():
        while not connections:
            threading.Event().wait(.005)
        assert connections[0].started.wait(1)
        planner._stop_event.set()
    cancelling = threading.Thread(target=cancel_when_started)
    cancelling.start()
    try:
        with pytest.raises(groq.GroqError, match="cancelled"):
            planner.plan("A synthetic request.")
        assert connections[0].closed and len(connections[0].requests) == 1
    finally:
        cancelling.join(2)
        planner.close()
    assert planner._request_worker is None
