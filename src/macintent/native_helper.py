"""Locate the fixed MacIntent native helper and build its minimal environment.

The helper bundle path and executable identity are part of the existing local
permission boundary. This module does not expose general desktop actions or an
arbitrary native-operation protocol.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any


class NativeHelperError(RuntimeError):
    def __init__(self, code: str = "native_unavailable") -> None:
        self.code = code if code == "native_unavailable" else "native_unavailable"
        super().__init__(self.code)


class NativeBridge:
    """Fixed executable locator and strict JSON decoding helpers."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(os.path.abspath(root))

    def _child_environment(self) -> dict[str, str]:
        env = {"PATH": "/usr/bin:/bin", "MACINTENT_ROOT": str(self.root)}
        try:
            root_st = os.lstat(self.root)
        except OSError as exc:
            raise NativeHelperError("native_unavailable") from exc
        if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
            raise NativeHelperError("native_unavailable")
        tmpdir = self.root / ".cache" / "tmp"
        try:
            tmp_st = os.lstat(tmpdir)
        except FileNotFoundError:
            tmp_st = None
        except OSError as exc:
            raise NativeHelperError("native_unavailable") from exc
        if tmp_st is not None:
            if stat.S_ISLNK(tmp_st.st_mode) or not stat.S_ISDIR(tmp_st.st_mode) or tmp_st.st_uid != os.geteuid():
                raise NativeHelperError("native_unavailable")
            env["TMPDIR"] = str(tmpdir)
        return env

    def _executable(self) -> Path:
        project = self.root / ".local" / "native" / "VoiceAgent.app" / "Contents" / "MacOS" / "voice-native"
        try:
            current = self.root
            root_st = os.lstat(current)
            if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
                raise NativeHelperError("native_unavailable")
            for component in (".local", "native", "VoiceAgent.app", "Contents", "MacOS"):
                current = current / component
                st = os.lstat(current)
                if (stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode)
                        or st.st_uid != os.geteuid() or st.st_mode & 0o022):
                    raise NativeHelperError("native_unavailable")
            st = os.lstat(project)
        except OSError as exc:
            raise NativeHelperError("native_unavailable") from exc
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.geteuid() or not st.st_mode & 0o111:
            raise NativeHelperError("native_unavailable")
        return project

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    @staticmethod
    def _reject_constant(value: str) -> None:
        raise ValueError("invalid JSON constant")


__all__ = ["NativeBridge", "NativeHelperError"]
