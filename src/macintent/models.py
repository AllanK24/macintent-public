"""Explicitly download and verify only MacIntent's pinned public Parakeet model."""

from pathlib import Path
import sys

from .paths import confined_path, launcher_workspace
from .speech import PARAKEET_FILES, PARAKEET_SPEC, local_parakeet_path


def download(root: Path, hf_home: Path) -> Path:
    target = confined_path(root, "models/" + PARAKEET_SPEC.directory)
    if target.exists():
        # Never repair or overwrite unfamiliar/invalid model-directory contents.
        return local_parakeet_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(mode=0o700)
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=PARAKEET_SPEC.repo_id,
        revision=PARAKEET_SPEC.revision,
        local_dir=str(target),
        cache_dir=str(hf_home / "hub"),
        allow_patterns=list(PARAKEET_SPEC.required_files),
        token=False,
    )
    # Same pinned size/hash/custom-code checks as every inference startup.
    return local_parakeet_path(root)


def main() -> int:
    if sys.argv[1:]:
        print("Usage: ./scripts/download-model", file=sys.stderr)
        return 2
    try:
        root, hf_home = launcher_workspace()
        download(root, hf_home)
    except Exception:
        print("Pinned model download/validation failed. Existing model files were not repaired; inspect any incomplete model directory before retrying.", file=sys.stderr)
        return 1
    print("Pinned Parakeet snapshot verified locally. No microphone, inference or cloud planning request started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
