"""Path confinement and launcher-environment helpers.

The application deliberately has no implicit ``~`` or current-directory path
fallback.  The launcher supplies ``MACINTENT_ROOT`` and ``HF_HOME``; all
application paths are resolved below that root before they are read or written.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


class WorkspacePathError(ValueError):
    """Raised when a path is not safely contained by the workspace."""


def workspace_root(value: str | os.PathLike[str]) -> Path:
    """Return an existing, absolute workspace root.

    ``value`` is the launcher-controlled root, not an arbitrary model or
    output path.  Resolving it also gives symlink checks a stable base.
    """

    raw = os.fspath(value)
    if not raw:
        raise WorkspacePathError("workspace root must not be empty")
    root = Path(raw).resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise WorkspacePathError(f"workspace root is not an existing directory: {root}")
    return root


def confined_path(
    root: Path,
    supplied: str | os.PathLike[str],
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve ``supplied`` and reject traversal or symlink escapes.

    Relative paths are relative to ``root``.  Absolute paths are accepted only
    when they still resolve inside ``root``; this keeps the helper useful for
    callers that already have a workspace absolute path while preventing an
    absolute outside path from becoming an accidental input/output.
    """

    root = workspace_root(root)
    raw = os.fspath(supplied)
    if not raw:
        raise WorkspacePathError("workspace path must not be empty")
    if raw.startswith("~"):
        raise WorkspacePathError("tilde paths are not accepted; use a workspace-relative path")

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    # Check lexical containment before consulting the filesystem. In particular,
    # do not resolve a supplied personal path just to discover that it is outside.
    resolved = Path(os.path.abspath(candidate))
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError(
            f"path escapes workspace root {root}: {supplied!s} -> {resolved}"
        ) from exc

    # Refuse symlink traversal entirely for developer inputs/model snapshots.
    # This avoids following even an intermediate symlink into personal data.
    current = root
    for component in resolved.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            raise WorkspacePathError(f"symlinked workspace paths are not supported: {current}")

    if must_exist and not resolved.exists():
        raise WorkspacePathError(f"workspace path does not exist: {supplied!s}")
    return resolved


def relative_workspace_path(root: Path, path: Path) -> str:
    """Return a portable POSIX relative path after applying confinement."""

    return confined_path(root, path).relative_to(workspace_root(root)).as_posix()


def launcher_workspace(environ: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    """Read and validate the required launcher environment.

    ``HF_HOME`` must be exactly ``<MACINTENT_ROOT>/.cache/huggingface`` after
    resolution.  This prevents Hugging Face or Transformers from silently
    reading/writing a personal global cache.
    """

    environ = os.environ if environ is None else environ
    root_value = environ.get("MACINTENT_ROOT")
    if not root_value:
        raise WorkspacePathError("MACINTENT_ROOT is required")
    root = workspace_root(root_value)

    hf_value = environ.get("HF_HOME")
    if not hf_value:
        raise WorkspacePathError("HF_HOME is required")
    hf_home = confined_path(root, hf_value)
    expected = (root / ".cache" / "huggingface").resolve(strict=False)
    if hf_home != expected:
        raise WorkspacePathError(
            f"HF_HOME must be {expected}, not {hf_home}"
        )
    return root, hf_home


# Descriptive aliases used by callers and tests.
resolve_workspace_path = confined_path
workspace_path = confined_path
workspace_from_env = launcher_workspace


def models_directory(root: Path) -> Path:
    """Return the confined model storage directory."""

    return confined_path(root, "models")


def reports_directory(root: Path) -> Path:
    """Return the confined validation-report directory."""

    return confined_path(root, ".local/reports")
