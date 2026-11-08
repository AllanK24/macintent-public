"""Fixed-loopback HTTP transport for the explicitly started MacIntent session.

The server owns no model, microphone, credential, broker, or approval policy.
It exposes only the session's public snapshot/command protocol plus a fixed set
of local static files. There is no host/port override, cloud browser access,
CORS, fallback route, or arbitrary project-file serving.

The complete request and review notes are planned by Groq through the existing
session. Speech recognition and all workspace effects stay local. Python runs
remain compute-only and independently approved by that existing pipeline; an
unsupported platform or unavailable sandbox fails closed rather than using a
fallback.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from contextlib import suppress
import json
import math
from pathlib import Path
import re
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import Any
import uuid

from .groq_contract import MAX_CORRECTIONS, MAX_CORRECTION_TOTAL_CHARS
from .planner import MODEL_ID
from .action_types import MAX_COMMAND
from .session import (
    ERROR_CODES as MANAGER_ERROR_CODES,
    MacIntentSession,
    SessionError,
)
from .paths import WorkspacePathError, launcher_workspace


HOST = "127.0.0.1"
DEFAULT_PORT = 8093
MAX_BODY_BYTES = 64 * 1024
READ_TIMEOUT_SECONDS = 10.0

_POST_OPERATIONS = frozenset({
    "connect", "record", "send", "continue", "decide", "cancel", "reset", "disconnect", "pause", "resume",
})
_POST_PATHS = frozenset(f"/api/{operation}" for operation in _POST_OPERATIONS)
TRANSPORT_ERROR_CODES = frozenset({
    "invalid_host", "invalid_origin", "unauthorized", "invalid_json", "body_too_large",
    "not_found", "method_not_allowed",
})
ERROR_CODES = TRANSPORT_ERROR_CODES | MANAGER_ERROR_CODES
_CLIENT = re.compile(r"[0-9a-f]{32}\Z")
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "same-site", "none"})
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; media-src 'none'; worker-src 'none'; font-src 'none'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'"
)
_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=()"

DISCLOSURE = (
    "MacIntent live web sends the complete original request and any ordered review notes you submit "
    f"over HTTPS to Groq model {MODEL_ID} for planning. This includes requested note text, "
    "script or code text, file and folder names, and paths you mention. Speech recognition runs "
    "locally, and all workspace operations run locally through the existing action broker. "
    "Audio, existing file contents, execution output, workspace inventory or history, credentials, "
    "and audit data are not uploaded automatically. Planning can cover listing, folder and note "
    "creation, copying, permanent deletion, Python-script creation, and compute-only runs, including "
    "bounded multi-step plans. Review every proposed action or plan; every mutation and Python run "
    "requires its own fresh exact approval. Python execution is compute-only, uses the local OS "
    "sandbox, and fails closed if that sandbox is unavailable. Groq's own retention terms apply."
)

# URL -> (fixed directory, fixed filename, content type). No request path is ever
# concatenated with a filesystem path, and no other project file is readable.
_ASSET_SPECS = {
    "/": ("web", "index.html", "text/html; charset=utf-8"),
    "/app.mjs": ("web", "app.mjs", "text/javascript; charset=utf-8"),
    "/live.css": ("web", "live.css", "text/css; charset=utf-8"),
    "/styles.css": ("web", "styles.css", "text/css; charset=utf-8"),
    "/scenarios.mjs": ("web", "scenarios.mjs", "text/javascript; charset=utf-8"),
    "/stars.svg": ("web", "stars.svg", "image/svg+xml"),
    "/mark.svg": ("web", "mark.svg", "image/svg+xml"),
}


class AssetError(RuntimeError):
    """Sanitized startup failure for a missing or redirected fixed asset."""

    def __init__(self, code: str = "static_asset_unavailable") -> None:
        self.code = code
        super().__init__(code)


def load_assets(root: Path) -> dict[str, tuple[bytes, str]]:
    """Read only the fixed public assets, refusing symlink/path redirects."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise AssetError("static_root_unavailable")
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise AssetError("static_root_unavailable") from None
    result: dict[str, tuple[bytes, str]] = {}
    for url, (directory_name, filename, content_type) in _ASSET_SPECS.items():
        directory = root / directory_name
        if directory.is_symlink() or not directory.is_dir():
            raise AssetError()
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise AssetError()
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(resolved_root)
            body = resolved.read_bytes()
        except (OSError, RuntimeError, ValueError):
            raise AssetError() from None
        result[url] = (body, content_type)
    return result


class MacIntentServer(ThreadingHTTPServer):
    """Loopback server; the injected session/factory is created after bind."""

    daemon_threads = True

    def __init__(
        self,
        port: int,
        *,
        assets: Mapping[str, tuple[bytes, str]],
        session: Any | None = None,
        session_factory: Any | None = None,
        token: str | None = None,
    ) -> None:
        if (session is None) == (session_factory is None):
            raise TypeError("provide exactly one session or session_factory")
        if not isinstance(assets, Mapping):
            raise TypeError("assets must be a mapping")
        if token is not None and (type(token) is not str or not token):
            raise ValueError("invalid token")
        self.assets = dict(assets)
        self.token = secrets.token_urlsafe(32) if token is None else token
        self.session: Any | None = None
        self._close_lock = Lock()
        self._session_closed = False
        super().__init__((HOST, port), MacIntentHandler)
        try:
            self.session = session if session_factory is None else session_factory()
        except BaseException:
            super().server_close()
            raise

    def close_session(self) -> None:
        with self._close_lock:
            if self._session_closed:
                return
            self._session_closed = True
            session = self.session
        if session is not None:
            session.close()

    def server_close(self) -> None:
        try:
            self.close_session()
        finally:
            super().server_close()

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # No socket/request/exception logging.


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def _check_finite(value: Any) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite")
    elif isinstance(value, dict):
        for item in value.values():
            _check_finite(item)
    elif isinstance(value, list):
        for item in value:
            _check_finite(item)


class MacIntentHandler(BaseHTTPRequestHandler):
    server: MacIntentServer

    protocol_version = "HTTP/1.1"
    timeout = READ_TIMEOUT_SECONDS
    server_version = "MacIntent"
    sys_version = ""

    def version_string(self) -> str:
        return self.server_version

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_HEAD(self) -> None:
        self._unsupported_method()

    def do_PUT(self) -> None:
        self._unsupported_method()

    def do_PATCH(self) -> None:
        self._unsupported_method()

    def do_DELETE(self) -> None:
        self._unsupported_method()

    def do_OPTIONS(self) -> None:
        self._unsupported_method()

    def do_TRACE(self) -> None:
        self._unsupported_method()

    def do_CONNECT(self) -> None:
        self._unsupported_method()

    def handle_one_request(self) -> None:
        # Reset before parse errors too, so a malformed keep-alive request still
        # receives one sanitized response.
        self._response_started = False
        super().handle_one_request()

    def _handle(self, method: str) -> None:
        self._response_started = False
        try:
            if not self._valid_host():
                self._error(403, "invalid_host")
                return
            if self._has_duplicate_headers():
                self._error(400, "invalid_request")
                return
            if self.headers.get("Transfer-Encoding") is not None:
                self._error(400, "invalid_request")
                return
            if not self._valid_origin(post=method == "POST"):
                self._error(403, "invalid_origin")
                return
            if method == "GET":
                length = self.headers.get("Content-Length")
                if length is not None and re.fullmatch(r"0+", length.strip()) is None:
                    self._error(400, "invalid_request")
                    return
                self._get()
            else:
                self._post()
        except Exception:
            self.close_connection = True
            if not getattr(self, "_response_started", False):
                with suppress(Exception):
                    self._send_json(500, {"ok": False, "error": "unavailable"})

    def _unsupported_method(self) -> None:
        self._response_started = False
        try:
            if not self._valid_host():
                self._error(403, "invalid_host")
                return
            if self._has_duplicate_headers():
                self._error(400, "invalid_request")
                return
            if self.headers.get("Transfer-Encoding") is not None:
                self._error(400, "invalid_request")
                return
            if not self._valid_origin(post=False):
                self._error(403, "invalid_origin")
                return
            self._error(405, "method_not_allowed")
        except Exception:
            self.close_connection = True
            if not getattr(self, "_response_started", False):
                with suppress(Exception):
                    self._send_json(500, {"ok": False, "error": "unavailable"})

    def _get(self) -> None:
        path = self.path
        if path == "/api/session":
            self._bootstrap()
        elif path == "/api/state":
            client = self._authorized_client()
            if client is None:
                return
            self._manager_call(lambda: self.server.session.snapshot(client))
        elif path in self.server.assets:
            body, content_type = self.server.assets[path]
            self._send(200, content_type, body)
        elif path in _POST_PATHS:
            self._error(405, "method_not_allowed")
        else:
            self._error(404, "not_found")

    def _post(self) -> None:
        path = self.path
        if path not in _POST_PATHS:
            known = path in self.server.assets or path in {"/api/session", "/api/state"}
            self._error(405 if known else 404, "method_not_allowed" if known else "not_found")
            return
        # No request body is chunked or kept for a subsequent request on this
        # connection; every POST closes after its one bounded body.
        self.close_connection = True
        client = self._authorized_client()
        if client is None:
            return
        if self.headers.get("Content-Type") != "application/json":
            self._error(400, "invalid_request")
            return
        payload = self._read_payload()
        if payload is None:
            return
        operation = path.removeprefix("/api/")
        self._manager_call(lambda: self.server.session.command(client, operation, payload))

    def _bootstrap(self) -> None:
        # Bootstrap is metadata only: no manager call, credential read, request,
        # recording, or workspace effect.
        self._send_json(200, {
            "token": self.server.token,
            "client_id": uuid.uuid4().hex,
            "disclosure": DISCLOSURE,
            "model": MODEL_ID,
            "max_command": MAX_COMMAND,
            "max_corrections": MAX_CORRECTIONS,
            "max_correction_chars": MAX_CORRECTION_TOTAL_CHARS,
        })

    def _manager_call(self, call: Any) -> None:
        if self.server.session is None:
            self._error(503, "unavailable")
            return
        try:
            state = call()
        except SessionError as exc:
            code = exc.code if type(exc.code) is str and exc.code in ERROR_CODES else "unavailable"
            status = exc.status if type(exc.status) is int and 400 <= exc.status <= 599 else 500
            self._error(status, code)
            return
        except Exception:
            self._error(500, "unavailable")
            return
        if type(state) is not dict:
            self._error(500, "unavailable")
            return
        self._send_json(200, {"ok": True, "state": state})

    def _read_payload(self) -> dict[str, Any] | None:
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or re.fullmatch(r"[0-9]{1,20}", lengths[0].strip()) is None:
            self._error(400, "invalid_request")
            return None
        length = int(lengths[0].strip())
        if length > MAX_BODY_BYTES:
            self._error(413, "body_too_large")
            return None
        if length == 0:
            self._error(400, "invalid_json")
            return None
        try:
            body = self.rfile.read(length)
        except (TimeoutError, OSError):
            self.close_connection = True
            self._error(408, "invalid_request")
            return None
        if len(body) != length:
            self._error(400, "invalid_request")
            return None
        try:
            text = body.decode("utf-8")
            payload = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            _check_finite(payload)
        except (UnicodeError, ValueError, TypeError, RecursionError):
            self._error(400, "invalid_json")
            return None
        if type(payload) is not dict:
            self._error(400, "invalid_json")
            return None
        return payload

    def _valid_host(self) -> bool:
        values = self.headers.get_all("Host") or []
        return len(values) == 1 and values[0] == f"{HOST}:{self.server.server_port}"

    def _has_duplicate_headers(self) -> bool:
        names = [name.lower() for name in self.headers.keys()]
        return len(names) != len(set(names))

    def _valid_origin(self, *, post: bool) -> bool:
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site.strip().lower() not in _ALLOWED_FETCH_SITES:
            return False
        origin = self.headers.get("Origin")
        expected = f"http://{HOST}:{self.server.server_port}"
        if post:
            return origin == expected
        return origin is None or origin == expected

    def _authorized_client(self) -> str | None:
        token = self.headers.get("x-macintent-token")
        client = self.headers.get("x-macintent-client")
        if type(token) is not str or type(client) is not str or _CLIENT.fullmatch(client) is None:
            self._error(401, "unauthorized")
            return None
        try:
            valid = secrets.compare_digest(token, self.server.token)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            self._error(401, "unauthorized")
            return None
        return client

    def _error(self, status: int, code: str) -> None:
        code = code if code in ERROR_CODES else "unavailable"
        if type(status) is not int or not 400 <= status <= 599:
            status = 500
        self.close_connection = True
        self._send_json(status, {"ok": False, "error": code})

    def _send_json(self, status: int, value: Any) -> None:
        try:
            body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            status = 500
            body = b'{"ok":false,"error":"unavailable"}'
        self._send(status, "application/json; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        if getattr(self, "_response_started", False):
            return
        self._response_started = True
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("Permissions-Policy", _PERMISSIONS_POLICY)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if getattr(self, "command", None) != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionError, OSError):
            self.close_connection = True

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Parser-level errors also use the closed JSON/security contract."""
        self.close_connection = True
        status = 405 if code == 501 else code
        if not 400 <= status <= 599:
            status = 400
        with suppress(Exception):
            self._send_json(status, {"ok": False, "error": {
                404: "not_found", 405: "method_not_allowed", 413: "body_too_large",
                501: "method_not_allowed",
            }.get(code, "invalid_request")})

    def log_message(self, format: str, *args: object) -> None:
        pass  # No request/query/header/error logging.

    def log_error(self, format: str, *args: object) -> None:
        pass


def serve(server: MacIntentServer) -> int:
    """Run the owned serving thread and close its session/server on shutdown."""
    worker = Thread(target=server.serve_forever, daemon=True, name="macintent-http")
    failed = False
    try:
        worker.start()
        print(f"MacIntent live web: http://{HOST}:{server.server_port}/", flush=True)
        worker.join()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if worker.is_alive():
                server.shutdown()
                worker.join(timeout=5)
        finally:
            try:
                server.server_close()
            except SessionError:
                failed = True
                print("MacIntent session cleanup did not complete; inspect the local session.", flush=True)
            except Exception:
                failed = True
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Loopback MacIntent live web transport.")
    parser.add_argument("port", type=int, nargs="?", default=DEFAULT_PORT,
                        help="fixed loopback port (default: 8093)")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        root, _ = launcher_workspace()
        assets = load_assets(root)
    except (WorkspacePathError, AssetError, OSError):
        print("MacIntent live web unavailable: fixed local static assets or launcher environment are unavailable.",
              flush=True)
        return 1
    try:
        server = MacIntentServer(
            args.port,
            assets=assets,
            session_factory=lambda: MacIntentSession(root),
        )
    except (OSError, SessionError, RuntimeError):
        print("MacIntent live web unavailable: the fixed loopback port is unavailable or the session could not start.",
              flush=True)
        return 1
    return serve(server)


if __name__ == "__main__":
    raise SystemExit(main())
