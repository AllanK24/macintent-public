"""Synthetic pipe/PCM tests: no microphone or native capture is executed."""

import base64
import json
from pathlib import Path
import struct
import sys
import threading

import pytest

from macintent.capture import CaptureError, CapturedCommand, CommandMicrophone, _Frames, validate_command_capture
from macintent.native_helper import NativeBridge


def reply(reason="silence"):
    pcm = struct.pack("<ff", .2, -.2) if reason != "no_speech" else b""
    return {"sample_rate": 16000, "pcm_f32le": base64.b64encode(pcm).decode(),
            "audio_seconds": len(pcm) / 64000, "reason": reason}


def packet(value):
    return (json.dumps(value) + "\n").encode()


def test_fragmented_ready_and_result_have_one_ordered_callback():
    seen = []
    frames = _Frames(lambda: seen.append("ready"))
    data = packet({"event": "ready"}) + packet({"ok": True, "result": reply()})
    for byte in data:
        frames.feed(bytes([byte]))
    result = frames.finish()
    assert seen == ["ready"] and isinstance(result, CapturedCommand)
    assert result.pcm == struct.pack("<ff", .2, -.2)


@pytest.mark.parametrize("data", [
    packet({"ok": True, "result": reply()}),
    packet({"event": "ready"}) * 2,
    packet({"event": "ready", "extra": True}),
    b'{"event":"ready","event":"ready"}\n',
    packet({"event": "ready"}) + packet({"ok": True, "result": reply()}) + b'{}\n',
    b'{"ok":true,"result":NaN}\n',
    b'x' * (3 * 1024 * 1024 + 1),
])
def test_malformed_duplicate_out_of_order_and_oversize_frames_fail_closed(data):
    with pytest.raises(CaptureError):
        frames = _Frames(lambda: None)
        frames.feed(data)
        frames.finish()


def test_unterminated_result_and_error_text_never_become_success():
    frames = _Frames(lambda: None)
    frames.feed(packet({"event": "ready"}) + json.dumps({"ok": True, "result": reply()}).encode())
    with pytest.raises(CaptureError):
        frames.finish()
    with pytest.raises(CaptureError, match="permission_denied"):
        _Frames(None).feed(packet({"ok": False, "error": "permission_denied"}))
    with pytest.raises(CaptureError, match="invalid_capture"):
        _Frames(None).feed(packet({"ok": False, "error": "private-provider-payload"}))


@pytest.mark.parametrize("change", [
    {"reason": "unknown"}, {"reason": "no_speech"}, {"sample_rate": True},
    {"audio_seconds": 30.1}, {"pcm_f32le": "%%%"}, {"extra": "private"},
    {"pcm_f32le": base64.b64encode(struct.pack("<f", float("nan"))).decode(), "audio_seconds": 1 / 16000},
])
def test_capture_shape_and_pcm_remain_strict(change):
    with pytest.raises(CaptureError):
        validate_command_capture({**reply(), **change})
    assert validate_command_capture(reply("no_speech")).pcm is None


def test_child_environment_is_minimal_and_uses_macintent_root(tmp_path):
    (tmp_path / ".cache" / "tmp").mkdir(parents=True)
    environment = NativeBridge(tmp_path)._child_environment()
    assert environment == {
        "PATH": "/usr/bin:/bin",
        "MACINTENT_ROOT": str(tmp_path),
        "TMPDIR": str(tmp_path / ".cache" / "tmp"),
    }


def _child(root: Path, body: str):
    helper = root / ".local/native/VoiceAgent.app/Contents/MacOS/voice-native"
    helper.parent.mkdir(parents=True)
    helper.write_text(f"#!{sys.executable}\n{body}\n")
    helper.chmod(0o700)
    return helper


def test_real_synthetic_child_check_does_not_capture_and_capture_closes(tmp_path):
    result_json = json.dumps(reply())
    _child(tmp_path, f"""
import json, sys
request = json.loads(sys.stdin.read())
assert request == {{'op':'capture_command','arguments':{{'max_seconds':30}}}}
if sys.argv[1:] == ['--validate']:
    print(json.dumps({{'ok':True,'result':{{'valid':True}}}}))
else:
    print(json.dumps({{'event':'ready'}}), flush=True)
    print(json.dumps({{'ok':True,'result':{result_json}}}), flush=True)
""")
    microphone = CommandMicrophone(tmp_path)
    microphone.check()
    ready = []
    assert microphone.capture(lambda: ready.append(True)).reason == "silence"
    assert ready == [True]


def test_synthetic_timeout_and_readiness_cancellation_reap_owned_child(tmp_path, monkeypatch):
    import macintent.capture as module
    _child(tmp_path, "import json, sys, time; sys.stdin.read(); print(json.dumps({'event':'ready'}), flush=True); time.sleep(30)")
    original = module.subprocess.Popen
    children = []
    def tracked(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", tracked)
    with pytest.raises(CaptureError, match="capture_timeout"):
        CommandMicrophone(tmp_path, timeout=.15).capture(lambda: None)
    assert children[-1].poll() is not None
    def interrupt():
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        CommandMicrophone(tmp_path).capture(interrupt)
    assert children[-1].poll() is not None


def test_already_cancelled_request_never_starts_child(tmp_path, monkeypatch):
    import macintent.capture as module
    event = threading.Event()
    event.set()
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_a, **_k: pytest.fail("child started"))
    with pytest.raises(CaptureError, match="cancelled"):
        CommandMicrophone(tmp_path, stop_event=event).capture(lambda: None)


def test_check_rejects_old_helper_without_arming(tmp_path):
    _child(tmp_path, "import sys; assert sys.argv[1:] == ['--validate']; sys.stdin.read(); print('{\"ok\":false,\"error\":\"invalid_request\"}')")
    with pytest.raises(CaptureError, match="helper_outdated"):
        CommandMicrophone(tmp_path).check()
