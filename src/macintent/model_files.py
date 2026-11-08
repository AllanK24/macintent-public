"""Validate the pinned local model snapshots used by MacIntent.

Validation utilities consume an existing local snapshot and optional file
metadata. No network, download, manifest, or model-loading behaviour belongs
here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import confined_path


@dataclass(frozen=True)
class ModelSpec:
    name: str
    repo_id: str
    revision: str
    directory: str
    required_files: tuple[str, ...]


class ModelDownloadError(RuntimeError):
    """Raised for an incomplete, unexpected, or non-reproducible snapshot."""


class _MissingModelFilesError(ModelDownloadError):
    """An incomplete snapshot that can safely be resumed from its pinned Hub."""


def _required_file_paths(root: Path, spec: ModelSpec) -> dict[str, Path]:
    model_dir = confined_path(root, f"models/{spec.directory}")
    return {name: confined_path(root, model_dir / name) for name in spec.required_files}


def _require_file_digest(item: dict[str, Any]) -> None:
    sha256 = item.get("sha256")
    blob_id = item.get("blob_id")
    if sha256:
        valid = isinstance(sha256, str) and re.fullmatch(r"[0-9a-f]{64}", sha256)
    else:
        valid = isinstance(blob_id, str) and re.fullmatch(r"[0-9a-f]{40}", blob_id)
    if not valid:
        raise ModelDownloadError("model file metadata requires a usable checksum")


def _validate_local_snapshot(root: Path, spec: ModelSpec, record: dict[str, Any] | None = None) -> None:
    paths = _required_file_paths(root, spec)
    model_dir = confined_path(root, f"models/{spec.directory}")
    if model_dir.exists() and not model_dir.is_dir():
        raise ModelDownloadError(f"local {spec.name} snapshot path is not a directory: {model_dir}")

    # Resolve every required path before deciding that a partial snapshot is
    # resumable. A symlink, non-regular file, or hard link is an invalid local
    # snapshot, not a missing-file condition that snapshot_download may repair.
    missing = []
    for name, path in paths.items():
        confined_path(root, path)
        if not path.exists():
            missing.append(name)
            continue
        if not path.is_file():
            raise ModelDownloadError(f"model file is not a regular file: {path}")
        if path.stat(follow_symlinks=False).st_nlink != 1:
            raise ModelDownloadError(f"model file has unexpected hard links: {path}")

    # A second precision or shard weight would violate the selected MLX
    # artifact contract.  Other Hub metadata is harmless and may be present in
    # local_dir's cache bookkeeping.
    unexpected_weights = sorted(
        path.name
        for path in model_dir.glob("*.safetensors")
        if path.name not in spec.required_files
    )
    if unexpected_weights:
        raise ModelDownloadError(
            f"local {spec.name} snapshot has unexpected weight files: {', '.join(unexpected_weights)}"
        )
    if record is not None:
        items = record.get("files")
        if (not isinstance(items, list)
                or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in items)
                or len(items) != len(paths) or {item["name"] for item in items} != set(paths)):
            raise ModelDownloadError("model manifest file list mismatch")
        for item in items:
            _require_file_digest(item)
            path = paths[item["name"]]
            if not path.exists():
                continue
            if "size" in item and path.stat().st_size != item["size"]:
                raise ModelDownloadError(f"model file size mismatch: {path}")
            if item.get("sha256"):
                with path.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                if digest != item["sha256"]:
                    raise ModelDownloadError(f"model file checksum mismatch: {path}")
            elif item.get("blob_id"):
                # Git's blob hash includes a header, not just the file bytes.
                digest = hashlib.sha1(f"blob {path.stat().st_size}\0".encode())
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != item["blob_id"]:
                    raise ModelDownloadError(f"model metadata checksum mismatch: {path}")
    if "config.json" in paths and paths["config.json"].exists():
        try:
            config = json.loads(paths["config.json"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelDownloadError(f"invalid local model config: {paths['config.json']}") from exc
        if not isinstance(config, dict):
            raise ModelDownloadError("model config must be a JSON object")
        if config.get("model_file") or config.get("auto_map"):
            raise ModelDownloadError("custom model/processor code is not permitted")
    if any(model_dir.glob("*.py")):
        raise ModelDownloadError("Python code in a model snapshot is not permitted")
    if missing:
        raise _MissingModelFilesError(
            f"local {spec.name} snapshot is missing files: {', '.join(missing)}"
        )


__all__ = [
    "ModelDownloadError",
    "ModelSpec",
    "_MissingModelFilesError",
    "_require_file_digest",
    "_required_file_paths",
    "_validate_local_snapshot",
]
