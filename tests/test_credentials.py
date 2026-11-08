"""Groq credential storage tests use only synthetic keys in temporary roots."""

import os
from pathlib import Path
import stat

import pytest

from macintent import credentials


SYNTHETIC_KEY = "gsk_0123456789abcdefghijklmnopqrstuv"


def make_source(root: Path, key: str = SYNTHETIC_KEY, *, suffix: bytes = b"\n") -> Path:
    source = root / "api.txt"
    source.write_bytes(key.encode("ascii") + suffix)
    source.chmod(0o644)
    return source


def destination(root: Path) -> Path:
    return root / credentials.KEY_RELATIVE_PATH


def test_import_creates_private_key_then_read_returns_it_without_source(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = make_source(root)

    credentials.import_api_key(root)

    key_path = destination(root)
    assert not source.exists()
    assert key_path.read_bytes() == SYNTHETIC_KEY.encode("ascii")
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    for directory in (key_path.parent, key_path.parent.parent, key_path.parent.parent.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert credentials.read_api_key(root) == SYNTHETIC_KEY


def test_identical_existing_destination_is_safe_recovery_without_overwrite(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    key_path = destination(root)
    key_path.parent.mkdir(parents=True)
    key_path.parent.chmod(0o700)
    key_path.write_bytes(SYNTHETIC_KEY.encode("ascii"))
    key_path.chmod(0o600)
    original = key_path.stat()
    source = make_source(root)

    credentials.import_api_key(root)

    assert not source.exists()
    after = key_path.stat()
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (original.st_dev, original.st_ino, original.st_mtime_ns)
    assert credentials.read_api_key(root) == SYNTHETIC_KEY


def test_conflicting_destination_preserves_source_and_existing_key(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    existing_key = "gsk_abcdefghijklmnop0123456789"
    key_path = destination(root)
    key_path.parent.mkdir(parents=True)
    key_path.parent.chmod(0o700)
    key_path.write_bytes(existing_key.encode("ascii"))
    key_path.chmod(0o600)
    source = make_source(root)
    source_bytes = source.read_bytes()

    with pytest.raises(credentials.CredentialError) as exc:
        credentials.import_api_key(root)

    assert str(exc.value) == "credential_unavailable"
    assert source.read_bytes() == source_bytes
    assert key_path.read_bytes() == existing_key.encode("ascii")


@pytest.mark.parametrize("value", [
    b"", b" gsk_0123456789abcdefghijklmnopqrstuv", b"gsk_0123456789abcdefghijklmnopqrstuv extra",
    b"gsk_0123456789abcdefghijklmnop\ngsk_abcdefghijklmnop0123456789", b"gsk_bad!key0123456789abcdefghijkl",
    b"not-a-groq-key", b"gsk_0123456789abcdefghijklmnopqrstuv\r\n\n", b"gsk_" + b"a" * 1025,
])
def test_invalid_source_fails_closed_without_changing_contents(tmp_path, value):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "api.txt"
    source.write_bytes(value)
    source.chmod(0o644)

    with pytest.raises(credentials.CredentialError) as exc:
        credentials.import_api_key(root)

    assert str(exc.value) == "credential_unavailable"
    assert exc.value.__context__ is None
    assert source.read_bytes() == value
    assert not destination(root).exists()


def test_missing_and_symlinked_roots_are_rejected(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(credentials.CredentialError):
        credentials.read_api_key(root)
    with pytest.raises(credentials.CredentialError):
        credentials.import_api_key(alias)
    with pytest.raises(credentials.CredentialError):
        credentials.read_api_key(Path("relative/project"))


def test_symlinked_source_hardlinked_source_and_nonregular_source_are_rejected(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "synthetic-key"
    outside.write_bytes(SYNTHETIC_KEY.encode("ascii"))
    (root / "api.txt").symlink_to(outside)
    with pytest.raises(credentials.CredentialError):
        credentials.import_api_key(root)
    assert outside.read_bytes() == SYNTHETIC_KEY.encode("ascii")

    (root / "api.txt").unlink()
    source = make_source(root)
    os.link(source, tmp_path / "hardlink")
    with pytest.raises(credentials.CredentialError):
        credentials.import_api_key(root)
    assert source.exists()

    source.unlink()
    (root / "api.txt").mkdir()
    with pytest.raises(credentials.CredentialError):
        credentials.import_api_key(root)


def test_symlinked_intermediate_destination_and_key_are_rejected(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = make_source(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".local").symlink_to(outside, target_is_directory=True)

    with pytest.raises(credentials.CredentialError):
        credentials.import_api_key(root)
    assert source.exists() and not (outside / "state").exists()

    (root / ".local").unlink()
    key_path = destination(root)
    key_path.parent.mkdir(parents=True)
    key_path.parent.chmod(0o700)
    target = tmp_path / "external-key"
    target.write_bytes(SYNTHETIC_KEY.encode("ascii"))
    target.chmod(0o600)
    key_path.symlink_to(target)
    with pytest.raises(credentials.CredentialError):
        credentials.read_api_key(root)
    assert target.read_bytes() == SYNTHETIC_KEY.encode("ascii")


def test_read_rejects_hardlinked_nonprivate_and_oversized_destination(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    key_path = destination(root)
    key_path.parent.mkdir(parents=True)
    key_path.parent.chmod(0o700)
    key_path.write_bytes(SYNTHETIC_KEY.encode("ascii"))
    key_path.chmod(0o600)
    os.link(key_path, tmp_path / "external-hardlink")
    with pytest.raises(credentials.CredentialError):
        credentials.read_api_key(root)

    (tmp_path / "external-hardlink").unlink()
    key_path.chmod(0o644)
    with pytest.raises(credentials.CredentialError):
        credentials.read_api_key(root)
    key_path.chmod(0o600)
    key_path.write_bytes(b"gsk_" + b"a" * 300)
    with pytest.raises(credentials.CredentialError):
        credentials.read_api_key(root)


def test_source_is_not_removed_when_key_creation_fails(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    source = make_source(root)

    def fail_write(*_args, **_kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(credentials, "_write_all", fail_write)
    with pytest.raises(credentials.CredentialError):
        credentials.import_api_key(root)
    assert source.exists() and source.read_bytes().startswith(b"gsk_")
    assert not destination(root).exists()


@pytest.mark.parametrize("value", [
    "GROQ_API_KEY=" + SYNTHETIC_KEY + "\n",
    'GROQ_API_KEY="' + SYNTHETIC_KEY + '"\r\n',
    "GROQ_API_KEY = '" + SYNTHETIC_KEY + "' \n",
])
def test_single_literal_assignment_is_data_not_shell(tmp_path, value):
    (tmp_path / "api.txt").write_text(value)
    credentials.import_api_key(tmp_path)
    assert credentials.read_api_key(tmp_path) == SYNTHETIC_KEY
    assert not (tmp_path / "api.txt").exists()


@pytest.mark.parametrize("value", [
    "GROQ_API_KEY=$(anything)", "GROQ_API_KEY=" + SYNTHETIC_KEY + "; anything",
    "GROQ_API_KEY=" + SYNTHETIC_KEY + "\nOTHER=value\n",
    'GROQ_API_KEY="' + SYNTHETIC_KEY + "'", "GROQ_API_KEY=${OTHER}",
])
def test_assignment_cannot_include_commands_expansion_or_additional_keys(tmp_path, value):
    source = tmp_path / "api.txt"
    source.write_text(value)
    with pytest.raises(credentials.CredentialError):
        credentials.import_api_key(tmp_path)
    assert source.read_text() == value and not destination(tmp_path).exists()


@pytest.mark.parametrize("directory", [".local", ".local/state", ".local/state/keys"])
def test_read_and_import_reject_insecure_existing_directories_without_repair(tmp_path, directory):
    make_source(tmp_path)
    credentials.import_api_key(tmp_path)
    unsafe = tmp_path / directory
    unsafe.chmod(0o755)
    with pytest.raises(credentials.CredentialError):
        credentials.read_api_key(tmp_path)
    assert stat.S_IMODE(unsafe.stat().st_mode) == 0o755
    source = make_source(tmp_path)
    with pytest.raises(credentials.CredentialError):
        credentials.import_api_key(tmp_path)
    assert source.exists() and stat.S_IMODE(unsafe.stat().st_mode) == 0o755


def test_readiness_read_does_not_change_private_directory_metadata(tmp_path):
    make_source(tmp_path)
    credentials.import_api_key(tmp_path)
    directories = [tmp_path / part for part in (".local", ".local/state", ".local/state/keys")]
    before = [path.stat().st_ctime_ns for path in directories]
    assert credentials.read_api_key(tmp_path) == SYNTHETIC_KEY
    assert [path.stat().st_ctime_ns for path in directories] == before
