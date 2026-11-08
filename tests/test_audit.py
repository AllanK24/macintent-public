import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from macintent.audit import ActionAudit, AuditError


@pytest.fixture
def root():
    path = Path(tempfile.mkdtemp(prefix="action-audit-", dir=os.environ["TMPDIR"]))
    yield path


def test_fixed_private_database_and_safe_rows(root):
    audit = ActionAudit(root)
    try:
        audit.record(request_id="request", event="planned", tool="list_directory", result={"status": "planned"})
        rows = audit.events()
        assert rows[0]["event"] == "planned"
        assert rows[0]["result"] == {"status": "planned"}
        assert (root / ".local" / "state" / "actions.sqlite3").exists()
        assert not any("ticket" in row for row in rows[0])
    finally:
        audit.close()

def test_database_symlink_is_rejected(root):
    state = root / ".local" / "state"
    state.mkdir(parents=True)
    target = root / "outside.sqlite3"
    target.touch()
    (state / "actions.sqlite3").symlink_to(target)
    with pytest.raises(AuditError):
        ActionAudit(root)


def test_database_schema_is_append_only_shape(root):
    audit = ActionAudit(root)
    audit.close()
    with sqlite3.connect(root / ".local" / "state" / "actions.sqlite3") as db:
        columns = [row[1] for row in db.execute("PRAGMA table_info(events)")]
    assert columns == ["id", "occurred_at", "request_id", "tool", "event", "policy", "approval", "execution", "result_json", "error_code"]


def test_note_events_persist_paths_only(root):
    audit = ActionAudit(root)
    try:
        audit.record(
            request_id="note-1",
            event="planned",
            tool="create_note",
            policy="confirm",
            approval="required",
            result={"status": "planned", "arguments": {"path": "demo/workspace/note.txt"}},
        )
        audit.record(
            request_id="note-1",
            event="execution_completed",
            tool="create_note",
            policy="confirm",
            approval="approved",
            execution="completed",
            result={"status": "executed", "tool": "create_note", "details": {"path": "demo/workspace/note.txt", "bytes": 12, "created": True}},
        )
        rows = audit.events()
        assert [row["event"] for row in rows] == ["planned", "execution_completed"]
        assert rows[0]["result"]["arguments"] == {"path": "demo/workspace/note.txt"}
        assert rows[1]["result"]["details"] == {"path": "demo/workspace/note.txt", "bytes": 12, "created": True}
    finally:
        audit.close()


@pytest.mark.parametrize("result", [
    {"status": "planned", "arguments": {"path": "demo/workspace/note.txt", "content": "secret body"}},
    {"status": "executed", "tool": "create_note", "details": {"path": "demo/workspace/note.txt", "bytes": 1, "created": True, "content": "secret body"}},
    {"status": "executed", "tool": "create_note", "details": {"path": "demo/workspace/note.txt", "content": "secret body"}},
    {"status": "executed", "tool": "create_note", "details": {"entries": [{"name": "note.txt", "type": "file"}]}},
    {"status": "executed", "tool": "create_note", "details": {"source": "demo/workspace/note.txt", "bytes": 1}},
    {"status": "planned", "content": "secret body"},
    {"status": "planned", "body": "secret body"},
])
def test_note_bodies_and_foreign_metadata_are_refused(root, result):
    audit = ActionAudit(root)
    try:
        with pytest.raises(AuditError):
            audit.record(request_id="note-2", event="planned", tool="create_note", result=result)
        assert audit.events() == []
    finally:
        audit.close()


def test_unknown_tool_names_are_refused(root):
    audit = ActionAudit(root)
    try:
        with pytest.raises(AuditError):
            audit.record(request_id="note-3", event="planned", tool="create_note_file")
        with pytest.raises(AuditError):
            ActionAudit._validate_result({"status": "executed", "tool": "create_note_file", "details": {"path": "demo/workspace/note.txt"}})
        with pytest.raises(AuditError):
            ActionAudit._validate_result({"status": "executed", "tool": "copy_file", "details": {"path": "demo/workspace/note.txt", "created": True}})
        with pytest.raises(AuditError):
            audit.record(
                request_id="note-4",
                event="execution_completed",
                tool="create_note",
                result={"status": "executed", "tool": "list_directory", "details": {"path": "demo/workspace", "entries": []}},
            )
    finally:
        audit.close()
