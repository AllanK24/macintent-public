"""The deliberately small, descriptor-anchored workspace action policy.

This module contains no shell or general filesystem interface.  Paths accepted
by the action broker are names in ``demo/workspace`` and are opened relative to
directory descriptors.  The extra checks are intentional: this is a policy
boundary, not a convenience wrapper around :mod:`pathlib`.

``create_note`` additionally accepts model-authored text.  That body is bounded,
plain, and control-character free.  Trusted clients receive it back for exact
confirmation, but persistence uses only the destination projection from
:meth:`PreparedAction.audited_arguments`.
"""

from __future__ import annotations

import errno
import math
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class WorkspaceActionError(Exception):
    """A static, safe-to-return action policy error."""

    def __init__(self, code: str = "policy_denied") -> None:
        super().__init__(code)
        self.code = code


_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
_BASE = ("demo", "workspace")
_MAX_COPY = 8 * 1024 * 1024
_MAX_ENTRIES = 200
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
# Notes are plain text only: one short, literal block with no control
# characters, so a note can never carry a terminal escape sequence, a NUL, or
# C0/C1 framing controls.
_NOTE_NAME = re.compile(r"^.+\.txt$")
_MAX_NOTE_CODE_POINTS = 1024
_MAX_NOTE_BYTES = 4096
# Newline is the one control character a note may contain.  NUL, the rest of
# C0, DEL, and all of C1 are refused.
_ALLOWED_NOTE_CONTROLS = frozenset({0x0A})
# Action kinds whose real effect the trusted user must confirm first.
_CONFIRMED_KINDS = frozenset({"copy", "note", "delete"})


@dataclass(frozen=True)
class FileIdentity:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int
    mode: int


@dataclass(frozen=True)
class DirIdentity:
    dev: int
    ino: int


@dataclass(frozen=True)
class PreparedAction:
    tool: str
    arguments: dict[str, str]
    summary: str
    kind: str
    path_parts: tuple[str, ...]
    parent_parts: tuple[str, ...] | None
    source_parts: tuple[str, ...] | None
    destination_parts: tuple[str, ...] | None
    root: DirIdentity
    base: DirIdentity
    target: DirIdentity | None
    parent: DirIdentity | None
    source: FileIdentity | None
    source_parent: DirIdentity | None
    destination_parent: DirIdentity | None
    #: Validated UTF-8 note body, held in memory only for ``create_note``.
    note_bytes: bytes | None = None

    @property
    def requires_confirmation(self) -> bool:
        """Whether the trusted user must approve this action first.

        The broker asks this one question instead of naming tool kinds, so a
        future write tool joins the confirmed set by policy, not by editing
        every check in the broker.
        """

        return self.kind in _CONFIRMED_KINDS

    def audited_arguments(self) -> dict[str, str]:
        """The argument projection that is safe to persist.

        A note body is model-authored private text, so only its destination is
        returned here.  Every other tool is fully described by its paths.
        """

        if self.kind == "note":
            return {"path": self.arguments["path"]}
        return dict(self.arguments)


def _identity(st: os.stat_result) -> DirIdentity:
    return DirIdentity(st.st_dev, st.st_ino)


def _file_identity(st: os.stat_result) -> FileIdentity:
    return FileIdentity(
        st.st_dev,
        st.st_ino,
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
        st.st_nlink,
        st.st_mode,
    )


def _same_dir(a: DirIdentity | None, b: os.stat_result) -> bool:
    return a is not None and a.dev == b.st_dev and a.ino == b.st_ino


def _same_file(a: FileIdentity, b: os.stat_result) -> bool:
    return (
        a.dev == b.st_dev
        and a.ino == b.st_ino
        and a.size == b.st_size
        and a.mtime_ns == b.st_mtime_ns
        and a.ctime_ns == b.st_ctime_ns
        and a.nlink == b.st_nlink
        and a.mode == b.st_mode
    )


def _cleanup_new_file(
    fd: int,
    parent_fd: int,
    name: str,
    created: tuple[int, int] | None,
    completed: bool,
) -> bool:
    """Undo a partial new-file write, scoped to the inode this call created.

    The name is re-checked against the recorded identity and link count before
    anything is removed, so a replacement file that appeared at the same path
    is never touched.  Returning ``True`` means the caller can no longer state
    what the workspace holds and must report an unknown outcome.
    """

    uncertain = False
    if fd >= 0 and created is None:
        try:
            own = os.fstat(fd)
            created = (own.st_dev, own.st_ino)
        except OSError:
            uncertain = True
    if created is not None and not completed:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == created and current.st_nlink == 1:
                os.unlink(name, dir_fd=parent_fd)
            else:
                uncertain = True
        except FileNotFoundError:
            pass
        except OSError:
            uncertain = True
    return uncertain


class WorkspaceActions:
    """Validate and execute the default workspace tools and optional deletion."""

    def __init__(self, root: str | os.PathLike[str], *, allow_delete: bool = False) -> None:
        raw = os.fspath(root)
        if not raw or not isinstance(raw, str):
            raise WorkspaceActionError("invalid_root")
        self.allow_delete = allow_delete is True
        self.root_path = Path(os.path.abspath(raw))
        try:
            root_st = os.lstat(self.root_path)
        except OSError as exc:
            raise WorkspaceActionError("invalid_root") from exc
        if not stat.S_ISDIR(root_st.st_mode) or root_st.st_nlink < 1:
            raise WorkspaceActionError("invalid_root")
        try:
            self.root_fd = os.open(self.root_path, _DIR_FLAGS)
        except OSError as exc:
            raise WorkspaceActionError("invalid_root") from exc
        self._root = _identity(os.fstat(self.root_fd))
        try:
            self._ensure_directory(("demo", "workspace"))
            self._base = self._directory_identity(_BASE)
        except Exception:
            os.close(self.root_fd)
            raise

    def close(self) -> None:
        fd = getattr(self, "root_fd", -1)
        if fd >= 0:
            os.close(fd)
            self.root_fd = -1

    def _check_root(self) -> None:
        try:
            fd_st = os.fstat(self.root_fd)
            path_st = os.lstat(self.root_path)
        except OSError as exc:
            raise WorkspaceActionError("workspace_changed") from exc
        if not stat.S_ISDIR(path_st.st_mode) or not _same_dir(self._root, fd_st) or not _same_dir(self._root, path_st):
            raise WorkspaceActionError("workspace_changed")

    @staticmethod
    def validate_path(value: Any, *, allow_base: bool = True) -> tuple[str, ...]:
        """Lexically validate before looking at any filesystem object."""
        if not isinstance(value, str) or not value or len(value) > 512:
            raise WorkspaceActionError("invalid_path")
        if "\x00" in value or "\\" in value or value.startswith(("/", "~")) or "%" in value:
            raise WorkspaceActionError("invalid_path")
        parts = tuple(value.split("/"))
        if any(not p or p in (".", "..") for p in parts):
            raise WorkspaceActionError("invalid_path")
        if len(parts) > 8 or parts[:2] != _BASE or (len(parts) == 2 and not allow_base):
            raise WorkspaceActionError("invalid_path")
        for component in parts:
            if len(component) > 64 or component.startswith(".") or component != component.strip() or not _COMPONENT.fullmatch(component):
                raise WorkspaceActionError("invalid_path")
        return parts

    @staticmethod
    def _same_path(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
        return "/".join(a).casefold() == "/".join(b).casefold()

    @staticmethod
    def validate_note_path(value: Any) -> tuple[str, ...]:
        """Lexically validate a note destination and its ``.txt`` extension.

        The suffix is matched case-sensitively so exactly one spelling of a
        given name reaches the workspace, and only the basename is inspected,
        after :meth:`validate_path` has confined the value.
        """

        parts = WorkspaceActions.validate_path(value, allow_base=False)
        if not _NOTE_NAME.fullmatch(parts[-1]):
            raise WorkspaceActionError("invalid_note_name")
        return parts

    @staticmethod
    def validate_note_content(value: Any) -> bytes:
        """Validate one note body and return the exact bytes to write.

        A note is short, non-blank, encodable, and free of control characters:
        at most 1024 Unicode code points and 4096 UTF-8 bytes, with newline the
        only permitted control character.  NUL, the rest of C0, DEL, and all of
        C1 are refused, as is any text Python cannot encode (an unpaired
        surrogate from the model's JSON).  The bound is intentionally small: a
        note is a short literal reminder, not a document, a script, or a blob.
        """

        if not isinstance(value, str) or not value or not value.strip():
            raise WorkspaceActionError("invalid_content")
        if len(value) > _MAX_NOTE_CODE_POINTS:
            raise WorkspaceActionError("invalid_content")
        for character in value:
            point = ord(character)
            if point in _ALLOWED_NOTE_CONTROLS:
                continue
            if point < 0x20 or point == 0x7F or 0x80 <= point < 0xA0:
                raise WorkspaceActionError("invalid_content")
        try:
            data = value.encode("utf-8")
        except UnicodeError as exc:
            raise WorkspaceActionError("invalid_content") from exc
        if not 1 <= len(data) <= _MAX_NOTE_BYTES:
            raise WorkspaceActionError("invalid_content")
        return data

    def _open_dir(self, parts: tuple[str, ...]) -> int:
        """Open a path below root, refusing every symlink component."""
        fd = os.dup(self.root_fd)
        try:
            for part in parts:
                try:
                    next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    raise WorkspaceActionError("path_not_found") from exc
                os.close(fd)
                fd = next_fd
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise WorkspaceActionError("not_directory")
            return fd
        except Exception:
            os.close(fd)
            raise

    def _directory_identity(self, parts: tuple[str, ...]) -> DirIdentity:
        fd = self._open_dir(parts)
        try:
            return _identity(os.fstat(fd))
        finally:
            os.close(fd)

    def _ensure_directory(self, parts: tuple[str, ...]) -> None:
        """Only used for the fixed bootstrap directory, never model paths."""
        fd = os.dup(self.root_fd)
        try:
            for part in parts:
                try:
                    next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                        next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
                    except OSError as exc:
                        raise WorkspaceActionError("bootstrap_failed") from exc
                except OSError as exc:
                    raise WorkspaceActionError("bootstrap_failed") from exc
                os.close(fd)
                fd = next_fd
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise WorkspaceActionError("bootstrap_failed")
        finally:
            os.close(fd)

    def _open_parent(self, parts: tuple[str, ...]) -> tuple[int, str]:
        if len(parts) <= len(_BASE):
            raise WorkspaceActionError("invalid_path")
        return self._open_dir(parts[:-1]), parts[-1]

    def _check_base(self) -> None:
        self._check_root()
        fd = self._open_dir(_BASE)
        try:
            if not _same_dir(self._base, os.fstat(fd)):
                raise WorkspaceActionError("workspace_changed")
        finally:
            os.close(fd)

    def prepare(self, tool: str, arguments: dict[str, str]) -> PreparedAction:
        if tool not in {"list_directory", "create_directory", "copy_file", "create_note", "delete_file"} or (tool == "delete_file" and not self.allow_delete):
            raise WorkspaceActionError("unknown_tool")
        if not isinstance(arguments, dict):
            raise WorkspaceActionError("invalid_arguments")
        self._check_base()
        if tool == "list_directory":
            if set(arguments) != {"path"}:
                raise WorkspaceActionError("invalid_arguments")
            parts = self.validate_path(arguments["path"])
            fd = self._open_dir(parts)
            try:
                target = _identity(os.fstat(fd))
            finally:
                os.close(fd)
            return PreparedAction(tool, dict(arguments), "List directory " + arguments["path"], "list", parts, None, None, None, self._root, self._base, target, None, None, None, None)

        if tool == "create_directory":
            if set(arguments) != {"path"}:
                raise WorkspaceActionError("invalid_arguments")
            parts = self.validate_path(arguments["path"], allow_base=False)
            parent_fd, name = self._open_parent(parts)
            try:
                parent = _identity(os.fstat(parent_fd))
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise WorkspaceActionError("destination_exists")
            finally:
                os.close(parent_fd)
            return PreparedAction(tool, dict(arguments), "Create directory " + arguments["path"], "mkdir", parts, parts[:-1], None, None, self._root, self._base, None, parent, None, None, None)

        if tool == "create_note":
            if set(arguments) != {"path", "content"}:
                raise WorkspaceActionError("invalid_arguments")
            parts = self.validate_note_path(arguments["path"])
            data = self.validate_note_content(arguments["content"])
            parent_fd, name = self._open_parent(parts)
            try:
                parent = _identity(os.fstat(parent_fd))
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise WorkspaceActionError("destination_exists")
            finally:
                os.close(parent_fd)
            # The summary names the destination and the size of the body only:
            # it is shown to the trusted user and carried in developer traces.
            characters = f" ({len(arguments['content'])} characters)"
            return PreparedAction(tool, dict(arguments), "Create note " + arguments["path"] + characters, "note", parts, parts[:-1], None, None, self._root, self._base, None, parent, None, None, None, data)

        if tool == "delete_file":
            if set(arguments) != {"path"}:
                raise WorkspaceActionError("invalid_arguments")
            parts = self.validate_path(arguments["path"], allow_base=False)
            parent_fd, name = self._open_parent(parts)
            try:
                parent = _identity(os.fstat(parent_fd))
                try:
                    path_st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise WorkspaceActionError("source_not_found") from exc
                except OSError as exc:
                    raise WorkspaceActionError("invalid_source") from exc
                if not stat.S_ISREG(path_st.st_mode) or path_st.st_nlink != 1:
                    raise WorkspaceActionError("invalid_source")
                if path_st.st_size < 0 or path_st.st_size > _MAX_COPY or not math.isfinite(float(path_st.st_size)):
                    raise WorkspaceActionError("source_too_large")
                initial_path_st = path_st
                try:
                    source_fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
                except FileNotFoundError as exc:
                    raise WorkspaceActionError("source_not_found") from exc
                except OSError as exc:
                    # O_NOFOLLOW refuses a symlink swapped in after lstat.
                    raise WorkspaceActionError("invalid_source") from exc
                try:
                    source_st = os.fstat(source_fd)
                    try:
                        landed_st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError as exc:
                        raise WorkspaceActionError("source_not_found") from exc
                    except OSError as exc:
                        raise WorkspaceActionError("invalid_source") from exc
                    if (
                        not stat.S_ISREG(source_st.st_mode)
                        or source_st.st_nlink != 1
                        or not _same_file(_file_identity(source_st), initial_path_st)
                        or not _same_file(_file_identity(source_st), landed_st)
                    ):
                        raise WorkspaceActionError("invalid_source")
                    if source_st.st_size < 0 or source_st.st_size > _MAX_COPY or not math.isfinite(float(source_st.st_size)):
                        raise WorkspaceActionError("source_too_large")
                    source_identity = _file_identity(source_st)
                finally:
                    os.close(source_fd)
            finally:
                os.close(parent_fd)
            summary = f"Permanently and irreversibly delete {arguments['path']} ({source_identity.size} bytes)"
            return PreparedAction(
                tool,
                dict(arguments),
                summary,
                "delete",
                parts,
                parts[:-1],
                None,
                None,
                self._root,
                self._base,
                None,
                parent,
                source_identity,
                parent,
                None,
            )

        if set(arguments) != {"source", "destination"}:
            raise WorkspaceActionError("invalid_arguments")
        source = self.validate_path(arguments["source"], allow_base=False)
        destination = self.validate_path(arguments["destination"], allow_base=False)
        if self._same_path(source, destination):
            raise WorkspaceActionError("same_path")
        source_parent_fd, source_name = self._open_parent(source)
        try:
            destination_parent_fd, destination_name = self._open_parent(destination)
        except BaseException:
            os.close(source_parent_fd)
            raise
        try:
            source_parent = _identity(os.fstat(source_parent_fd))
            destination_parent = _identity(os.fstat(destination_parent_fd))
            try:
                source_fd = os.open(source_name, _READ_FLAGS, dir_fd=source_parent_fd)
            except OSError as exc:
                raise WorkspaceActionError("source_not_found") from exc
            try:
                source_st = os.fstat(source_fd)
                path_st = os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
                if not stat.S_ISREG(source_st.st_mode) or source_st.st_nlink != 1 or not _same_file(_file_identity(source_st), path_st):
                    raise WorkspaceActionError("invalid_source")
                if source_st.st_size < 0 or source_st.st_size > _MAX_COPY or not math.isfinite(float(source_st.st_size)):
                    raise WorkspaceActionError("source_too_large")
            finally:
                os.close(source_fd)
            try:
                os.stat(destination_name, dir_fd=destination_parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise WorkspaceActionError("destination_exists")
            source_identity = _file_identity(source_st)
        finally:
            os.close(source_parent_fd)
            os.close(destination_parent_fd)
        return PreparedAction(
            tool,
            dict(arguments),
            "Copy file " + "/".join(source) + " to " + "/".join(destination),
            "copy",
            destination,
            None,
            source,
            destination,
            self._root,
            self._base,
            None,
            None,
            source_identity,
            source_parent,
            destination_parent,
        )

    def _revalidate_common(self, action: PreparedAction) -> None:
        self._check_root()
        self._check_base()
        if action.root != self._root or action.base != self._base:
            raise WorkspaceActionError("workspace_changed")

    def _revalidate_parent(self, parts: tuple[str, ...], expected: DirIdentity | None) -> tuple[int, str]:
        fd, name = self._open_parent(parts)
        try:
            if expected is None or not _same_dir(expected, os.fstat(fd)):
                raise WorkspaceActionError("workspace_changed")
            return fd, name
        except Exception:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def execute(self, action: PreparedAction) -> dict[str, Any]:
        if action.kind == "delete" and not self.allow_delete:
            raise WorkspaceActionError("unknown_tool")
        self._revalidate_common(action)
        if action.kind == "list":
            fd = self._open_dir(action.path_parts)
            try:
                if action.target is None or not _same_dir(action.target, os.fstat(fd)):
                    raise WorkspaceActionError("workspace_changed")
                try:
                    names = os.listdir(fd)
                except OSError as exc:
                    raise WorkspaceActionError("list_failed") from exc
                if len(names) > _MAX_ENTRIES:
                    raise WorkspaceActionError("too_many_entries")
                entries: list[dict[str, Any]] = []
                for name in sorted(names):
                    if name.startswith(".") or name != name.strip() or not _COMPONENT.fullmatch(name) or len(name) > 64:
                        continue
                    try:
                        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except OSError as exc:
                        raise WorkspaceActionError("list_failed") from exc
                    if stat.S_ISLNK(st.st_mode) or not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                        continue
                    item: dict[str, Any] = {"name": name, "type": "directory" if stat.S_ISDIR(st.st_mode) else "file"}
                    if stat.S_ISREG(st.st_mode):
                        item["size"] = st.st_size
                    entries.append(item)
                return {"path": "/".join(action.path_parts), "entries": entries}
            finally:
                os.close(fd)

        if action.kind == "mkdir":
            if action.parent_parts is None:
                raise WorkspaceActionError("invalid_path")
            fd, name = self._revalidate_parent(action.path_parts, action.parent)
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError as exc:
                raise WorkspaceActionError("destination_exists") from exc
            except OSError as exc:
                raise WorkspaceActionError("create_failed") from exc
            finally:
                os.close(fd)
            return {"path": "/".join(action.path_parts), "created": True}

        if action.kind == "note":
            return self._create_note(action)

        if action.kind == "delete":
            return self._delete_file(action)

        if action.source_parts is None or action.destination_parts is None:
            raise WorkspaceActionError("invalid_action")
        source_parent_fd, source_name = self._revalidate_parent(action.source_parts, action.source_parent)
        try:
            destination_parent_fd, destination_name = self._revalidate_parent(action.destination_parts, action.destination_parent)
        except BaseException:
            os.close(source_parent_fd)
            raise
        source_fd = -1
        destination_fd = -1
        created: tuple[int, int] | None = None
        completed = False
        try:
            try:
                source_fd = os.open(source_name, _READ_FLAGS, dir_fd=source_parent_fd)
                source_st = os.fstat(source_fd)
                source_path_st = os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceActionError("source_changed") from exc
            if action.source is None or not stat.S_ISREG(source_st.st_mode) or source_st.st_nlink != 1 or not _same_file(action.source, source_st) or not _same_file(action.source, source_path_st):
                raise WorkspaceActionError("source_changed")
            try:
                destination_fd = os.open(destination_name, _WRITE_FLAGS, 0o600, dir_fd=destination_parent_fd)
            except FileExistsError as exc:
                raise WorkspaceActionError("destination_exists") from exc
            except OSError as exc:
                raise WorkspaceActionError("destination_failed") from exc
            destination_st = os.fstat(destination_fd)
            created = (destination_st.st_dev, destination_st.st_ino)
            try:
                os.fchmod(destination_fd, 0o600)
            except OSError as exc:
                raise WorkspaceActionError("destination_failed") from exc
            total = 0
            while True:
                block = os.read(source_fd, min(1024 * 1024, _MAX_COPY + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > _MAX_COPY:
                    raise WorkspaceActionError("source_too_large")
                view = memoryview(block)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise WorkspaceActionError("copy_failed")
                    view = view[written:]
            os.fsync(destination_fd)
            after = os.fstat(source_fd)
            after_path = os.stat(source_name, dir_fd=source_parent_fd, follow_symlinks=False)
            if action.source is None or not _same_file(action.source, after) or not _same_file(action.source, after_path):
                raise WorkspaceActionError("source_changed")
            if os.fstat(destination_fd).st_size != total:
                raise WorkspaceActionError("copy_failed")
            final_destination = os.stat(destination_name, dir_fd=destination_parent_fd, follow_symlinks=False)
            if (final_destination.st_dev, final_destination.st_ino) != created or final_destination.st_nlink != 1:
                raise WorkspaceActionError("copy_failed")
            completed = True
            return {"source": "/".join(action.source_parts), "destination": "/".join(action.destination_parts), "bytes": total}
        except OSError as exc:
            raise WorkspaceActionError("copy_failed") from exc
        finally:
            uncertain = _cleanup_new_file(destination_fd, destination_parent_fd, destination_name, created, completed)
            for fd in (source_fd, destination_fd, source_parent_fd, destination_parent_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        uncertain = True
            if uncertain:
                raise WorkspaceActionError("outcome_unknown")

    def _create_note(self, action: PreparedAction) -> dict[str, Any]:
        """Write one new note, or leave the workspace exactly as it was.

        The body is held on the prepared action in memory and written through a
        descriptor opened against the revalidated parent directory, so no path
        is re-resolved by name.  Creation is exclusive: an existing name, a
        symlink, or a directory at the destination is refused instead of being
        replaced.  Nothing but the destination, the byte count, and the fact of
        creation is ever returned, and no body byte is logged.
        """

        if action.note_bytes is None or not action.path_parts:
            raise WorkspaceActionError("invalid_action")
        parent_fd, name = self._revalidate_parent(action.path_parts, action.parent)
        destination_fd = -1
        created: tuple[int, int] | None = None
        completed = False
        try:
            try:
                destination_fd = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=parent_fd)
            except FileExistsError as exc:
                raise WorkspaceActionError("destination_exists") from exc
            except OSError as exc:
                raise WorkspaceActionError("note_failed") from exc
            try:
                opened = os.fstat(destination_fd)
                created = (opened.st_dev, opened.st_ino)
            except OSError as exc:
                raise WorkspaceActionError("note_failed") from exc
            try:
                os.fchmod(destination_fd, 0o600)
            except OSError as exc:
                raise WorkspaceActionError("note_failed") from exc
            total = 0
            view = memoryview(action.note_bytes)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise WorkspaceActionError("note_failed")
                view = view[written:]
                total += written
            os.fsync(destination_fd)
            if os.fstat(destination_fd).st_size != total:
                raise WorkspaceActionError("note_failed")
            final = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (final.st_dev, final.st_ino) != created or final.st_nlink != 1 or final.st_size != total or not stat.S_ISREG(final.st_mode):
                raise WorkspaceActionError("note_failed")
            completed = True
            return {"path": "/".join(action.path_parts), "bytes": total, "created": True}
        except OSError as exc:
            raise WorkspaceActionError("note_failed") from exc
        finally:
            uncertain = _cleanup_new_file(destination_fd, parent_fd, name, created, completed)
            for fd in (destination_fd, parent_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        uncertain = True
            if uncertain:
                raise WorkspaceActionError("outcome_unknown")

    def _delete_file(self, action: PreparedAction) -> dict[str, Any]:
        """Permanently unlink one confirmed file, without reading its contents.

        The parent descriptor anchors the final unlink and the open target
        descriptor lets us compare the captured identity immediately before the
        effect and verify its link count afterward.  As with the rest of this
        policy, this is not atomic protection against a hostile same-user
        process concurrently swapping names between the final check and unlink.
        """

        parts = action.path_parts
        try:
            validated_parts = self.validate_path(action.arguments.get("path"), allow_base=False)
        except WorkspaceActionError as exc:
            raise WorkspaceActionError("invalid_action") from exc
        if (
            action.tool != "delete_file"
            or set(action.arguments) != {"path"}
            or validated_parts != parts
            or action.source is None
            or action.source.nlink != 1
            or action.source.size < 0
            or action.source.size > _MAX_COPY
            or not stat.S_ISREG(action.source.mode)
            or action.parent is None
            or action.parent_parts != parts[:-1]
            or action.source_parent != action.parent
            or len(parts) <= len(_BASE)
        ):
            raise WorkspaceActionError("invalid_action")
        parent_fd, name = self._revalidate_parent(parts, action.parent)
        source_fd = -1
        dispatched = False
        deleted_verified = False
        try:
            try:
                before_open = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceActionError("source_changed") from exc
            if (
                not stat.S_ISREG(before_open.st_mode)
                or before_open.st_nlink != 1
                or not _same_file(action.source, before_open)
            ):
                raise WorkspaceActionError("source_changed")
            try:
                source_fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise WorkspaceActionError("source_changed") from exc

            def check_target() -> os.stat_result:
                opened = os.fstat(source_fd)
                landed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or not _same_file(action.source, opened)
                    or not _same_file(action.source, landed)
                ):
                    raise WorkspaceActionError("source_changed")
                if opened.st_size < 0 or opened.st_size > _MAX_COPY:
                    raise WorkspaceActionError("source_changed")
                return opened

            try:
                check_target()
                # Repeat the full descriptor/path check as the last policy step
                # before dispatching unlink.  Do not retry if anything is uncertain.
                check_target()
            except WorkspaceActionError:
                raise
            except OSError as exc:
                raise WorkspaceActionError("source_changed") from exc
            dispatched = True
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError as exc:
                # A syscall error is a no-effect failure only when the original
                # inode is still linked at the exact path with its full identity.
                try:
                    opened = os.fstat(source_fd)
                    landed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError:
                    raise WorkspaceActionError("outcome_unknown") from exc
                if _same_file(action.source, opened) and _same_file(action.source, landed):
                    raise WorkspaceActionError("delete_failed") from exc
                raise WorkspaceActionError("outcome_unknown") from exc

            try:
                after = os.fstat(source_fd)
                if after.st_nlink != 0:
                    raise WorkspaceActionError("outcome_unknown")
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise WorkspaceActionError("outcome_unknown")
                deleted_verified = True
            except WorkspaceActionError:
                raise
            except OSError as exc:
                raise WorkspaceActionError("outcome_unknown") from exc
            return {"path": "/".join(parts), "bytes": action.source.size, "deleted": True}
        except WorkspaceActionError:
            raise
        except OSError as exc:
            if dispatched:
                raise WorkspaceActionError("outcome_unknown") from exc
            raise WorkspaceActionError("delete_failed") from exc
        finally:
            pending_exception = sys.exc_info()[1]
            close_error: OSError | None = None
            for descriptor in (source_fd, parent_fd):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError as exc:
                        close_error = exc
            if deleted_verified and close_error is not None and pending_exception is None:
                raise WorkspaceActionError("outcome_unknown") from close_error


TOOLS: dict[str, dict[str, Any]] = {
    "list_directory": {
        "description": "List file/folder names and sizes in demo/workspace or one of its directories; never read file contents.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "maxLength": 512, "pattern": "^demo/workspace(?:/[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}){0,6}$", "description": "Full project-relative directory path, starting with demo/workspace. Preserve literal spaces in folder names."}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "create_directory": {
        "description": "Create one new folder below demo/workspace. Its parent must exist; never overwrite. Preserve the complete requested name, including spaces.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "maxLength": 512, "pattern": "^demo/workspace(?:/[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}){1,6}$", "description": "Exact full project-relative path of the new folder, starting with demo/workspace/. Spaces belong to the name; do not drop words."}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "copy_file": {
        "description": "Copy one regular, single-link file up to 8 MiB within demo/workspace. Source and destination are exact full paths; destination must not exist. Trusted user confirmation is required outside the model.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "maxLength": 512, "pattern": "^demo/workspace(?:/[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}){1,6}$", "description": "Exact full project-relative source file path. Preserve spaces."},
                "destination": {"type": "string", "maxLength": 512, "pattern": "^demo/workspace(?:/[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}){1,6}$", "description": "Exact full project-relative new file path, including its filename. Preserve spaces."},
            },
            "required": ["source", "destination"],
            "additionalProperties": False,
        },
    },
    "create_note": {
        "description": "Create one new plain-text note file whose name ends in .txt, below demo/workspace. Its parent must exist and the destination must not exist; never overwrite. The body is short plain text with no control characters. Trusted user confirmation is required outside the model.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "maxLength": 512, "pattern": "^demo/workspace(?:/[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}){1,6}$", "description": "Exact full project-relative path of the new note, starting with demo/workspace/. The filename must end in a lowercase .txt. Preserve spaces."},
                "content": {"type": "string", "minLength": 1, "maxLength": 1024, "description": "Literal note text: at most 1024 characters and 4096 UTF-8 bytes, non-blank, line breaks allowed but no tabs or other control characters. Keep it short and do not add headings or quotes."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}


DELETE_TOOL: dict[str, Any] = {
    "description": "Permanently and irreversibly delete exactly one existing regular, single-link file up to 8 MiB in demo/workspace. This cannot be undone. Trusted user confirmation is required outside the model.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "maxLength": 512,
                "pattern": "^demo/workspace(?:/[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}){1,6}$",
                "description": "Exact full project-relative file path, starting with demo/workspace/. No directories, wildcards, hidden names, or paths outside the workspace.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


WorkspacePolicy = WorkspaceActions
