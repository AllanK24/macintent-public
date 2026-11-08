"""Fail-closed, compute-only execution for approved Python source.

The interpreter's isolated mode and import checks are defense in depth only;
the actual filesystem, process and network boundary is the macOS seatbelt
profile passed to ``sandbox-exec``.
"""

from __future__ import annotations

import ctypes
import os
import platform
import selectors
import signal
import stat
import struct
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_SCRIPT_BYTES = 16_384
MAX_OUTPUT_BYTES = 16 * 1024
WALL_SECONDS = 5.0
CPU_SECONDS = 2
RSS_LIMIT_BYTES = 128 * 1024 * 1024
_RUNTIME_NAME = "cpython-3.12.14-macos-aarch64-none"
_READY = b"SCRIPT_SANDBOX_READY"


class SandboxError(Exception):
    """Static, sanitized sandbox failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SandboxResult:
    stdout: str
    stderr: str
    exit_code: int


def _proc_rss(pid: int) -> int | None:
    """Return Darwin proc_taskinfo resident bytes, or None if unsupported."""

    if platform.system() != "Darwin":
        return None
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        proc_pidinfo.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(96)
        # PROC_PIDTASKINFO is 4; pti_resident_size is the second uint64.
        result = proc_pidinfo(pid, 4, 0, buffer, ctypes.sizeof(buffer))
        if result != ctypes.sizeof(buffer):
            return None
        return struct.unpack_from("=Q", buffer.raw, 8)[0]
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _checked_directory(path: Path) -> Path:
    try:
        st = os.lstat(path)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SandboxError("script_execution_unavailable") from exc
    if not stat.S_ISDIR(st.st_mode) or resolved != path:
        raise SandboxError("script_execution_unavailable")
    return resolved


def _managed_runtime(root: Path) -> tuple[Path, Path, Path]:
    if platform.system() != "Darwin":
        raise SandboxError("script_execution_unavailable")
    root = Path(os.path.abspath(root))
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise SandboxError("script_execution_unavailable") from exc
    if resolved_root != root:
        raise SandboxError("script_execution_unavailable")

    runtime = root / ".local" / "share" / "uv" / "python" / _RUNTIME_NAME
    runtime = _checked_directory(runtime)
    binary = runtime / "bin" / "python3.12"
    try:
        binary_st = os.stat(binary, follow_symlinks=False)
        resolved_binary = binary.resolve(strict=True)
        resolved_binary.relative_to(runtime)
    except (OSError, ValueError) as exc:
        raise SandboxError("script_execution_unavailable") from exc
    if not (stat.S_ISREG(binary_st.st_mode) or stat.S_ISLNK(binary_st.st_mode)) or not os.access(resolved_binary, os.X_OK):
        raise SandboxError("script_execution_unavailable")
    stdlib = runtime / "lib" / "python3.12"
    _checked_directory(stdlib)
    site_packages = stdlib / "site-packages"

    # The application itself is expected to run on this managed base runtime.
    try:
        if Path(os.path.realpath(__import__("sys").base_prefix)) != runtime:
            raise SandboxError("script_execution_unavailable")
    except (OSError, TypeError, ValueError) as exc:
        raise SandboxError("script_execution_unavailable") from exc
    return runtime, resolved_binary, site_packages


def _sbpl_quote(path: Path | str) -> str:
    value = os.fspath(path)
    if any(ord(character) < 0x20 for character in value):
        raise SandboxError("script_execution_unavailable")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _profile(runtime: Path, python: Path, site_packages: Path, *, cwd: Path | None = None) -> str:
    runtime_q = _sbpl_quote(runtime)
    python_q = _sbpl_quote(python)
    site_q = _sbpl_quote(site_packages)
    return "\n".join((
        "(version 1)",
        "(deny default)",
        '(allow file-read* (literal "/"))',
        "(allow file-read-metadata " + " ".join(f"(literal {_sbpl_quote(p)})" for p in runtime.parents) + ")",
        ("(allow file-read-metadata " + " ".join(f"(literal {_sbpl_quote(p)})" for p in (cwd, *cwd.parents)) + ")") if cwd is not None else "",
        '(allow sysctl-read (sysctl-name-prefix "hw.") (sysctl-name-prefix "kern.os") (sysctl-name "kern.usrstack64"))',
        # Only the selected managed runtime and public operating-system
        # libraries may be read.  Third-party runtime packages stay denied.
        f"(allow file-read* (subpath {runtime_q}))",
        f"(deny file-read* (subpath {site_q}))",
        '(allow file-read* (subpath "/System/Library"))',
        '(allow file-read* (subpath "/usr/lib"))',
        "(deny file-write*)",
        "(deny network*)",
        "(deny process-fork)",
        f"(allow process-exec (literal {python_q}))",
    ))


_BOOTSTRAP = r'''import os, resource, sys
try:
    with open(os.path.join(os.getcwd(), ".sandbox-probe"), "rb") as marker:
        marker.read(1)
except PermissionError:
    pass
else:
    os._exit(125)
try:
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    os.write(__STATUS_FD__, b"SCRIPT_SANDBOX_READY")
    os.close(__STATUS_FD__)
except BaseException:
    os._exit(125)
try:
    source = sys.stdin.buffer.read(16385)
    if len(source) > 16384:
        os._exit(125)
    exec(compile(source.decode("utf-8"), "<approved-script>", "exec"), {"__name__": "__main__", "__file__": "<approved-script>"})
except SystemExit as exit_request:
    status = exit_request.code
    if status is None:
        status = 0
    if type(status) is not int or not 0 <= status <= 255 or status == 125:
        status = 1
    raise SystemExit(status)
except BaseException:
    sys.stderr.write("script failed\n")
    raise SystemExit(1)
'''


def _stop_process(process: subprocess.Popen[bytes]) -> bool:
    """Kill and reap the owned process group; report unverifiable cleanup."""

    try:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1.0)
        return process.poll() is not None
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=1.0)
            return process.poll() is not None
        except (OSError, subprocess.TimeoutExpired):
            return False


def run_script(code: bytes, *, root: Path, stop_event: Any = None) -> SandboxResult:
    """Run captured UTF-8 source under macOS seatbelt with bounded resources.

    The only user-controlled data is sent over stdin after a sandbox bootstrap
    has verified that a synthetic file in its private cwd cannot be read.
    """

    if not isinstance(code, bytes) or not 1 <= len(code) <= MAX_SCRIPT_BYTES:
        raise SandboxError("script_failed")
    if stop_event is not None and not callable(getattr(stop_event, "is_set", None)):
        raise SandboxError("script_execution_unavailable")
    if stop_event is not None and stop_event.is_set():
        raise SandboxError("script_cancelled")
    runtime, python, site_packages = _managed_runtime(Path(root))
    sandbox = Path("/usr/bin/sandbox-exec")
    try:
        sandbox_st = os.stat(sandbox, follow_symlinks=False)
    except OSError as exc:
        raise SandboxError("script_execution_unavailable") from exc
    if not stat.S_ISREG(sandbox_st.st_mode) or not os.access(sandbox, os.X_OK):
        raise SandboxError("script_execution_unavailable")
    if _proc_rss(os.getpid()) is None:
        raise SandboxError("script_execution_unavailable")

    temp_root = Path(root) / ".cache" / "tmp"
    _checked_directory(temp_root)
    temp_dir: str | None = None
    marker_identity: tuple[int, int] | None = None
    process: subprocess.Popen[bytes] | None = None
    status_read = status_write = -1
    selector: selectors.BaseSelector | None = None
    stdout_data = bytearray()
    stderr_data = bytearray()
    error: SandboxError | None = None
    try:
        temp_dir = tempfile.mkdtemp(prefix=".script-run-", dir=temp_root)
        os.chmod(temp_dir, 0o700)
        marker = Path(temp_dir) / ".sandbox-probe"
        marker_fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(marker_fd, b"synthetic sandbox probe")
            marker_stat = os.fstat(marker_fd)
            marker_identity = (marker_stat.st_dev, marker_stat.st_ino)
        finally:
            os.close(marker_fd)

        status_read, status_write = os.pipe()
        os.set_inheritable(status_write, True)
        bootstrap = _BOOTSTRAP.replace("__STATUS_FD__", str(status_write))
        command = [str(sandbox), "-p", _profile(runtime, python, site_packages, cwd=Path(temp_dir)), str(python), "-I", "-S", "-B", "-c", bootstrap]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=temp_dir,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "PYTHONNOUSERSITE": "1"},
                close_fds=True,
                pass_fds=(status_write,),
                start_new_session=True,
                bufsize=0,
            )
        except (OSError, ValueError) as exc:
            raise SandboxError("script_execution_unavailable") from exc
        os.close(status_write)
        status_write = -1

        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        for stream in (process.stdin, process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
        os.set_blocking(status_read, False)
        selector = selectors.DefaultSelector()
        stdout_fd = process.stdout.fileno()
        stderr_fd = process.stderr.fileno()
        selector.register(stdout_fd, selectors.EVENT_READ, "stdout")
        selector.register(stderr_fd, selectors.EVENT_READ, "stderr")
        selector.register(status_read, selectors.EVENT_READ, "status")

        ready = False
        source_offset = 0
        stdin_open = True
        started = time.monotonic()
        deadline = started + WALL_SECONDS
        startup_deadline = started + min(2.0, WALL_SECONDS)
        status_bytes = bytearray()
        open_outputs = {stdout_fd, stderr_fd}
        while process.poll() is None or open_outputs or not ready:
            now = time.monotonic()
            if stop_event is not None and stop_event.is_set():
                error = SandboxError("script_cancelled")
                break
            if now >= deadline:
                error = SandboxError("script_timeout")
                break
            if not ready and now >= startup_deadline:
                error = SandboxError("script_execution_unavailable")
                break
            if ready:
                rss = _proc_rss(process.pid) if process.poll() is None else 0
                if rss is None and process.poll() is None:
                    error = SandboxError("script_execution_unavailable")
                    break
                if rss is not None and rss > RSS_LIMIT_BYTES:
                    error = SandboxError("script_memory_limit")
                    break
                if stdin_open:
                    selector.modify(process.stdin.fileno(), selectors.EVENT_WRITE, "stdin")
            timeout = min(0.05, max(0.0, deadline - now))
            for key, _ in selector.select(timeout):
                kind = key.data
                if kind == "stdin":
                    try:
                        written = os.write(key.fd, code[source_offset:source_offset + 4096])
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, OSError):
                        stdin_open = False
                        try:
                            selector.unregister(key.fd)
                        except (KeyError, ValueError):
                            pass
                        process.stdin.close()
                        continue
                    source_offset += written
                    if source_offset >= len(code):
                        stdin_open = False
                        selector.unregister(key.fd)
                        process.stdin.close()
                elif kind == "status":
                    try:
                        block = os.read(key.fd, 128)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fd)
                        os.close(status_read)
                        status_read = -1
                    else:
                        status_bytes.extend(block)
                        if _READY in status_bytes:
                            ready = True
                            if stdin_open:
                                selector.register(process.stdin.fileno(), selectors.EVENT_WRITE, "stdin")
                        elif len(status_bytes) > len(_READY):
                            error = SandboxError("script_execution_unavailable")
                            break
                else:
                    try:
                        block = os.read(key.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fd)
                        open_outputs.discard(key.fd)
                        continue
                    target = stdout_data if kind == "stdout" else stderr_data
                    remaining = MAX_OUTPUT_BYTES - len(stdout_data) - len(stderr_data)
                    if len(block) > remaining:
                        target.extend(block[:max(remaining, 0)])
                        error = SandboxError("script_output_limit")
                        break
                    target.extend(block)
            if error is not None:
                break
            if process.poll() is not None and not ready:
                error = SandboxError("script_execution_unavailable")
                break
            if ready and stdin_open and source_offset == len(code):
                stdin_open = False
                try:
                    selector.unregister(process.stdin.fileno())
                except (KeyError, ValueError):
                    pass
                process.stdin.close()

        if error is not None:
            if not _stop_process(process):
                raise SandboxError("outcome_unknown")
            raise error
        try:
            return_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            if not _stop_process(process):
                raise SandboxError("outcome_unknown") from exc
            raise SandboxError("script_timeout") from exc
        exit_code = return_code if return_code >= 0 else min(255, 128 + -return_code)
        if exit_code == 125:
            raise SandboxError("script_execution_unavailable")
        # Pipes are drained by the selector loop before returning.  Keep any
        # impossible residual bytes bounded as a final defensive check.
        if len(stdout_data) + len(stderr_data) > MAX_OUTPUT_BYTES:
            raise SandboxError("script_output_limit")
        return SandboxResult(stdout_data.decode("utf-8", errors="replace"), stderr_data.decode("utf-8", errors="replace"), exit_code)
    except SandboxError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if process is not None and process.poll() is None and not _stop_process(process):
            raise SandboxError("outcome_unknown") from exc
        raise SandboxError("script_execution_unavailable") from exc
    finally:
        if process is not None and process.poll() is None:
            if not _stop_process(process):
                error = SandboxError("outcome_unknown")
        if selector is not None:
            try:
                selector.close()
            except Exception:
                error = SandboxError("outcome_unknown")
        for fd in (status_read, status_write):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    error = SandboxError("outcome_unknown")
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        error = SandboxError("outcome_unknown")
        if temp_dir is not None:
            marker = Path(temp_dir) / ".sandbox-probe"
            try:
                current = os.stat(marker, follow_symlinks=False)
                if marker_identity != (current.st_dev, current.st_ino):
                    raise OSError("probe changed")
                os.unlink(marker)
                os.rmdir(temp_dir)
            except FileNotFoundError:
                try:
                    os.rmdir(temp_dir)
                except OSError:
                    error = SandboxError("outcome_unknown")
            except OSError:
                error = SandboxError("outcome_unknown")
        if error is not None:
            raise error
