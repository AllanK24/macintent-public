"""Focused loopback transport tests with a stub session; no live state or effects."""

from __future__ import annotations

from contextlib import contextmanager, suppress
import json
from http.client import HTTPConnection
import re
import socket
from threading import Thread
from pathlib import Path

import pytest

from macintent import server as intent_server
from macintent.groq_contract import MAX_CORRECTIONS, MAX_CORRECTION_TOTAL_CHARS
from macintent.planner import MODEL_ID
from macintent.action_types import MAX_COMMAND
from macintent.session import ERROR_CODES as MANAGER_ERROR_CODES, SessionError


HOST = "127.0.0.1"
CLIENT = "0123456789abcdef" * 2
TEST_ASSETS = {
    "/": (b"<!doctype html><title>MacIntent</title>", "text/html; charset=utf-8"),
    "/app.mjs": (b"export const ready = true;", "text/javascript; charset=utf-8"),
    "/live.css": (b"body { color: white; }", "text/css; charset=utf-8"),
    "/styles.css": (b".panel { display: block; }", "text/css; charset=utf-8"),
    "/scenarios.mjs": (b"export const scenarios = [];", "text/javascript; charset=utf-8"),
    "/stars.svg": (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
    "/mark.svg": (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
}
_SAME_ORIGIN = object()
_SERVER_TOKEN = object()
_DEFAULT_CLIENT = object()


class StubSession:
    def __init__(self) -> None:
        self.state = {"phase": "idle", "connected": True, "blocked": False}
        self.failure: BaseException | None = None
        self.snapshots: list[str] = []
        self.commands: list[tuple[str, str, object]] = []
        self.closed = 0

    def snapshot(self, client: str) -> dict[str, object]:
        self.snapshots.append(client)
        if self.failure is not None:
            raise self.failure
        return dict(self.state)  # type: ignore[arg-type]

    def command(self, client: str, operation: str, payload: object) -> dict[str, object]:
        self.commands.append((client, operation, payload))
        if self.failure is not None:
            raise self.failure
        return {**self.state, "operation": operation}

    def close(self) -> None:
        self.closed += 1


@contextmanager
def running_server(session: StubSession | None = None):
    session = StubSession() if session is None else session
    server = intent_server.MacIntentServer(0, session=session, assets=TEST_ASSETS)
    worker = Thread(
        target=lambda: server.serve_forever(poll_interval=0.01),
        daemon=True,
        name="test-macintent",
    )
    worker.start()
    try:
        yield server, session, server.server_port
    finally:
        server.shutdown()
        worker.join(timeout=2)
        server.server_close()


def lower_headers(response) -> dict[str, str]:
    return {name.lower(): value for name, value in response.getheaders()}


def request(port: int, method: str, path: str, *, headers=None, body=None):
    connection = HTTPConnection(HOST, port, timeout=3)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        return response.status, lower_headers(response), raw
    finally:
        connection.close()


def request_json(port: int, method: str, path: str, *, headers=None, body=None):
    status, response_headers, raw = request(port, method, path, headers=headers, body=body)
    payload = json.loads(raw.decode("utf-8")) if raw else None
    return status, response_headers, payload


def raw_request(server, method: str, path: str, header_lines, *, body: bytes = b""):
    connection = HTTPConnection(HOST, server.server_port, timeout=3)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in header_lines:
            connection.putheader(name, value)
        connection.endheaders()
        with suppress(BrokenPipeError, ConnectionResetError):
            if body:
                connection.send(body)
        response = connection.getresponse()
        raw = response.read()
        payload = json.loads(raw.decode("utf-8")) if raw else None
        return response.status, lower_headers(response), payload
    finally:
        connection.close()


def same_origin(server) -> str:
    return f"http://{HOST}:{server.server_port}"


def api_headers(server, *, origin=_SAME_ORIGIN, token=_SERVER_TOKEN, client=_DEFAULT_CLIENT):
    headers: dict[str, str] = {}
    if origin is _SAME_ORIGIN:
        origin = same_origin(server)
    if origin is not None:
        headers["Origin"] = origin
    if token is _SERVER_TOKEN:
        token = server.token
    if token is not None:
        headers["x-macintent-token"] = token
    if client is _DEFAULT_CLIENT:
        client = CLIENT
    if client is not None:
        headers["x-macintent-client"] = client
    return headers


def bootstrap(server):
    status, headers, payload = request_json(server.server_port, "GET", "/api/session")
    assert status == 200
    assert_security(headers)
    assert isinstance(payload, dict)
    return payload


def assert_security(headers: dict[str, str]) -> None:
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "img-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "media-src 'none'" in csp
    assert "worker-src 'none'" in csp
    assert "font-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "access-control-allow-origin" not in headers
    assert "python" not in headers["server"].lower()


def assert_error(result, status: int, code: str) -> None:
    actual_status, headers, payload = result
    assert actual_status == status
    assert payload == {"ok": False, "error": code}
    assert_security(headers)


def test_bootstrap_is_metadata_only_fixed_and_random():
    assert intent_server.HOST == "127.0.0.1"
    assert intent_server.DEFAULT_PORT == 8093
    session = StubSession()
    with running_server(session) as (server, _, _):
        first = bootstrap(server)
        second = bootstrap(server)
        assert set(first) == {
            "token", "client_id", "disclosure", "model", "max_command",
            "max_corrections", "max_correction_chars",
        }
        assert first["token"] == server.token
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first["token"])
        assert re.fullmatch(r"[0-9a-f]{32}", first["client_id"])
        assert re.fullmatch(r"[0-9a-f]{32}", second["client_id"])
        assert first["client_id"] != second["client_id"]
        assert second["token"] == first["token"]
        assert first["model"] == MODEL_ID
        assert first["max_command"] == MAX_COMMAND == 2048
        assert first["max_corrections"] == MAX_CORRECTIONS == 8
        assert first["max_correction_chars"] == MAX_CORRECTION_TOTAL_CHARS == 2048
        disclosure = first["disclosure"]
        assert isinstance(disclosure, str)
        assert "Groq" in disclosure and MODEL_ID in disclosure
        assert "existing file contents" in disclosure
        assert "execution output" in disclosure
        assert "workspace inventory or history" in disclosure
        assert "Audio" in disclosure and "locally" in disclosure
        assert "fresh exact approval" in disclosure
        assert "compute-only" in disclosure and "fails closed" in disclosure
        assert "Advanced" not in disclosure
        # Bootstrap never touches the session.
        assert session.snapshots == []
        assert session.commands == []
        assert session.closed == 0


def test_transport_error_allowlist_is_closed_and_includes_manager_codes():
    assert MANAGER_ERROR_CODES <= intent_server.ERROR_CODES
    assert intent_server.TRANSPORT_ERROR_CODES <= intent_server.ERROR_CODES
    for code in {
        "invalid_request", "invalid_state", "busy", "not_owner", "stale_request",
        "stale_approval", "not_connected", "approval_expired", "session_blocked",
        "unavailable", "invalid_host", "invalid_origin", "unauthorized", "invalid_json",
        "body_too_large", "not_found", "method_not_allowed",
    }:
        assert code in intent_server.ERROR_CODES


def test_state_requires_both_headers_and_reads_one_snapshot():
    session = StubSession()
    with running_server(session) as (server, _, port):
        assert_error(request_json(port, "GET", "/api/state"), 401, "unauthorized")
        assert_error(request_json(port, "GET", "/api/state", headers={"x-macintent-token": server.token}),
                     401, "unauthorized")
        assert_error(request_json(
            port, "GET", "/api/state", headers={"x-macintent-client": CLIENT}), 401, "unauthorized")
        assert_error(request_json(
            port, "GET", "/api/state", headers=api_headers(server, token="wrong")), 401, "unauthorized")
        assert_error(request_json(
            port, "GET", "/api/state", headers=api_headers(server, client="A" * 32)), 401, "unauthorized")
        assert_error(request_json(
            port, "GET", "/api/state", headers=api_headers(server, client="0" * 31)), 401, "unauthorized")

        status, headers, payload = request_json(
            port, "GET", "/api/state", headers=api_headers(server))
        assert status == 200
        assert payload == {"ok": True, "state": session.state}
        assert_security(headers)
        assert session.snapshots == [CLIENT]
        assert session.commands == []


def test_state_and_static_get_reject_cross_site_and_foreign_origins():
    with running_server() as (server, _, port):
        assert_error(request_json(
            port, "GET", "/api/state",
            headers=api_headers(server, origin=f"http://{HOST}:{port + 1}")), 403, "invalid_origin")
        assert_error(request_json(
            port, "GET", "/api/state", headers={
                **api_headers(server, origin=None),
                "Sec-Fetch-Site": "cross-site",
            }), 403, "invalid_origin")
        assert_error(request_json(
            port, "GET", "/", headers={"Sec-Fetch-Site": "cross-site"}), 403, "invalid_origin")
        assert_error(request_json(
            port, "GET", "/", headers={"Origin": "null"}), 403, "invalid_origin")


def test_post_origin_host_and_header_strictness():
    session = StubSession()
    with running_server(session) as (server, _, port):
        expected = same_origin(server)
        body = b"{}"
        base = {"Content-Type": "application/json"}

        assert_error(request_json(
            port, "POST", "/api/connect", headers={**api_headers(server, origin=None), **base},
            body=body), 403, "invalid_origin")
        assert_error(request_json(
            port, "POST", "/api/connect",
            headers={**api_headers(server, origin=f"https://{HOST}:{port}"), **base},
            body=body), 403, "invalid_origin")
        assert_error(request_json(
            port, "POST", "/api/connect",
            headers={**api_headers(server, origin=expected + "/"), **base},
            body=body), 403, "invalid_origin")
        assert_error(request_json(
            port, "POST", "/api/connect",
            headers={**api_headers(server, token=None), **base}, body=body), 401, "unauthorized")
        assert_error(request_json(
            port, "POST", "/api/connect",
            headers={**api_headers(server, client=None), **base}, body=body), 401, "unauthorized")
        assert_error(request_json(
            port, "POST", "/api/connect",
            headers={**api_headers(server), "Content-Type": "text/plain"}, body=body),
            400, "invalid_request")
        assert_error(request_json(
            port, "POST", "/api/connect",
            headers={**api_headers(server), "Content-Type": "application/json; charset=utf-8"},
            body=body), 400, "invalid_request")
        assert_error(request_json(
            port, "POST", "/api/connect",
            headers={**api_headers(server), **base, "Sec-Fetch-Site": "cross-site"}, body=body),
            403, "invalid_origin")

        # The same request with no Origin is rejected even when other headers look valid.
        assert_error(request_json(
            port, "POST", "/api/connect",
            headers={**api_headers(server, origin=None), **base}, body=body), 403, "invalid_origin")

        host = f"{HOST}:{port}"
        assert_error(raw_request(server, "GET", "/api/session", [
            ("Host", f"localhost:{port}"),
        ]), 403, "invalid_host")
        assert_error(raw_request(server, "GET", "/api/session", [
            ("Host", host), ("Host", host),
        ]), 403, "invalid_host")

        duplicate_token = [
            ("Host", host),
            ("Origin", expected),
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
            ("x-macintent-token", server.token),
            ("x-macintent-token", server.token),
            ("x-macintent-client", CLIENT),
        ]
        assert_error(raw_request(server, "POST", "/api/connect", duplicate_token, body=b"{}"),
                     400, "invalid_request")

        duplicate_length = [
            ("Host", host),
            ("Origin", expected),
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
            ("Content-Length", "2"),
            ("x-macintent-token", server.token),
            ("x-macintent-client", CLIENT),
        ]
        assert_error(raw_request(server, "POST", "/api/connect", duplicate_length, body=b"{}"),
                     400, "invalid_request")

        duplicate_other = [
            ("Host", host),
            ("Origin", expected),
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
            ("x-macintent-token", server.token),
            ("x-macintent-client", CLIENT),
            ("X-Synthetic", "one"),
            ("X-Synthetic", "two"),
        ]
        assert_error(raw_request(server, "POST", "/api/connect", duplicate_other, body=b"{}"),
                     400, "invalid_request")

        transfer_encoding = [
            ("Host", host),
            ("Origin", expected),
            ("Content-Type", "application/json"),
            ("Transfer-Encoding", "chunked"),
            ("x-macintent-token", server.token),
            ("x-macintent-client", CLIENT),
        ]
        assert_error(raw_request(server, "POST", "/api/connect", transfer_encoding), 400, "invalid_request")

        unknown_body = [
            ("Host", host),
            ("Origin", expected),
            ("Content-Type", "application/json"),
            ("x-macintent-token", server.token),
            ("x-macintent-client", CLIENT),
        ]
        assert_error(raw_request(server, "POST", "/api/connect", unknown_body), 400, "invalid_request")

        too_large = [
            ("Host", host),
            ("Origin", expected),
            ("Content-Type", "application/json"),
            ("Content-Length", str(intent_server.MAX_BODY_BYTES + 1)),
            ("x-macintent-token", server.token),
            ("x-macintent-client", CLIENT),
        ]
        assert_error(raw_request(server, "POST", "/api/connect", too_large), 413, "body_too_large")

        status, headers, payload = request_json(
            port, "POST", "/api/connect", headers={**api_headers(server), **base}, body=body)
        assert status == 200
        assert payload == {"ok": True, "state": {**session.state, "operation": "connect"}}
        assert_security(headers)
        assert session.commands == [(CLIENT, "connect", {})]


@pytest.mark.parametrize("body", [
    b"",
    b"not-json",
    b"[]",
    b"null",
    b'"text"',
    b"\xff",
    b'{"alpha": 1, "alpha": 2}',
    b'{"alpha": {"beta": 1, "beta": 2}}',
    b'{"alpha": NaN}',
    b'{"alpha": Infinity}',
    b'{"alpha": -Infinity}',
    b'{"alpha": 1e400}',
    b'{"alpha": 1,}',
])
def test_malformed_or_nonfinite_json_never_reaches_the_session(body: bytes):
    session = StubSession()
    with running_server(session) as (server, _, port):
        result = request_json(
            port, "POST", "/api/connect",
            headers={**api_headers(server), "Content-Type": "application/json"},
            body=body,
        )
        assert_error(result, 400, "invalid_json")
        assert session.commands == []


def test_all_fixed_post_routes_forward_operation_payload_and_client():
    session = StubSession()
    with running_server(session) as (server, _, port):
        client = bootstrap(server)["client_id"]
        headers = {**api_headers(server, client=client), "Content-Type": "application/json"}
        payload = {"request_id": "synthetic-request", "value": 7}
        for operation in sorted(intent_server._POST_OPERATIONS):
            status, response_headers, response = request_json(
                port, "POST", f"/api/{operation}", headers=headers,
                body=json.dumps(payload).encode("utf-8"),
            )
            assert status == 200
            assert set(response) == {"ok", "state"}
            assert response["ok"] is True
            assert response["state"]["operation"] == operation
            assert_security(response_headers)
        assert session.commands == [
            (client, operation, payload) for operation in sorted(intent_server._POST_OPERATIONS)
        ]
        assert session.snapshots == []


def test_manager_failures_and_invalid_states_are_sanitized():
    session = StubSession()
    with running_server(session) as (server, _, port):
        session.failure = SessionError("stale_approval", 409)
        assert_error(request_json(
            port, "POST", "/api/decide",
            headers={**api_headers(server), "Content-Type": "application/json"},
            body=b'{"approval_id":"x","approve":true}'), 409, "stale_approval")
        session.failure = SessionError("not_owner", 403)
        assert_error(request_json(port, "GET", "/api/state", headers=api_headers(server)),
                     403, "not_owner")
        session.failure = RuntimeError("private exception text /tmp/secret")
        result = request_json(port, "GET", "/api/state", headers=api_headers(server))
        assert_error(result, 500, "unavailable")
        assert "private" not in json.dumps(result[2])
        session.failure = None
        session.state = ["not", "a", "projection"]  # type: ignore[assignment]
        assert_error(request_json(port, "GET", "/api/state", headers=api_headers(server)),
                     500, "unavailable")


def test_static_allowlist_routes_methods_and_no_cors_or_leaks():
    with running_server() as (server, _, port):
        for path, (body, content_type) in TEST_ASSETS.items():
            status, headers, payload = request(port, "GET", path)
            assert status == 200
            assert payload == body
            assert headers["content-type"] == content_type
            assert_security(headers)
            assert headers["server"] == "MacIntent"

        for path in [
            "/index.html", "/model.mjs", "/preview.test.mjs", "/demo/workspace/",
            "/../pyproject.toml", "/%2e%2e/pyproject.toml", "/?", "/?private-query",
            "/api/session/", "/api/unknown",
        ]:
            assert_error(request_json(port, "GET", path), 404, "not_found")

        assert_error(request_json(port, "GET", "/api/connect"), 405, "method_not_allowed")
        assert_error(request_json(port, "POST", "/api/state", headers={
            **api_headers(server), "Content-Type": "application/json"}, body=b"{}"),
            405, "method_not_allowed")
        assert_error(request_json(port, "POST", "/styles.css", headers={
            **api_headers(server), "Content-Type": "application/json"}, body=b"{}"),
            405, "method_not_allowed")
        assert_error(request_json(port, "POST", "/api/confirm", headers={
            **api_headers(server), "Content-Type": "application/json"}, body=b"{}"),
            404, "not_found")
        assert_error(request_json(port, "OPTIONS", "/api/state"), 405, "method_not_allowed")
        assert_error(request_json(port, "PATCH", "/"), 405, "method_not_allowed")

        status, headers, raw = request(port, "HEAD", "/")
        assert status == 405
        assert raw == b""
        assert headers["content-type"].startswith("application/json")
        assert_security(headers)


def test_serve_prints_only_the_startup_url_and_closes(capsys):
    class FakeServer:
        server_port = 8093

        def __init__(self):
            self.closed = 0

        def serve_forever(self, poll_interval=0.5):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            self.closed += 1

    server = FakeServer()
    assert intent_server.serve(server) == 0
    captured = capsys.readouterr()
    assert captured.out == "MacIntent live web: http://127.0.0.1:8093/\n"
    assert captured.err == ""
    assert server.closed == 1


def test_server_close_is_idempotent_and_bind_precedes_session_factory():
    session = StubSession()
    with running_server(session) as (server, _, _):
        pass
    assert session.closed == 1
    server.server_close()
    assert session.closed == 1

    with socket.socket() as occupied:
        occupied.bind((HOST, 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        called: list[bool] = []
        with pytest.raises(OSError):
            intent_server.MacIntentServer(
                port, assets=TEST_ASSETS, session_factory=lambda: called.append(True))
        assert called == []


def _write_fixed_assets(root: Path) -> None:
    (root / "web").mkdir(parents=True)
    for url, (directory, filename, _) in intent_server._ASSET_SPECS.items():
        (root / directory / filename).write_bytes(f"fixed:{url}".encode())
    (root / "unrelated.txt").write_text("not served")


def test_load_assets_reads_only_fixed_files_and_refuses_redirects(tmp_path):
    assert intent_server._ASSET_SPECS == {
        "/": ("web", "index.html", "text/html; charset=utf-8"),
        "/app.mjs": ("web", "app.mjs", "text/javascript; charset=utf-8"),
        "/live.css": ("web", "live.css", "text/css; charset=utf-8"),
        "/styles.css": ("web", "styles.css", "text/css; charset=utf-8"),
        "/scenarios.mjs": ("web", "scenarios.mjs", "text/javascript; charset=utf-8"),
        "/stars.svg": ("web", "stars.svg", "image/svg+xml"),
        "/mark.svg": ("web", "mark.svg", "image/svg+xml"),
    }
    root = tmp_path / "project"
    _write_fixed_assets(root)
    assets = intent_server.load_assets(root)
    assert set(assets) == set(intent_server._ASSET_SPECS)
    for url, (directory, filename, content_type) in intent_server._ASSET_SPECS.items():
        assert assets[url] == (f"fixed:{url}".encode(), content_type)

    root_link = tmp_path / "linked"
    root_link.symlink_to(root, target_is_directory=True)
    with pytest.raises(intent_server.AssetError):
        intent_server.load_assets(root_link)

    redirected = tmp_path / "redirected"
    _write_fixed_assets(redirected)
    real_ui = tmp_path / "real-ui"
    (redirected / "web").rename(real_ui)
    (redirected / "web").symlink_to(real_ui, target_is_directory=True)
    with pytest.raises(intent_server.AssetError):
        intent_server.load_assets(redirected)

    (root / "web" / "index.html").unlink()
    (root / "web" / "index.html").symlink_to(root / "unrelated.txt")
    with pytest.raises(intent_server.AssetError):
        intent_server.load_assets(root)
