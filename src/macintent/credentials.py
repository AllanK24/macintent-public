"""Private, project-local Groq credential access and one-time key migration.

All filesystem traversal is descriptor-anchored and rejects symbolic links.
The migration accepts only one plain Groq API key and never reports it.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat


KEY_RELATIVE_PATH = ".local/state/keys/groq-api-key"

_KEY_BYTES = re.compile(rb"gsk_[A-Za-z0-9_-]{16,252}\Z")
_SOURCE_BYTES = re.compile(rb"(?P<key>gsk_[A-Za-z0-9_-]{16,252})(?:\r?\n)?\Z")
_ASSIGNMENT_BYTES = re.compile(
    rb"GROQ_API_KEY[ \t]*=[ \t]*(?P<quote>['\"]?)(?P<key>gsk_[A-Za-z0-9_-]{16,252})(?P=quote)[ \t]*(?:\r?\n)?\Z"
)
_MAX_SOURCE_BYTES = 1024
_MAX_KEY_BYTES = 256
_DIRECTORY_MODE = 0o700
_KEY_MODE = 0o600


class CredentialError(ValueError):
    """Static credential failure with no filesystem or secret-bearing detail."""

    def __init__(self) -> None:
        self.code = "credential_unavailable"
        super().__init__(self.code)


def _raise_unavailable() -> None:
    raise CredentialError() from None


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _file_flags(access: int) -> int:
    return access | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)


def _open_root(root: Path) -> int:
    if not isinstance(root, Path) or not root.is_absolute() or ".." in root.parts:
        _raise_unavailable()
    parts = root.parts[1:]
    if not parts or any(part in {"", ".", ".."} or "/" in part for part in parts):
        _raise_unavailable()

    current = os.open("/", _directory_flags())
    try:
        for component in parts:
            next_fd = os.open(component, _directory_flags(), dir_fd=current)
            os.close(current)
            current = next_fd
        root_stat = os.fstat(current)
        if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
            _raise_unavailable()
        return current
    except BaseException:
        os.close(current)
        raise


def _open_directory(parent_fd: int, name: str, *, create: bool = False, private: bool = False) -> int:
    created = False
    if create:
        try:
            os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)
            created = True
            # Commit the new ancestor entry before removing any source secret.
            os.fsync(parent_fd)
        except FileExistsError:
            pass
    fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            _raise_unavailable()
        if private:
            if created:
                os.fchmod(fd, _DIRECTORY_MODE)
                info = os.fstat(fd)
            # A readiness read must not repair/change existing private state.
            if stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE:
                _raise_unavailable()
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_key_directories(root_fd: int, *, create: bool) -> int:
    local_fd = _open_directory(root_fd, ".local", create=create, private=True)
    state_fd = -1
    try:
        state_fd = _open_directory(local_fd, "state", create=create, private=True)
        keys_fd = _open_directory(state_fd, "keys", create=create, private=True)
        return keys_fd
    finally:
        os.close(local_fd)
        if state_fd >= 0:
            os.close(state_fd)


def _stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_uid, info.st_nlink,
        stat.S_IMODE(info.st_mode), info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def _validate_regular(info: os.stat_result, *, mode: int | None, max_size: int) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_size < 0 or info.st_size > max_size):
        _raise_unavailable()
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        _raise_unavailable()


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = os.read(fd, min(4096, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > limit:
        _raise_unavailable()
    return b"".join(chunks)


def _read_key_file(parent_fd: int, name: str) -> tuple[str, tuple[int, ...]]:
    fd = os.open(name, _file_flags(os.O_RDONLY), dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        _validate_regular(before, mode=_KEY_MODE, max_size=_MAX_KEY_BYTES)
        raw = _read_bounded(fd, _MAX_KEY_BYTES)
        after = os.fstat(fd)
        if _stat_identity(before) != _stat_identity(after) or len(raw) != after.st_size or not _KEY_BYTES.fullmatch(raw):
            _raise_unavailable()
        return raw.decode("ascii"), _stat_identity(after)
    finally:
        os.close(fd)


def _read_source(root_fd: int, *, prepare_permissions: bool) -> tuple[str, bytes, tuple[int, ...]]:
    fd = os.open("api.txt", _file_flags(os.O_RDONLY), dir_fd=root_fd)
    try:
        before = os.fstat(fd)
        _validate_regular(before, mode=None, max_size=_MAX_SOURCE_BYTES)
        if prepare_permissions and stat.S_IMODE(before.st_mode) != _KEY_MODE:
            os.fchmod(fd, _KEY_MODE)
        after_chmod = os.fstat(fd)
        _validate_regular(after_chmod, mode=_KEY_MODE, max_size=_MAX_SOURCE_BYTES)
        raw = _read_bounded(fd, _MAX_SOURCE_BYTES)
        after_read = os.fstat(fd)
        identity = _stat_identity(after_chmod)
        if (identity != _stat_identity(after_read) or len(raw) != after_read.st_size
                or not _source_match(raw)):
            _raise_unavailable()
        match = _source_match(raw)
        assert match is not None
        return match.group("key").decode("ascii"), raw, identity
    finally:
        os.close(fd)


def _source_match(raw: bytes) -> re.Match[bytes] | None:
    """One literal key or assignment, never sourced/interpolated/executed."""
    return _SOURCE_BYTES.fullmatch(raw) or _ASSIGNMENT_BYTES.fullmatch(raw)


def _write_all(fd: int, value: bytes) -> None:
    written = 0
    while written < len(value):
        amount = os.write(fd, value[written:])
        if amount <= 0:
            _raise_unavailable()
        written += amount


def _remove_created_key(parent_fd: int, name: str, identity: tuple[int, ...] | None) -> None:
    if identity is None:
        return
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        current_identity = _stat_identity(current)
        if current_identity[:5] == identity[:5] and stat.S_ISREG(current.st_mode):
            os.unlink(name, dir_fd=parent_fd)
    except OSError:
        pass


def _write_or_verify_key(parent_fd: int, key: str) -> None:
    name = "groq-api-key"
    try:
        fd = os.open(name, _file_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), _KEY_MODE, dir_fd=parent_fd)
    except FileExistsError:
        existing, _ = _read_key_file(parent_fd, name)
        if existing != key:
            _raise_unavailable()
        return

    created_identity: tuple[int, ...] | None = None
    try:
        created = os.fstat(fd)
        created_identity = _stat_identity(created)
        _validate_regular(created, mode=None, max_size=_MAX_KEY_BYTES)
        os.fchmod(fd, _KEY_MODE)
        created = os.fstat(fd)
        _validate_regular(created, mode=_KEY_MODE, max_size=_MAX_KEY_BYTES)
        created_identity = _stat_identity(created)
        payload = key.encode("ascii")
        _write_all(fd, payload)
        os.fsync(fd)
        written = os.fstat(fd)
        if (written.st_dev, written.st_ino, written.st_uid, written.st_nlink) != (
                created.st_dev, created.st_ino, created.st_uid, created.st_nlink):
            _raise_unavailable()
    except BaseException:
        os.close(fd)
        _remove_created_key(parent_fd, name, created_identity)
        raise
    else:
        os.close(fd)

    # Read back through a fresh no-follow descriptor before the source can be removed.
    verified, verified_identity = _read_key_file(parent_fd, name)
    if verified != key or verified_identity[0:2] != created_identity[0:2]:
        _remove_created_key(parent_fd, name, created_identity)
        _raise_unavailable()
    os.fsync(parent_fd)


def _unlink_unchanged_source(root_fd: int, key: str, expected_identity: tuple[int, ...]) -> None:
    current_key, current_raw, current_identity = _read_source(root_fd, prepare_permissions=False)
    if current_key != key or current_identity != expected_identity:
        _raise_unavailable()
    current = os.stat("api.txt", dir_fd=root_fd, follow_symlinks=False)
    if _stat_identity(current) != expected_identity:
        _raise_unavailable()
    # Confirm exact bytes as well as the original inode/metadata immediately before unlink.
    match = _source_match(current_raw)
    if match is None or match.group("key").decode("ascii") != key:
        _raise_unavailable()
    os.unlink("api.txt", dir_fd=root_fd)
    os.fsync(root_fd)


def _read_api_key(root: Path) -> str:
    root_fd = _open_root(root)
    try:
        keys_fd = _open_key_directories(root_fd, create=False)
        try:
            key, _ = _read_key_file(keys_fd, "groq-api-key")
            return key
        finally:
            os.close(keys_fd)
    finally:
        os.close(root_fd)


def read_api_key(root: Path) -> str:
    """Read the validated project-local key without printing or logging it."""
    failed = False
    key = ""
    try:
        key = _read_api_key(root)
    except Exception:
        failed = True
    if failed:
        raise CredentialError()
    return key


def _import_api_key(root: Path) -> None:
    root_fd = _open_root(root)
    try:
        key, _raw, source_identity = _read_source(root_fd, prepare_permissions=True)
        keys_fd = _open_key_directories(root_fd, create=True)
        try:
            _write_or_verify_key(keys_fd, key)
            # Reopen and validate the committed destination before removing the input.
            if _read_key_file(keys_fd, "groq-api-key")[0] != key:
                _raise_unavailable()
        finally:
            os.close(keys_fd)
        _unlink_unchanged_source(root_fd, key, source_identity)
    finally:
        os.close(root_fd)


def import_api_key(root: Path) -> None:
    """Safely migrate a single root ``api.txt`` key into the private key store."""
    failed = False
    try:
        _import_api_key(root)
    except Exception:
        failed = True
    if failed:
        raise CredentialError()
