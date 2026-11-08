"""Model setup checks are mocked and never download weights or read real models."""

import sys
from types import SimpleNamespace

import pytest

from macintent import models


def test_download_uses_only_pinned_public_files_then_verifies(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=lambda **kwargs: calls.append(kwargs)))
    expected = tmp_path / "models/parakeet-v2"
    monkeypatch.setattr(models, "local_parakeet_path", lambda root: expected)
    hf_home = tmp_path / ".cache/huggingface"
    assert models.download(tmp_path, hf_home) == expected
    assert calls == [{
        "repo_id": models.PARAKEET_SPEC.repo_id,
        "revision": models.PARAKEET_SPEC.revision,
        "local_dir": str(expected), "cache_dir": str(hf_home / "hub"),
        "allow_patterns": list(models.PARAKEET_SPEC.required_files), "token": False,
    }]


def test_existing_invalid_model_directory_is_not_overwritten(tmp_path, monkeypatch):
    target = tmp_path / "models/parakeet-v2"
    target.mkdir(parents=True)
    marker = target / "operator-file"
    marker.write_text("preserve")
    def invalid(root):
        raise RuntimeError("invalid snapshot")
    monkeypatch.setattr(models, "local_parakeet_path", invalid)
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=lambda **kwargs: pytest.fail("unexpected download")))
    with pytest.raises(RuntimeError, match="invalid snapshot"):
        models.download(tmp_path, tmp_path / ".cache/huggingface")
    assert marker.read_text() == "preserve"
