"""Owned, cancellable process adapter for local MacIntent speech inference."""

from __future__ import annotations

import multiprocessing
import math
import os
from pathlib import Path
import threading
import time
from typing import Any


MAX_PCM_BYTES = 1_920_000
MAX_SAMPLES = 480_000


def _worker(connection: Any, root: str, backend_factory: Any) -> None:
    # A model/runtime may write directly to file descriptors as well as through
    # Python logging. Keep all child diagnostics off the user's terminal.
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
    finally:
        if null_fd not in (1, 2):
            os.close(null_fd)
    try:
        if backend_factory is None:
            raise RuntimeError("backend_factory_required")
        backend = backend_factory()
        connection.send({"ready": True})
    except BaseException:
        try:
            connection.send({"ready": False, "code": "model_unavailable"})
        except Exception:
            pass
        connection.close()
        return
    try:
        while True:
            request = connection.recv()
            if request == {"stop": True}:
                return
            if (not isinstance(request, dict) or set(request) not in ({"pcm"}, {"pcm", "diagnostics"})
                    or ("diagnostics" in request and type(request["diagnostics"]) is not bool)):
                connection.send({"ok": False, "code": "invalid_audio"})
                request = None
                continue
            payload = request["pcm"]
            if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_PCM_BYTES or len(payload) % 4:
                del request, payload
                connection.send({"ok": False, "code": "invalid_audio"})
                continue
            samples = text = raw = None
            try:
                import numpy as np
                samples = np.frombuffer(payload, dtype="<f4")
                if not 0 < samples.size <= MAX_SAMPLES or not np.isfinite(samples).all():
                    raise ValueError
                del payload
                from .asr_protocol import invoke_backend_diagnostic
                raw, text = invoke_backend_diagnostic(backend, samples)
                if request.get("diagnostics") is True:
                    # Public strings only, bounded before IPC.  The parent
                    # records these before applying the normal response gate.
                    normalized = " ".join(text[:4096].split())
                    connection.send({"diagnostic": {"raw": raw[:4096], "normalized": normalized[:4096],
                                                      "raw_truncated": len(raw) > 4096,
                                                      "normalized_truncated": len(text) > 4096}})
                if not isinstance(text, str) or len(text) > 4096 or any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 for c in text):
                    raise ValueError
                connection.send({"ok": True, "text": text})
            except BaseException:
                connection.send({"ok": False, "code": "transcription_failed"})
            finally:
                # Drop utterance-sized data before accepting another request.
                request = payload = samples = raw = text = normalized = None
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


class SpeechProcess:
    """One child process owns the model; cancellation terminates it, never overlaps."""

    def __init__(self, root: str | Path, *, backend_factory: Any = None, startup_timeout: float = 120.0,
                 request_timeout: float = 60.0, stop_event: Any = None) -> None:
        self.root = str(root)
        self.backend_factory = backend_factory
        self.startup_timeout = startup_timeout
        self.request_timeout = request_timeout
        self.stop_event = stop_event
        self.process = None
        self.connection = None
        self._child_connection = None
        self._lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        if self._started or self.process is not None:
            raise RuntimeError("speech process already started")
        self._started = True
        self._check_cancelled()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self.connection, self._child_connection = parent, child
        try:
            process = context.Process(target=_worker, args=(child, self.root, self.backend_factory), daemon=True)
            # Publish ownership before start: start() itself can be interrupted
            # after the OS process has been created.
            self.process = process
            process.start()
            child.close()
            self._child_connection = None
            deadline = time.monotonic() + self.startup_timeout
            while True:
                self._check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("model_unavailable")
                if parent.poll(min(0.05, remaining)):
                    ready = parent.recv()
                    if not isinstance(ready, dict) or set(ready) != {"ready"} or type(ready["ready"]) is not bool or ready["ready"] is not True:
                        raise RuntimeError("model_unavailable")
                    return
                if not process.is_alive():
                    raise RuntimeError("model_unavailable")
        except BaseException:
            self.stop()
            raise

    def transcribe(self, pcm: bytes, *, diagnostic: Any = None) -> str:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("transcription_failed")
        try:
            return self._transcribe(pcm, diagnostic=diagnostic)
        finally:
            self._lock.release()

    def _transcribe(self, pcm: bytes, *, diagnostic: Any = None) -> str:
        if not isinstance(pcm, bytes) or not 0 < len(pcm) <= MAX_PCM_BYTES or len(pcm) % 4:
            raise ValueError("invalid_audio")
        import struct
        if any(not math.isfinite(value) for (value,) in struct.iter_unpack("<f", pcm)):
            raise ValueError("invalid_audio")
        self._check_cancelled()
        if self.process is None or self.connection is None or not self.process.is_alive():
            raise RuntimeError("model_unavailable")
        try:
            self.connection.send({"pcm": pcm, **({"diagnostics": True} if diagnostic is not None else {})})
            deadline = time.monotonic() + self.request_timeout
            diagnostic_seen = False
            while True:
                self._check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("transcription_failed")
                if self.connection.poll(min(0.05, remaining)):
                    result = self.connection.recv()
                    if isinstance(result, dict) and "diagnostic" in result:
                        frame = result.get("diagnostic")
                        if (diagnostic is None or diagnostic_seen or set(result) != {"diagnostic"}
                                or not isinstance(frame, dict)
                                or set(frame) != {"raw", "normalized", "raw_truncated", "normalized_truncated"}
                                or any(not isinstance(frame[key], str) or len(frame[key]) > 4096 for key in ("raw", "normalized"))
                                or type(frame["raw_truncated"]) is not bool or type(frame["normalized_truncated"]) is not bool):
                            raise RuntimeError("transcription_failed")
                        diagnostic_seen = True
                        try:
                            diagnostic(frame["raw"], frame["normalized"], frame["raw_truncated"], frame["normalized_truncated"])
                        except Exception:
                            # Ordinary observer faults are observational only;
                            # KeyboardInterrupt/cancellation must still escape.
                            pass
                        result = frame = None
                        continue
                    if (not isinstance(result, dict) or set(result) != {"ok", "text"}
                            or type(result.get("ok")) is not bool or result["ok"] is not True
                            or not isinstance(result["text"], str) or len(result["text"]) > 4096
                            or any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 for c in result["text"])):
                        raise RuntimeError("transcription_failed")
                    if diagnostic is not None and result["ok"] is True and not diagnostic_seen:
                        raise RuntimeError("transcription_failed")
                    return result["text"]
                if self.process is None or not self.process.is_alive():
                    raise RuntimeError("transcription_failed")
        except BaseException:
            self.stop()
            raise

    def _check_cancelled(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            self.stop()
            raise RuntimeError("cancelled")

    def stop(self) -> None:
        process, connection = self.process, self.connection
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        child_connection, self._child_connection = self._child_connection, None
        if child_connection is not None:
            try:
                child_connection.close()
            except OSError:
                pass
        if process is not None:
            try:
                alive = process.is_alive()
            except (AssertionError, ValueError):  # process.start() never completed
                alive = False
            if alive:
                process.terminate()
            try:
                process.join(timeout=2.0)
            except (AssertionError, ValueError):
                pass
            try:
                alive = process.is_alive()
            except (AssertionError, ValueError):
                alive = False
            if alive:
                process.kill()
                process.join(timeout=2.0)
                alive = process.is_alive()
            if not alive:
                self.process = self.connection = None
            else:
                # Keep ownership and block reuse if OS cleanup did not finish.
                self.process, self.connection = process, connection
        else:
            self.process = self.connection = None

    close = stop

    def __enter__(self) -> "SpeechProcess":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()


__all__ = ["SpeechProcess", "MAX_PCM_BYTES", "MAX_SAMPLES"]
