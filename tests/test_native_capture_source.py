"""Static checks for the public capture-only native helper.

These inspect source, manifest, and build-script contracts only. They do not
launch Swift, query audio devices, request permissions, or run a build.
"""

from __future__ import annotations

import plistlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native/VoiceNative.swift"
MANIFEST = ROOT / "native/Info.plist"
BUILD = ROOT / "scripts/build-native"


def test_helper_exposes_only_the_fixed_capture_command():
    source = SOURCE.read_text(encoding="utf-8")
    operations = set(re.findall(r'case "([a-z_]+)":', source))
    assert operations == {"capture_command"}
    for legacy in (
        "launch_textedit",
        "open_textedit",
        "inspect_output_volume",
        "set_output_volume",
        "capture",
        "diagnose_microphone",
        "notice",
        "confirm",
    ):
        assert f'"{legacy}"' not in source
    assert source.count('case "capture_command":') == 2
    assert 'exactInt(args["max_seconds"]) == 30' in source
    assert "private func captureCommand()" in source
    assert "private func commandCaptureResult(" in source


def test_unrelated_desktop_capture_and_path_helpers_are_absent():
    source = SOURCE.read_text(encoding="utf-8")
    for token in (
        "rootURL(",
        "validatedNote(",
        "runAlert(",
        "showNotice(",
        "private func confirm(",
        "private func capture(",
        "DiagnosticLevels",
        "CallbackGate",
        "workspaceCompletion",
        "NSWorkspace",
        "NSAlert",
        "AudioObject",
        "kAudioDeviceProperty",
        "invalid_path",
        "invalid_plaintext",
        "device_changed",
        "import CoreAudio",
        "import AudioToolbox",
    ):
        assert token not in source


def test_validate_exits_before_application_device_or_permission_work():
    source = SOURCE.read_text(encoding="utf-8")
    main = source.index("let arguments = CommandLine.arguments")
    validate = source.index("if validateOnly {")
    app = source.index("_ = NSApplication.shared")
    execute = source.index("let result = try execute(op, args)")
    assert validate < app < execute
    assert "AVCaptureDevice.authorizationStatus" not in source[main:validate]
    assert "captureCommand(" not in source[main:validate]
    assert "AVAudioEngine(" not in source[main:validate]
    assert source.count("AVCaptureDevice.authorizationStatus") == 1
    assert source.count('output(["ok": true, "result": ["valid": true]])') == 1


def test_capture_detector_contract_is_preserved():
    source = SOURCE.read_text(encoding="utf-8")
    expected = (
        "private struct CommandCaptureState",
        "private func commandCaptureSelfTest()",
        "private func commandBackgroundSelfTest()",
        "static let sampleRate = 16_000",
        "static let frameSamples = 320",
        "static let calibrationSamples = 4_800",
        "static let prerollLimit = 8_000",
        "static let startVoiceSamples = 2_560",
        "static let candidateGapSamples = 1_600",
        "static let trailingQuietSamples = 19_200",
        "static let noSpeechSeconds = 10.0",
        "static let startupSeconds = 5.0",
        "static let sessionSeconds = 30.0",
        "static let sampleLimit = 480_000",
        "startThreshold = max(0.0025, noiseFloor * 2.5)",
        "endThreshold = max(0.0012, noiseFloor * 1.5)",
        'output(["event": "ready"])',
        "finished.wait(timeout: .now() + 36)",
        "repeating: .milliseconds(25)",
    )
    for token in expected:
        assert token in source
    assert "private let requestLimit = 32 * 1024" in source
    assert "private let responseLimit = 3 * 1024 * 1024" in source
    assert "private let pcmLimit = 1_920_000" in source


def test_manifest_keeps_microphone_identity_and_public_wording():
    manifest = plistlib.loads(MANIFEST.read_bytes())
    assert manifest["CFBundleExecutable"] == "voice-native"
    assert manifest["CFBundleIdentifier"] == "dev.asr-agent.voice-native"
    name = manifest["CFBundleName"]
    assert "MacIntent" in name and "ASR" not in name
    assert manifest["LSUIElement"] is True
    description = manifest["NSMicrophoneUsageDescription"]
    assert "MacIntent" in description and "microphone" in description


def test_build_script_uses_public_launcher_and_guarded_output_path():
    build = BUILD.read_text(encoding="utf-8")
    assert build.startswith("#!/bin/bash\n")
    assert 'exec /bin/bash "$ROOT/scripts/dev" /bin/bash "$ROOT/scripts/build-native" "$@"' in build
    assert "MACINTENT_LAUNCHER_ACTIVE" in build
    assert '"${MACINTENT_ROOT:-}" != "$ROOT"' in build
    assert "ASR_AGENT" not in build
    assert "build-voice-native" not in build
    assert 'base="$ROOT/.local/native"' in build
    assert 'app="$base/VoiceAgent.app"' in build
    assert 'binary="$contents/MacOS/voice-native"' in build
    assert "Identifier=dev.asr-agent.voice-native" in build
    assert "/usr/bin/xcrun --find swiftc" in build
    assert "/usr/bin/xcrun --show-sdk-path" in build
    assert "/usr/bin/codesign --force --sign -" in build
    assert "module-cache" in build


def test_public_helper_sources_contain_no_private_artifact_references():
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (SOURCE, MANIFEST, BUILD)
    ).casefold()
    for term in ("/users/", "project report"):
        assert term not in combined
