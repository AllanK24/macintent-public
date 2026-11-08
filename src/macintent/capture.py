"""Owned native command capture with a bounded, explicit readiness protocol."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import selectors
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .native_helper import NativeBridge
from .speech_worker import MAX_PCM_BYTES

_CAP = 3 * 1024 * 1024
_REQUEST = b'{"op":"capture_command","arguments":{"max_seconds":30}}\n'


class CaptureError(RuntimeError):
    def __init__(self, code: str = "invalid_capture") -> None:
        self.code = code if code in {"invalid_capture", "permission_denied", "unsupported_device", "native_unavailable",
                                     "capture_timeout", "cancelled", "capture_busy", "helper_outdated", "cleanup_failed"} else "invalid_capture"
        super().__init__(self.code)


@dataclass(frozen=True)
class CapturedCommand:
    pcm: bytes | None
    seconds: float
    reason: str


def _validate_fixed_pcm(result: Any) -> bytes | None:
    """Validate the complete strict native float32/16 kHz capture reply."""
    if not isinstance(result, dict) or set(result) != {"sample_rate", "pcm_f32le", "audio_seconds"}:
        raise ValueError("invalid_audio")
    rate, encoded, seconds = result["sample_rate"], result["pcm_f32le"], result["audio_seconds"]
    if type(rate) is not int or rate != 16000 or not isinstance(encoded, str) or len(encoded) > 2_560_000:
        raise ValueError("invalid_audio")
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 30:
        raise ValueError("invalid_audio")
    try:
        pcm = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid_audio") from exc
    if len(pcm) > MAX_PCM_BYTES or len(pcm) % 4:
        raise ValueError("invalid_audio")
    samples = len(pcm) // 4
    if samples > 480_000 or (samples == 0) != (seconds == 0):
        raise ValueError("invalid_audio")
    if abs(seconds - samples / 16000) > (1 / 16000):
        raise ValueError("invalid_audio")
    if any(not math.isfinite(value) for (value,) in struct.iter_unpack("<f", pcm)):
        raise ValueError("invalid_audio")
    return pcm or None


def validate_command_capture(value: Any) -> CapturedCommand:
    if (not isinstance(value, dict) or set(value) != {"sample_rate", "pcm_f32le", "audio_seconds", "reason"}
            or type(value["reason"]) is not str or value["reason"] not in {"silence", "limit", "no_speech"}):
        raise CaptureError()
    try:
        pcm = _validate_fixed_pcm({key: value[key] for key in ("sample_rate", "pcm_f32le", "audio_seconds")})
    except (ValueError, TypeError):
        raise CaptureError() from None
    if (pcm is None) != (value["reason"] == "no_speech"):
        raise CaptureError()
    return CapturedCommand(pcm, float(value["audio_seconds"]), value["reason"])


class _Frames:
    """Never forward native bytes, audio, or arbitrary errors to the Terminal."""
    def __init__(self, on_ready: Callable[[], None] | None, *, validation: bool = False) -> None:
        self.on_ready, self.validation = on_ready, validation
        self.pending = bytearray()
        self.total = 0
        self.ready = False
        self.result: CapturedCommand | bool | None = None

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.total > (32768 if self.validation else _CAP):
            raise CaptureError()
        self.pending.extend(chunk)
        while b"\n" in self.pending:
            line, _, tail = self.pending.partition(b"\n")
            self.pending = bytearray(tail)
            try:
                frame = json.loads(line, object_pairs_hook=NativeBridge._unique_object,
                                   parse_constant=NativeBridge._reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise CaptureError() from None
            if self.result is not None:
                raise CaptureError()
            if frame == {"event": "ready"}:
                if self.validation or self.ready:
                    raise CaptureError()
                self.ready = True
                if self.on_ready is not None:
                    self.on_ready()
                continue
            if not isinstance(frame, dict) or type(frame.get("ok")) is not bool:
                raise CaptureError()
            if set(frame) == {"ok", "error"} and frame["ok"] is False:
                code = frame["error"]
                if type(code) is not str:
                    raise CaptureError()
                raise CaptureError("helper_outdated" if self.validation and code == "invalid_request" else code)
            if set(frame) != {"ok", "result"} or frame["ok"] is not True:
                raise CaptureError()
            if self.validation:
                if frame["result"] != {"valid": True} or type(frame["result"].get("valid")) is not bool:
                    raise CaptureError()
                self.result = True
            else:
                if not self.ready:
                    raise CaptureError()
                self.result = validate_command_capture(frame["result"])

    def finish(self) -> CapturedCommand | bool:
        if self.pending or self.result is None:
            raise CaptureError()
        return self.result


class CommandMicrophone:
    """Only the fixed capture operation; no desktop tools or model callbacks."""
    def __init__(self, root: Path, *, stop_event: Any = None, timeout: float = 70.0) -> None:
        self.root, self.stop_event, self.timeout = Path(root), stop_event, timeout
        self._lock = threading.Lock()

    def check(self) -> None:
        # --validate exits before NSApplication/capture; no permission/device query.
        self._exchange(None, validation=True)

    def capture(self, on_ready: Callable[[], None]) -> CapturedCommand:
        if not callable(on_ready):
            raise CaptureError()
        result = self._exchange(on_ready)
        if not isinstance(result, CapturedCommand):
            raise CaptureError()
        return result

    def _exchange(self, on_ready: Callable[[], None] | None, *, validation: bool = False) -> CapturedCommand | bool:
        if not self._lock.acquire(blocking=False):
            raise CaptureError("capture_busy")
        process = None
        selector = None
        try:
            if self.stop_event is not None and self.stop_event.is_set():
                raise CaptureError("cancelled")
            bridge = NativeBridge(self.root)
            executable = bridge._executable()
            deadline = time.monotonic() + (5.0 if validation else self.timeout)
            process = subprocess.Popen([str(executable), *(["--validate"] if validation else [])],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       shell=False, cwd=self.root, env=bridge._child_environment(), bufsize=0)
            selector = selectors.DefaultSelector()
            incoming, outgoing = process.stdout, process.stdin
            if incoming is None or outgoing is None:
                raise CaptureError()
            for stream in (incoming, outgoing):
                os.set_blocking(stream.fileno(), False)
            selector.register(incoming, selectors.EVENT_READ, "read")
            selector.register(outgoing, selectors.EVENT_WRITE, "write")
            pending = memoryview(_REQUEST)
            frames = _Frames(on_ready, validation=validation)
            while selector.get_map() or process.poll() is None:
                if self.stop_event is not None and self.stop_event.is_set():
                    raise CaptureError("cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CaptureError("capture_timeout")
                for key, _ in selector.select(min(0.05, remaining)):
                    if key.data == "write":
                        written = os.write(key.fileobj.fileno(), pending)
                        pending = pending[written:]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                        if chunk:
                            frames.feed(chunk)
                        else:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
            if process.returncode != 0:
                raise CaptureError("native_unavailable")
            if self.stop_event is not None and self.stop_event.is_set():
                raise CaptureError("cancelled")
            return frames.finish()
        except CaptureError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise CaptureError("native_unavailable") from None
        finally:
            cleanup_failed = False
            try:
                if process is not None:
                    try:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=3)
                    except (OSError, subprocess.TimeoutExpired):
                        cleanup_failed = True
                    finally:
                        for stream in (process.stdin, process.stdout):
                            if stream is not None and not stream.closed:
                                stream.close()
            finally:
                if selector is not None:
                    selector.close()
                self._lock.release()
            if cleanup_failed:
                raise CaptureError("cleanup_failed")


__all__ = ["CaptureError", "CapturedCommand", "CommandMicrophone", "_Frames", "validate_command_capture"]
