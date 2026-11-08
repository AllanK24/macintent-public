"""Focused speech-runtime tests using constants, mocks, and pure PCM only."""

import threading
import time
import struct
from types import SimpleNamespace

import pytest

from macintent import asr_protocol, speech, speech_worker


def test_pinned_parakeet_declaration_is_exact_and_local_only():
    spec = speech.PARAKEET_SPEC
    assert (spec.name, spec.repo_id, spec.revision, spec.directory) == (
        "parakeet",
        "mlx-community/parakeet-tdt-0.6b-v2",
        "8ae155301e23d820d82aa60d24817c900e69e487",
        "parakeet-v2",
    )
    assert spec.required_files == (
        "config.json", "model.safetensors", "tokenizer.model", "tokenizer.vocab", "vocab.txt",
    )
    records = {record["name"]: record for record in speech.PARAKEET_FILES}
    assert tuple(records) == spec.required_files
    assert records["config.json"] == {
        "name": "config.json", "size": 36176,
        "blob_id": "8955c588b5549ef70811f2121c6c8bda33508992",
    }
    assert records["model.safetensors"] == {
        "name": "model.safetensors", "size": 2471559904,
        "sha256": "b958c37a6baa6874a279108755c8f2818e27bf647d72d54800a234a421341dfe",
    }
    assert records["tokenizer.model"] == {
        "name": "tokenizer.model", "size": 251396,
        "sha256": "5a3a82c48998709f9bc9f5db2924d99a899d20bf82550ca52272da7c7557b0a0",
    }
    assert records["tokenizer.vocab"] == {
        "name": "tokenizer.vocab", "size": 10393,
        "blob_id": "d3ce5dea5479a740d2bd5009cf4c86be2d0a6eae",
    }
    assert records["vocab.txt"] == {
        "name": "vocab.txt", "size": 5066,
        "blob_id": "18f79afd579e1b06fecae25f4e0d7c2b404a2aaf",
    }


def test_public_text_extraction_never_uses_an_object_repr():
    assert asr_protocol.public_text_from_result("synthetic speech") == "synthetic speech"
    assert asr_protocol.public_text_from_result(["synthetic speech", "ignored"]) == "synthetic speech"
    assert asr_protocol.public_text_from_result([]) == ""
    assert asr_protocol.public_text_from_result(SimpleNamespace(text="synthetic speech")) == "synthetic speech"
    assert asr_protocol.public_text_from_result(object()) == ""


def test_backend_diagnostic_accepts_the_public_shape_and_bounds_separately():
    class OneArgument:
        def transcribe(self, samples):
            assert samples == [.1, -.1]
            return "  synthetic speech  "

    class TwoArguments:
        def transcribe(self, samples, sample_rate):
            assert samples == [.1, -.1] and sample_rate == 16000
            return SimpleNamespace(text="synthetic speech")

    assert asr_protocol.invoke_backend_diagnostic(OneArgument(), [.1, -.1]) == (
        "  synthetic speech  ", "synthetic speech",
    )
    assert asr_protocol.invoke_backend_diagnostic(TwoArguments(), [.1, -.1]) == (
        "synthetic speech", "synthetic speech",
    )
    with pytest.raises(TypeError):
        asr_protocol.invoke_backend_diagnostic(lambda samples: object(), [.1])


def test_speech_engine_preflight_and_explicit_pinned_backend(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(speech, "local_parakeet_path", lambda root: called.append(root))
    monkeypatch.setattr(speech.SpeechProcess, "start", lambda self: called.append("start"))
    worker = speech.SpeechEngine(tmp_path)
    assert worker.backend_factory.func is speech.ParakeetOnlyBackend
    assert worker.backend_factory.args == (tmp_path,)
    worker.start()
    assert called == [tmp_path, "start"]


@pytest.mark.parametrize("cancel", [False, True])
def test_parent_deadline_also_bounds_a_blocked_transport_send(tmp_path, monkeypatch, cancel):
    released = threading.Event()
    stop_event = threading.Event()

    def blocked(self, pcm):
        if cancel:
            stop_event.set()
        released.wait(5)
        return "synthetic late transcript"

    monkeypatch.setattr(speech.SpeechProcess, "transcribe", blocked)
    monkeypatch.setattr(speech.SpeechProcess, "stop", lambda self: released.set())
    worker = speech.SpeechEngine(tmp_path, stop_event=stop_event)
    worker.request_timeout = .08
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cancelled" if cancel else "transcription_failed"):
        worker.transcribe(b"\0\0\0\0")
    assert time.monotonic() - started < 1 and released.is_set()
    assert worker._request_thread is None
    with pytest.raises(RuntimeError):
        worker.transcribe(b"\0\0\0\0")


@pytest.mark.parametrize("pcm", [b"", b"\0", b"\0" * (1_920_004), b"\0\0\x80\x7f"])
def test_transport_rejects_empty_misaligned_oversize_and_nonfinite_pcm(tmp_path, pcm):
    worker = speech_worker.SpeechProcess(tmp_path)
    with pytest.raises(ValueError, match="invalid_audio"):
        worker.transcribe(pcm)


def test_child_refuses_to_start_without_an_explicit_backend_factory(monkeypatch):
    class Connection:
        def __init__(self):
            self.sent = []
            self.closed = False

        def send(self, value):
            self.sent.append(value)

        def close(self):
            self.closed = True

    monkeypatch.setattr(speech_worker.os, "open", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(speech_worker.os, "dup2", lambda *_args: None)
    monkeypatch.setattr(speech_worker.os, "close", lambda *_args: None)
    connection = Connection()
    speech_worker._worker(connection, "/synthetic-root", None)
    assert connection.sent == [{"ready": False, "code": "model_unavailable"}]
    assert connection.closed is True


def test_worker_normal_pcm_path_uses_the_local_result_protocol(monkeypatch):
    class Backend:
        def transcribe(self, samples, sample_rate):
            assert samples.shape == (2,) and sample_rate == 16000
            return "  synthetic child transcript  "

    class Connection:
        def __init__(self):
            self.requests = iter([{"pcm": struct.pack("<ff", .1, -.1)}, {"stop": True}])
            self.sent = []
            self.closed = False

        def recv(self):
            return next(self.requests)

        def send(self, value):
            self.sent.append(value)

        def close(self):
            self.closed = True

    monkeypatch.setattr(speech_worker.os, "open", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(speech_worker.os, "dup2", lambda *_args: None)
    monkeypatch.setattr(speech_worker.os, "close", lambda *_args: None)
    connection = Connection()
    speech_worker._worker(connection, "/synthetic-root", Backend)
    assert connection.sent == [{"ready": True}, {"ok": True, "text": "synthetic child transcript"}]
    assert connection.closed
