"""Parakeet-only speech runtime for MacIntent's local command transcription.

The loaded backend runs only in the owned ASR child. It has no microphone or
action authority, and it never falls back to a remote model provider.
"""

from __future__ import annotations

from functools import partial
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any

from .model_files import ModelSpec, _validate_local_snapshot
from .paths import confined_path, launcher_workspace, workspace_root
from .speech_worker import SpeechProcess


PARAKEET_SPEC = ModelSpec(
    "parakeet", "mlx-community/parakeet-tdt-0.6b-v2",
    "8ae155301e23d820d82aa60d24817c900e69e487", "parakeet-v2",
    ("config.json", "model.safetensors", "tokenizer.model", "tokenizer.vocab", "vocab.txt"),
)
# Checksums are pinned in source. Never consult an unrelated model manifest or
# snapshot just to transcribe a MacIntent command.
PARAKEET_FILES = (
    {"name": "config.json", "size": 36176, "blob_id": "8955c588b5549ef70811f2121c6c8bda33508992"},
    {"name": "model.safetensors", "size": 2471559904, "sha256": "b958c37a6baa6874a279108755c8f2818e27bf647d72d54800a234a421341dfe"},
    {"name": "tokenizer.model", "size": 251396, "sha256": "5a3a82c48998709f9bc9f5db2924d99a899d20bf82550ca52272da7c7557b0a0"},
    {"name": "tokenizer.vocab", "size": 10393, "blob_id": "d3ce5dea5479a740d2bd5009cf4c86be2d0a6eae"},
    {"name": "vocab.txt", "size": 5066, "blob_id": "18f79afd579e1b06fecae25f4e0d7c2b404a2aaf"},
)


def local_parakeet_path(root: Path) -> Path:
    root = workspace_root(root)
    _validate_local_snapshot(root, PARAKEET_SPEC, {"files": list(PARAKEET_FILES)})
    return confined_path(root, "models/parakeet-v2", must_exist=True)


class ParakeetOnlyBackend:
    """Loaded only in the owned ASR child; no microphone or action authority."""

    def __init__(self, root: Path) -> None:
        root = workspace_root(root)
        launcher_root, hf_home = launcher_workspace()
        if root != launcher_root:
            raise RuntimeError("model_unavailable")
        path = local_parakeet_path(root)
        os.environ["HF_HOME"] = str(hf_home)
        os.environ["HF_HUB_CACHE"] = str(hf_home / "hub")
        os.environ["TRANSFORMERS_CACHE"] = str(hf_home / "transformers")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        import mlx.core as mx
        from parakeet_mlx import from_pretrained

        self.model = from_pretrained(str(path), dtype=mx.bfloat16, cache_dir=str(hf_home))
        mx.eval(self.model.parameters())
        self._mx = mx

    def transcribe(self, samples: Any, sample_rate: int = 16000) -> str:
        if sample_rate != 16000:
            raise ValueError("invalid_audio")
        from parakeet_mlx.audio import get_logmel

        mel = get_logmel(self._mx.array(samples, dtype=self._mx.float32), self.model.preprocessor_config)
        result = self.model.generate(mel)
        self._mx.eval(mel)
        if isinstance(result, (list, tuple)):
            result = result[0] if result else ""
        text = result if isinstance(result, str) else getattr(result, "text", None)
        if not isinstance(text, str):
            raise RuntimeError("transcription_failed")
        return text


class SpeechEngine(SpeechProcess):
    """Reuse bounded ASR ownership/PCM transport with an explicit pinned backend."""

    def __init__(self, root: Path, *, stop_event: Any = None) -> None:
        self._speech_root = Path(root)
        self._cleanup_lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._request_thread = None
        self._asr_failed = False
        super().__init__(root, backend_factory=partial(ParakeetOnlyBackend, Path(root)), stop_event=stop_event)

    def start(self) -> None:
        local_parakeet_path(self._speech_root)
        super().start()

    def transcribe(self, pcm: bytes) -> str:
        # The transport send is blocking. A parent-side deadline covers the
        # complete send/receive operation too, not only decoding after send.
        if self._asr_failed or not self._request_lock.acquire(blocking=False):
            raise RuntimeError("transcription_failed")
        results: queue.Queue = queue.Queue(maxsize=1)
        def request() -> None:
            try:
                text = super(SpeechEngine, self).transcribe(pcm)
                results.put((True, text))
            except BaseException:
                results.put((False, None))
        thread = threading.Thread(target=request, daemon=True)
        self._request_thread = thread
        try:
            thread.start()
            deadline = time.monotonic() + self.request_timeout
            while True:
                if self.stop_event is not None and self.stop_event.is_set():
                    raise RuntimeError("cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("transcription_failed")
                try:
                    ok, text = results.get(timeout=min(.05, remaining))
                except queue.Empty:
                    continue
                if not ok or not isinstance(text, str):
                    raise RuntimeError("transcription_failed")
                return text
        except BaseException:
            self._asr_failed = True
            self.stop()
            raise
        finally:
            if thread.ident is not None:
                thread.join(timeout=3)
            if not thread.is_alive():
                self._request_thread = None
            else:
                self._asr_failed = True
            self._request_lock.release()
            if thread.is_alive():
                raise RuntimeError("transcription_failed")

    def stop(self) -> None:
        with self._cleanup_lock:
            super().stop()
        thread = self._request_thread
        if thread is not None and thread is not threading.current_thread() and thread.ident is not None:
            thread.join(timeout=3)

    def close(self) -> None:
        self.stop()
        if self._request_thread is not None and self._request_thread.is_alive():
            raise RuntimeError("transcription_failed")


__all__ = [
    "PARAKEET_FILES",
    "PARAKEET_SPEC",
    "ParakeetOnlyBackend",
    "SpeechEngine",
    "local_parakeet_path",
]
