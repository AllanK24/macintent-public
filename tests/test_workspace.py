import os
import tempfile
from pathlib import Path

import pytest

from macintent.workspace import WorkspaceActionError, WorkspaceActions


@pytest.fixture
def root():
    path = Path(tempfile.mkdtemp(prefix="workspace-actions-", dir=os.environ["TMPDIR"]))
    (path / "demo" / "workspace").mkdir(parents=True)
    yield path


def test_copy_is_bounded_non_overwriting_and_private(root):
    workspace = root / "demo" / "workspace"
    (workspace / "source.txt").write_bytes(b"hello")
    actions = WorkspaceActions(root)
    try:
        prepared = actions.prepare("copy_file", {"source": "demo/workspace/source.txt", "destination": "demo/workspace/dest.txt"})
        result = actions.execute(prepared)
        assert result["bytes"] == 5
        assert (workspace / "dest.txt").read_bytes() == b"hello"
        assert (workspace / "dest.txt").stat().st_mode & 0o777 == 0o600
        with pytest.raises(WorkspaceActionError) as exc:
            actions.prepare("copy_file", {"source": "demo/workspace/source.txt", "destination": "demo/workspace/dest.txt"})
        assert exc.value.code == "destination_exists"
    finally:
        actions.close()

@pytest.mark.parametrize("value", [
    "../outside", "demo/workspace/../outside", "demo/workspace/%2e%2e/x",
    "/etc/passwd", "demo\\workspace\\x", "demo/workspace/.hidden",
    "demo/workspace/x ", "demo/workspace/a/b/c/d/e/f/g/h/i",
])
def test_paths_are_lexically_confined(root, value):
    actions = WorkspaceActions(root)
    try:
        with pytest.raises(WorkspaceActionError):
            actions.prepare("list_directory", {"path": value})
    finally:
        actions.close()


def test_symlink_and_hardlink_sources_are_denied(root):
    workspace = root / "demo" / "workspace"
    (workspace / "real").write_bytes(b"x")
    (workspace / "link").symlink_to(workspace / "real")
    os.link(workspace / "real", workspace / "hard")
    actions = WorkspaceActions(root)
    try:
        for name in ("link", "hard"):
            with pytest.raises(WorkspaceActionError):
                actions.prepare("copy_file", {"source": f"demo/workspace/{name}", "destination": "demo/workspace/out"})
    finally:
        actions.close()


def test_create_requires_existing_parent_and_list_hides_symlinks(root):
    workspace = root / "demo" / "workspace"
    (workspace / ".secret").write_text("no")
    (workspace / "escape").symlink_to(root)
    actions = WorkspaceActions(root)
    try:
        with pytest.raises(WorkspaceActionError):
            actions.prepare("create_directory", {"path": "demo/workspace/missing/child"})
        prepared = actions.prepare("list_directory", {"path": "demo/workspace"})
        result = actions.execute(prepared)
        assert {entry["name"] for entry in result["entries"]} == set()
    finally:
        actions.close()


NOTE_ARGUMENTS = {"path": "demo/workspace/note.txt", "content": "buy milk\nremind the user"}


def test_note_is_new_only_private_and_reports_only_write_metadata(root):
    workspace = root / "demo" / "workspace"
    actions = WorkspaceActions(root)
    try:
        prepared = actions.prepare("create_note", NOTE_ARGUMENTS)
        assert prepared.kind == "note"
        assert prepared.requires_confirmation is True
        assert prepared.note_bytes == b"buy milk\nremind the user"
        result = actions.execute(prepared)
        assert result == {"path": "demo/workspace/note.txt", "bytes": 24, "created": True}
        note = workspace / "note.txt"
        assert note.read_bytes() == b"buy milk\nremind the user"
        assert note.stat().st_mode & 0o777 == 0o600
        assert note.stat().st_nlink == 1
        # A second note never replaces the first one.
        with pytest.raises(WorkspaceActionError) as second:
            actions.prepare("create_note", NOTE_ARGUMENTS)
        assert second.value.code == "destination_exists"
        assert note.read_bytes() == b"buy milk\nremind the user"
    finally:
        actions.close()


def test_note_audit_projection_is_the_destination_only(root):
    actions = WorkspaceActions(root)
    try:
        prepared = actions.prepare("create_note", NOTE_ARGUMENTS)
        assert prepared.audited_arguments() == {"path": "demo/workspace/note.txt"}
        assert NOTE_ARGUMENTS["content"] not in str(prepared.audited_arguments())
        assert prepared.requires_confirmation is True
        # The other tools are fully described by their paths.
        listing = actions.prepare("list_directory", {"path": "demo/workspace"})
        assert listing.audited_arguments() == {"path": "demo/workspace"}
        assert listing.requires_confirmation is False
        folder = actions.prepare("create_directory", {"path": "demo/workspace/folder"})
        assert folder.requires_confirmation is False
        (workspace_file := root / "demo" / "workspace" / "source.txt").write_bytes(b"fixture")
        copied = actions.prepare("copy_file", {"source": "demo/workspace/source.txt", "destination": "demo/workspace/copy.txt"})
        assert copied.audited_arguments() == {"source": "demo/workspace/source.txt", "destination": "demo/workspace/copy.txt"}
        assert copied.requires_confirmation is True
        assert workspace_file.read_bytes() == b"fixture"
    finally:
        actions.close()


@pytest.mark.parametrize("value", [
    "demo/workspace/note.md", "demo/workspace/note.TXT", "demo/workspace/note.Txt",
    "demo/workspace/note.txt.bak", "demo/workspace/note.", "demo/workspace/notes",
    "demo/workspace/folder/note.json", "demo/workspace/.txt",
])
def test_note_destinations_must_end_in_a_literal_txt_suffix(root, value):
    actions = WorkspaceActions(root)
    try:
        with pytest.raises(WorkspaceActionError) as error:
            actions.prepare("create_note", {"path": value, "content": "body"})
        assert error.value.code in {"invalid_note_name", "invalid_path"}
    finally:
        actions.close()


@pytest.mark.parametrize("name", ["note.txt", "My note.txt", "a.txt.txt", "folder/note.txt"])
def test_accepted_note_destinations(root, name):
    (root / "demo" / "workspace" / "folder").mkdir(exist_ok=True)
    actions = WorkspaceActions(root)
    try:
        prepared = actions.prepare("create_note", {"path": f"demo/workspace/{name}", "content": "body"})
        assert prepared.path_parts[-1] == name.rsplit("/", 1)[-1]
    finally:
        actions.close()


@pytest.mark.parametrize("content", [
    "", " ", "\n", "\n\n\n", "  \n  ", "a\ta", "a\rb", "line\r\nnext\rcr",
    "esc\x1b[31mred", "nul\x00byte", "del\x7f", "nel\x85", "c1\x9b", "\x1c",
    "\ud800", "a" * 1025, "\n".join(["line"] * 300),
])
def test_note_content_must_be_bounded_plain_and_encodable(root, content):
    actions = WorkspaceActions(root)
    try:
        with pytest.raises(WorkspaceActionError) as error:
            actions.prepare("create_note", {"path": "demo/workspace/note.txt", "content": content})
        assert error.value.code == "invalid_content"
        assert not (root / "demo" / "workspace" / "note.txt").exists()
    finally:
        actions.close()


@pytest.mark.parametrize("content", [None, 5, b"bytes", ["a"], {"a": 1}, True])
def test_note_content_must_be_a_string(root, content):
    actions = WorkspaceActions(root)
    try:
        with pytest.raises(WorkspaceActionError) as error:
            actions.prepare("create_note", {"path": "demo/workspace/note.txt", "content": content})
        assert error.value.code == "invalid_content"
    finally:
        actions.close()


def test_note_bounds_are_inclusive_and_utf8_exact(root):
    actions = WorkspaceActions(root)
    try:
        # 1024 code points and exactly 4096 UTF-8 bytes are both accepted.
        widest = "\U0001f600" * 1024
        assert len(widest.encode("utf-8")) == 4096
        assert actions.validate_note_content(widest) == widest.encode("utf-8")
        assert actions.validate_note_content("a") == b"a"
        assert actions.validate_note_content("a\nb") == b"a\nb"
        with pytest.raises(WorkspaceActionError):
            actions.validate_note_content(widest + "a")
        with pytest.raises(WorkspaceActionError):
            actions.validate_note_content("a" * 4097)
    finally:
        actions.close()


def test_note_writes_utf8_bytes_and_needs_an_existing_parent(root):
    workspace = root / "demo" / "workspace"
    actions = WorkspaceActions(root)
    try:
        with pytest.raises(WorkspaceActionError) as missing:
            actions.prepare("create_note", {"path": "demo/workspace/missing/note.txt", "content": "body"})
        assert missing.value.code == "path_not_found"

        prepared = actions.prepare("create_note", {"path": "demo/workspace/note.txt", "content": "café über\nnewline"})
        assert actions.execute(prepared)["bytes"] == len("café über\nnewline".encode("utf-8"))
        assert (workspace / "note.txt").read_bytes() == "café über\nnewline".encode("utf-8")
    finally:
        actions.close()


def test_note_never_overwrites_a_file_directory_or_symlink(root):
    workspace = root / "demo" / "workspace"
    (workspace / "existing.txt").write_text("keep me")
    (workspace / "folder.txt").mkdir()
    (workspace / "link.txt").symlink_to(root / "outside.txt")
    (root / "outside.txt").write_text("unrelated")
    actions = WorkspaceActions(root)
    try:
        for name in ("existing.txt", "folder.txt", "link.txt"):
            with pytest.raises(WorkspaceActionError) as error:
                actions.prepare("create_note", {"path": f"demo/workspace/{name}", "content": "body"})
            assert error.value.code == "destination_exists"
        assert (workspace / "existing.txt").read_text() == "keep me"
        assert (root / "outside.txt").read_text() == "unrelated"
        assert (workspace / "link.txt").is_symlink()
    finally:
        actions.close()


def test_note_parent_swap_after_approval_is_denied(root):
    workspace = root / "demo" / "workspace" / "parent"
    workspace.mkdir(parents=True)
    actions = WorkspaceActions(root)
    try:
        prepared = actions.prepare("create_note", {"path": "demo/workspace/parent/note.txt", "content": "body"})
        (root / "demo" / "workspace" / "parent").rename(root / "demo" / "workspace" / "moved")
        (root / "demo" / "workspace" / "parent").mkdir()
        with pytest.raises(WorkspaceActionError) as error:
            actions.execute(prepared)
        assert error.value.code == "workspace_changed"
        assert not (root / "demo" / "workspace" / "parent" / "note.txt").exists()
    finally:
        actions.close()


def test_note_symlinked_parent_is_refused(root):
    (root / "demo" / "workspace" / "link").symlink_to(root)
    actions = WorkspaceActions(root)
    try:
        with pytest.raises(WorkspaceActionError) as error:
            actions.prepare("create_note", {"path": "demo/workspace/link/note.txt", "content": "body"})
        assert error.value.code == "path_not_found"
    finally:
        actions.close()


@pytest.mark.parametrize("arguments", [
    {"path": "demo/workspace/note.txt"},
    {"content": "body"},
    {"path": "demo/workspace/note.txt", "content": "body", "approved": True},
    {"path": "demo/workspace/note.txt", "content": "body", "approve": True},
])
def test_note_arguments_are_exactly_path_and_content(root, arguments):
    actions = WorkspaceActions(root)
    try:
        with pytest.raises(WorkspaceActionError) as error:
            actions.prepare("create_note", arguments)
        assert error.value.code in {"invalid_arguments", "invalid_note_name"}
        assert not any((root / "demo" / "workspace").iterdir())
    finally:
        actions.close()


def test_unknown_tool_is_still_refused(root):
    actions = WorkspaceActions(root)
    try:
        with pytest.raises(WorkspaceActionError) as error:
            actions.prepare("create_note_file", {"path": "demo/workspace/note.txt", "content": "body"})
        assert error.value.code == "unknown_tool"
    finally:
        actions.close()
