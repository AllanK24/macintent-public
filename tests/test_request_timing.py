"""Small mocked-transport diagnostic check; no real API, code execution or audio."""

import json
import socket

import pytest

from macintent import planner as groq
from test_planner import FakeHTTPSConnection, FakeResponse, install_key, native_response


def planner_for(tmp_path, monkeypatch, *, advanced=True, failure=None, status=200):
    root = tmp_path / "synthetic"
    install_key(root)
    connections = []

    class Response(FakeResponse):
        def read(self, limit):
            if failure == "reading_body":
                raise socket.timeout("sensitive-marker")
            return super().read(limit)

    class Connection(FakeHTTPSConnection):
        def connect(self):
            if failure == "connecting":
                raise socket.timeout("sensitive-marker")

        def request(self, *args, **kwargs):
            self.connect()
            super().request(*args, **kwargs)
            if failure == "sending":
                raise socket.timeout("sensitive-marker")

        def getresponse(self):
            if failure == "awaiting_headers":
                raise socket.timeout("sensitive-marker")
            if failure == "wall_timeout":
                self.release.wait(2)
            if failure == "invalid_output":
                return Response(b"sensitive-marker", status=status)
            return Response(native_response(), status=status)

    def connect(host, *, timeout, context):
        connection = Connection(host, timeout=timeout, context=context)
        connections.append(connection)
        return connection

    monkeypatch.setattr(groq.http.client, "HTTPSConnection", connect)
    return groq.GroqPlanner(root, advanced=advanced), connections


def test_success_is_numeric_and_next_preflight_clears_metadata(tmp_path, monkeypatch):
    planner, connections = planner_for(tmp_path, monkeypatch)
    try:
        assert planner.request_timing is None
        assert planner.plan("List the workspace.").name == "list_files"
        data = planner.request_timing
        assert set(data) == {"phase", "http_status", "headers_seconds", "body_seconds", "elapsed_seconds"}
        assert data["phase"] == "complete" and data["http_status"] == 200
        assert 0 <= data["headers_seconds"] <= data["body_seconds"] <= data["elapsed_seconds"]
        data["phase"] = "changed"
        assert planner.request_timing["phase"] == "complete"
        with pytest.raises(groq.GroqError) as error:
            planner.generate([], [])
        assert error.value.request_timing is None and planner.request_timing is None
        assert len(connections) == 1 and len(connections[0].requests) == 1
    finally:
        planner.close()


@pytest.mark.parametrize("phase", ["connecting", "sending", "awaiting_headers", "reading_body"])
def test_transport_timeout_distinguishes_last_phase_without_payloads_or_retry(tmp_path, monkeypatch, phase):
    planner, connections = planner_for(tmp_path, monkeypatch, failure=phase)
    try:
        with pytest.raises(groq.GroqError) as error:
            planner.plan("Create one synthetic compute script.")
        assert error.value.code == "timeout" and error.value.__context__ is None
        data = error.value.request_timing
        assert data == planner.request_timing and data["phase"] == phase
        assert data["http_status"] == (200 if phase == "reading_body" else None)
        assert (data["headers_seconds"] is not None) is (phase == "reading_body")
        assert data["body_seconds"] is None
        assert "sensitive-marker" not in json.dumps(vars(error.value)) + groq.output_error_help(error.value)
        assert len(connections) == 1 and connections[0].closed
        assert len(connections[0].requests) == (0 if phase == "connecting" else 1)
    finally:
        planner.close()


def test_wall_timeout_freezes_before_transport_cleanup_unblocks_late_response(tmp_path, monkeypatch):
    monkeypatch.setattr(groq, "REQUEST_TIMEOUT_SECONDS", 0.05)
    planner, connections = planner_for(tmp_path, monkeypatch, failure="wall_timeout")
    try:
        with pytest.raises(groq.GroqError) as error:
            planner.plan("Create one synthetic compute script.")
        assert error.value.code == "timeout" and error.value.request_timing["phase"] == "awaiting_headers"
        assert error.value.request_timing["http_status"] is None
        assert connections[0].closed and len(connections[0].requests) == 1
    finally:
        planner.close()


@pytest.mark.parametrize("failure,status,code,phase", [
    ("invalid_output", 200, "invalid_output", "validating"),
    (None, 429, "rate_limited", "response_headers"),
])
def test_http_or_validation_failures_keep_only_status_and_milestones(tmp_path, monkeypatch, failure, status, code, phase):
    planner, connections = planner_for(tmp_path, monkeypatch, failure=failure, status=status)
    try:
        with pytest.raises(groq.GroqError) as error:
            planner.plan("Create one synthetic compute script.")
        data = error.value.request_timing
        assert error.value.code == code and error.value.__context__ is None
        assert data["phase"] == phase and data["http_status"] == status
        assert (data["body_seconds"] is None) is (status == 429)
        assert "sensitive-marker" not in json.dumps(vars(error.value)) + groq.output_error_help(error.value)
        assert len(connections) == 1 and len(connections[0].requests) == 1
        data["phase"] = "changed"
        assert planner.request_timing["phase"] == phase
    finally:
        planner.close()


def test_tracker_rejects_unknown_phase_status_and_is_frozen_after_finish():
    timing = groq._RequestTiming()
    timing.mark("sensitive-marker")
    timing.mark("response_headers", status="sensitive-marker")
    first = timing.finish()
    assert first["phase"] == "response_headers" and first["http_status"] is None
    timing.mark("complete", status=200)
    assert timing.finish() == first
    first["phase"] = "changed"
    assert timing.finish()["phase"] == "response_headers"


@pytest.mark.parametrize("change", [
    {"phase": "sensitive-marker"}, {"http_status": "sensitive-marker"},
    {"headers_seconds": float("nan")}, {"body_seconds": float("inf")},
    {"elapsed_seconds": True}, {"elapsed_seconds": -1}, {"extra": "sensitive-marker"},
])
def test_terminal_helper_rejects_nonclosed_metadata(change):
    error = groq.GroqError("timeout")
    error.request_timing = {"phase": "awaiting_headers", "http_status": None, "headers_seconds": None,
                            "body_seconds": None, "elapsed_seconds": 1.0, **change}
    assert groq.output_error_help(error) is None
