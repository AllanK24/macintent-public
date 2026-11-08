"""Adversarial, synthetic coverage for the M3 action safety boundary.

All fixtures live below the project-controlled cache directory.  These tests
do not use the microphone, models, network, shell actions, or personal paths.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

import macintent.audit as audit_module
import macintent.broker as broker_module
import macintent.workspace as actions_module
from macintent.audit import ActionAudit, AuditError
from macintent.broker import Broker
from macintent.workspace import WorkspaceActionError, WorkspaceActions


PROJECT_TMP = Path(__file__).resolve().parents[1] / ".cache" / "tmp"


@pytest.fixture
def root():
    PROJECT_TMP.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="action-safety-", dir=PROJECT_TMP) as name:
        path = Path(name)
        (path / "demo" / "workspace").mkdir(parents=True)
        yield path


@pytest.fixture
def broker(root):
    instance = Broker(root)
    try:
        yield instance
    finally:
        instance.close()


def call(instance: Broker, ident: str, op: str, **fields):
    return instance.handle({"id": ident, "op": op, **fields})


def begin(instance: Broker, ident: str = "begin") -> str:
    reply = call(instance, ident, "begin")
    assert reply["ok"], reply
    return reply["result"]["request_id"]


def prepare_copy(instance: Broker, request_id: str, destination: str = "demo/workspace/copy.txt"):
    reply = call(
        instance,
        "prepare",
        "prepare",
        request_id=request_id,
        tool="copy_file",
        arguments={"source": "demo/workspace/source.txt", "destination": destination},
    )
    assert reply["ok"], reply
    return reply["result"]


#: A body that must never appear in the audit, in any event or column.
NOTE_SECRET = "synthetic note body 4d2f: buy milk, call the clinic, pay rent"


def prepare_note(instance: Broker, request_id: str, path: str = "demo/workspace/note.txt", content: str = NOTE_SECRET):
    reply = call(
        instance,
        "prepare",
        "prepare",
        request_id=request_id,
        tool="create_note",
        arguments={"path": path, "content": content},
    )
    assert reply["ok"], reply
    return reply["result"]


def approve(instance: Broker, request_id: str, ticket: str) -> None:
    assert call(instance, "approve", "decide", request_id=request_id, ticket=ticket, approve=True)["ok"]


def make_source(root: Path, name: str = "source.txt", content: bytes = b"fixture") -> Path:
    path = root / "demo" / "workspace" / name
    path.write_bytes(content)
    return path


def test_schema_is_strict_and_approve_is_not_model_tool_input(broker):
    hello = call(broker, "hello", "hello")
    assert hello["ok"]
    tools = {tool["name"]: tool for tool in hello["result"]["tools"]}
    assert set(tools) == {"list_directory", "create_directory", "copy_file", "create_note"}
    for tool in tools.values():
        schema = tool["parameters"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert "approve" not in schema["properties"]
        assert "approved" not in schema["properties"]
        assert set(schema["required"]) == set(schema["properties"])
        for name, prop in schema["properties"].items():
            assert prop["type"] == "string", name
            # A path is bounded at 512; a note body is bounded at 1024.
            assert prop["maxLength"] == (1024 if name == "content" else 512), name
            if name == "content":
                assert prop["minLength"] == 1

    note = tools["create_note"]["parameters"]
    assert set(note["properties"]) == {"path", "content"}
    assert note["required"] == ["path", "content"]
    assert note["properties"]["content"]["minLength"] == 1
    assert note["properties"]["content"]["maxLength"] == 1024

    request_id = begin(broker)
    extra = call(
        broker,
        "extra",
        "prepare",
        request_id=request_id,
        tool="list_directory",
        arguments={"path": "demo/workspace", "approve": True},
    )
    assert extra["ok"] is False
    assert extra["error"]["code"] == "invalid_arguments"

    top_level_extra = call(broker, "top-extra", "prepare", request_id=request_id, tool="list_directory", arguments={"path": "demo/workspace"}, approve=True)
    assert top_level_extra["ok"] is False
    assert top_level_extra["error"]["code"] == "invalid_request"


def test_a_note_cannot_carry_its_own_approval(broker, root):
    """The model may propose a note, never approve it."""

    request_id = begin(broker)
    for forged in ({"approved": True}, {"approve": True}, {"ticket": "guessed"}, {"confirmation_id": "guessed"}):
        reply = call(
            broker,
            "forged",
            "prepare",
            request_id=request_id,
            tool="create_note",
            arguments={"path": "demo/workspace/note.txt", "content": NOTE_SECRET, **forged},
        )
        assert reply["ok"] is False
        assert reply["error"]["code"] == "invalid_arguments"
        assert not (root / "demo" / "workspace" / "note.txt").exists()
        # One budget per request: a refused proposal is not retried.
        assert call(broker, "again", "begin")["error"]["code"] == "busy"
        assert call(broker, "end", "end", request_id=request_id, status="denied")["ok"]
        request_id = begin(broker, "next")


def test_expiry_replay_wrong_owner_cancel_and_completed_end(broker, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(broker_module.time, "time", lambda: clock[0])
    monkeypatch.setattr(broker_module.time, "monotonic", lambda: clock[0])

    request_id = begin(broker)
    planned = call(broker, "plan", "prepare", request_id=request_id, tool="list_directory", arguments={"path": "demo/workspace"})
    ticket = planned["result"]["ticket"]
    wrong_owner = call(broker, "wrong-owner", "execute", request_id="not-the-owner", ticket=ticket)
    assert wrong_owner["ok"] is False
    assert wrong_owner["error"]["code"] == "unknown_request"
    still_valid = call(broker, "owner", "execute", request_id=request_id, ticket=ticket)
    assert still_valid["ok"]
    assert call(broker, "finish", "end", request_id=request_id, status="completed")["ok"]

    request_id = begin(broker, "second-begin")
    planned = call(broker, "plan2", "prepare", request_id=request_id, tool="list_directory", arguments={"path": "demo/workspace"})
    ticket = planned["result"]["ticket"]
    clock[0] += 61
    expired = call(broker, "expired", "execute", request_id=request_id, ticket=ticket)
    assert expired["ok"] is False
    assert expired["error"]["code"] == "expired"
    replay = call(broker, "replay", "execute", request_id=request_id, ticket=ticket)
    assert replay["ok"] is False
    assert replay["error"]["code"] in {"invalid_ticket", "unknown_request"}

    reclaimed = call(broker, "reclaim", "begin")
    assert reclaimed["ok"]
    request_id = reclaimed["result"]["request_id"]
    not_completed = call(broker, "false-complete", "end", request_id=request_id, status="completed")
    assert not_completed["ok"] is False
    assert not_completed["error"]["code"] == "invalid_state"
    cancelled = call(broker, "cancel", "end", request_id=request_id, status="cancelled")
    assert cancelled["ok"]


def test_wrong_ticket_by_owner_burns_pending_ticket(broker):
    request_id = begin(broker)
    planned = call(broker, "plan", "prepare", request_id=request_id, tool="list_directory", arguments={"path": "demo/workspace"})
    ticket = planned["result"]["ticket"]
    wrong = call(broker, "wrong", "execute", request_id=request_id, ticket="not-the-ticket")
    assert wrong["ok"] is False
    burned = call(broker, "real", "execute", request_id=request_id, ticket=ticket)
    assert burned["ok"] is False
    assert burned["error"]["code"] in {"invalid_ticket", "invalid_state"}


def test_prepared_arguments_are_immutable_and_audited(broker, root):
    make_source(root)
    request_id = begin(broker)
    arguments = {"source": "demo/workspace/source.txt", "destination": "demo/workspace/immutable.txt"}
    planned = call(broker, "plan", "prepare", request_id=request_id, tool="copy_file", arguments=arguments)
    arguments["destination"] = "demo/workspace/changed.txt"
    ticket = planned["result"]["ticket"]
    assert call(broker, "approve", "decide", request_id=request_id, ticket=ticket, approve=True)["ok"]
    executed = call(broker, "execute", "execute", request_id=request_id, ticket=ticket)
    assert executed["ok"]
    assert (root / "demo" / "workspace" / "immutable.txt").exists()
    assert not (root / "demo" / "workspace" / "changed.txt").exists()
    planned_rows = [row for row in broker.audit_log.events() if row["event"] == "planned"]
    assert planned_rows[-1]["result"]["arguments"]["destination"] == "demo/workspace/immutable.txt"
    assert "source_identity" in planned_rows[-1]["result"]


def test_prepared_note_body_is_immutable_and_never_audited(broker, root):
    request_id = begin(broker)
    arguments = {"path": "demo/workspace/immutable note.txt", "content": "Original exact body"}
    planned = call(broker, "plan", "prepare", request_id=request_id, tool="create_note", arguments=arguments)
    arguments["content"] = "Changed after prepare"
    ticket = planned["result"]["ticket"]
    assert call(broker, "approve", "decide", request_id=request_id, ticket=ticket, approve=True)["ok"]
    executed = call(broker, "execute", "execute", request_id=request_id, ticket=ticket)
    assert executed["ok"]
    assert (root / "demo" / "workspace" / "immutable note.txt").read_text() == "Original exact body"
    encoded = json.dumps(broker.audit_log.events())
    assert "Original exact body" not in encoded
    assert "Changed after prepare" not in encoded


def test_changed_source_and_destination_after_approval_are_denied(root):
    make_source(root)
    broker = Broker(root)
    try:
        request_id = begin(broker)
        planned = prepare_copy(broker, request_id)
        ticket = planned["ticket"]
        assert call(broker, "approve", "decide", request_id=request_id, ticket=ticket, approve=True)["ok"]
        (root / "demo" / "workspace" / "source.txt").write_bytes(b"changed")
        changed = call(broker, "execute", "execute", request_id=request_id, ticket=ticket)
        assert changed["ok"] is False
        assert changed["error"]["code"] == "source_changed"
        assert call(broker, "failed-end", "end", request_id=request_id, status="failed")["ok"]

        request_id = begin(broker, "second")
        planned = prepare_copy(broker, request_id, "demo/workspace/other.txt")
        ticket = planned["ticket"]
        assert call(broker, "approve2", "decide", request_id=request_id, ticket=ticket, approve=True)["ok"]
        (root / "demo" / "workspace" / "other.txt").write_bytes(b"preexisting")
        exists = call(broker, "execute2", "execute", request_id=request_id, ticket=ticket)
        assert exists["ok"] is False
        assert exists["error"]["code"] == "destination_exists"
        assert (root / "demo" / "workspace" / "other.txt").read_bytes() == b"preexisting"
    finally:
        broker.close()


def test_root_and_parent_identity_changes_are_denied(root):
    actions = WorkspaceActions(root)
    make_source(root)
    try:
        listing = actions.prepare("list_directory", {"path": "demo/workspace"})
        moved = root.with_name(root.name + "-moved")
        root.rename(moved)
        root.mkdir()
        with pytest.raises(WorkspaceActionError) as root_error:
            actions.execute(listing)
        assert root_error.value.code == "workspace_changed"
    finally:
        actions.close()

    root.rmdir()  # This test created the empty replacement root above.
    moved.rename(root)  # Restore the owned fixture for TemporaryDirectory cleanup.

    # Use a fresh fixture path after the root replacement above.
    with tempfile.TemporaryDirectory(prefix="parent-swap-", dir=PROJECT_TMP) as name:
        fresh = Path(name)
        workspace = fresh / "demo" / "workspace" / "parent"
        workspace.mkdir(parents=True)
        (workspace / "source.txt").write_bytes(b"source")
        guarded = WorkspaceActions(fresh)
        try:
            prepared = guarded.prepare("copy_file", {"source": "demo/workspace/parent/source.txt", "destination": "demo/workspace/parent/destination.txt"})
            old = fresh / "demo" / "workspace" / "parent-old"
            (fresh / "demo" / "workspace" / "parent").rename(old)
            (fresh / "demo" / "workspace" / "parent").mkdir()
            with pytest.raises(WorkspaceActionError) as parent_error:
                guarded.execute(prepared)
            assert parent_error.value.code == "workspace_changed"
        finally:
            guarded.close()


def test_depth_and_copy_caps_and_list_cap(root):
    actions = WorkspaceActions(root)
    try:
        too_deep = "demo/workspace/" + "/".join(["a"] * 7)
        with pytest.raises(WorkspaceActionError) as depth_error:
            actions.prepare("list_directory", {"path": too_deep})
        assert depth_error.value.code == "invalid_path"

        oversized = root / "demo" / "workspace" / "oversized.bin"
        with oversized.open("wb") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
        with pytest.raises(WorkspaceActionError) as size_error:
            actions.prepare("copy_file", {"source": "demo/workspace/oversized.bin", "destination": "demo/workspace/out.bin"})
        assert size_error.value.code == "source_too_large"

        for index in range(201):
            (root / "demo" / "workspace" / f"entry-{index:03d}").write_bytes(b"")
        listing = actions.prepare("list_directory", {"path": "demo/workspace"})
        with pytest.raises(WorkspaceActionError) as entries_error:
            actions.execute(listing)
        assert entries_error.value.code == "too_many_entries"
    finally:
        actions.close()


def test_fifo_source_is_rejected_without_blocking(root):
    fifo = root / "demo" / "workspace" / "input.fifo"
    os.mkfifo(fifo)
    actions = WorkspaceActions(root)
    started = time.monotonic()
    try:
        with pytest.raises(WorkspaceActionError) as error:
            actions.prepare("copy_file", {"source": "demo/workspace/input.fifo", "destination": "demo/workspace/out"})
        assert error.value.code in {"invalid_source", "source_not_found"}
        assert time.monotonic() - started < 1.0
    finally:
        actions.close()


def test_second_parent_failure_releases_first_parent(root, monkeypatch):
    make_source(root)
    actions = WorkspaceActions(root)
    original = actions._open_parent
    calls = [0]

    def fail_second(parts):
        calls[0] += 1
        if calls[0] == 2:
            raise WorkspaceActionError("path_not_found")
        return original(parts)

    monkeypatch.setattr(actions, "_open_parent", fail_second)
    try:
        with pytest.raises(WorkspaceActionError):
            actions.prepare("copy_file", {"source": "demo/workspace/source.txt", "destination": "demo/workspace/destination.txt"})
    finally:
        monkeypatch.setattr(actions, "_open_parent", original)
        prepared = actions.prepare("copy_file", {"source": "demo/workspace/source.txt", "destination": "demo/workspace/destination.txt"})
        assert prepared.tool == "copy_file"
        actions.close()


@pytest.mark.parametrize("failure", ["write", "fsync", "fchmod"])
def test_copy_failures_cleanup_exclusive_destination(root, monkeypatch, failure):
    make_source(root)
    actions = WorkspaceActions(root)
    prepared = actions.prepare("copy_file", {"source": "demo/workspace/source.txt", "destination": "demo/workspace/failure.txt"})
    original = getattr(actions_module.os, failure)

    def fail(*args, **kwargs):
        raise OSError("synthetic failure")

    monkeypatch.setattr(actions_module.os, failure, fail)
    try:
        with pytest.raises(WorkspaceActionError) as error:
            actions.execute(prepared)
        assert error.value.code in {"copy_failed", "destination_failed"}
        assert not (root / "demo" / "workspace" / "failure.txt").exists()
    finally:
        monkeypatch.setattr(actions_module.os, failure, original)
        actions.close()


def test_unlink_failure_is_unknown_and_broker_stops(root, monkeypatch):
    make_source(root)
    broker = Broker(root)
    try:
        request_id = begin(broker)
        planned = prepare_copy(broker, request_id)
        ticket = planned["ticket"]
        assert call(broker, "approve", "decide", request_id=request_id, ticket=ticket, approve=True)["ok"]
        original_fsync = actions_module.os.fsync
        original_unlink = actions_module.os.unlink

        def fail_fsync(fd):
            raise OSError("synthetic fsync failure")

        def fail_unlink(path, *, dir_fd=None):
            if dir_fd is not None:
                raise OSError("synthetic unlink failure")
            return original_unlink(path)

        monkeypatch.setattr(actions_module.os, "fsync", fail_fsync)
        monkeypatch.setattr(actions_module.os, "unlink", fail_unlink)
        result = call(broker, "execute", "execute", request_id=request_id, ticket=ticket)
        assert result["ok"] is False
        assert result["error"]["code"] == "outcome_unknown"
        assert broker.stopped
        assert (root / "demo" / "workspace" / "copy.txt").exists()
        monkeypatch.setattr(actions_module.os, "fsync", original_fsync)
        monkeypatch.setattr(actions_module.os, "unlink", original_unlink)
        assert call(broker, "retry", "begin")["error"]["code"] == "internal_error"
    finally:
        broker.close()


def test_partial_destination_identity_is_never_removed(root, monkeypatch):
    make_source(root)
    actions = WorkspaceActions(root)
    destination = root / "demo" / "workspace" / "replaced.txt"
    prepared = actions.prepare("copy_file", {"source": "demo/workspace/source.txt", "destination": "demo/workspace/replaced.txt"})
    original_fchmod = actions_module.os.fchmod

    def replace_before_chmod(fd, mode):
        destination.unlink()
        destination.write_bytes(b"unrelated")
        raise OSError("synthetic chmod failure")

    monkeypatch.setattr(actions_module.os, "fchmod", replace_before_chmod)
    try:
        with pytest.raises(WorkspaceActionError) as error:
            actions.execute(prepared)
        assert error.value.code == "outcome_unknown"
        assert destination.read_bytes() == b"unrelated"
    finally:
        monkeypatch.setattr(actions_module.os, "fchmod", original_fchmod)
        actions.close()


def test_audit_failure_before_effect_denies_creation(root, monkeypatch):
    broker = Broker(root)
    try:
        request_id = begin(broker)
        planned = call(broker, "plan", "prepare", request_id=request_id, tool="create_directory", arguments={"path": "demo/workspace/not-created"})
        assert planned["ok"]
        original = broker.audit_log.record

        def fail_before(**kwargs):
            if kwargs.get("event") == "execution_started":
                raise AuditError("audit_unavailable")
            return original(**kwargs)

        monkeypatch.setattr(broker.audit_log, "record", fail_before)
        denied = call(broker, "execute", "execute", request_id=request_id, ticket=planned["result"]["ticket"])
        assert denied["ok"] is False
        assert not (root / "demo" / "workspace" / "not-created").exists()
        assert broker.stopped
    finally:
        broker.close()


def test_post_effect_audit_failure_stops_without_retry_and_keeps_started(root, monkeypatch):
    broker = Broker(root)
    try:
        request_id = begin(broker)
        planned = call(broker, "plan", "prepare", request_id=request_id, tool="create_directory", arguments={"path": "demo/workspace/created"})
        original = broker.audit_log.record

        def fail_after(**kwargs):
            if kwargs.get("event") == "execution_completed":
                raise AuditError("audit_unavailable")
            return original(**kwargs)

        monkeypatch.setattr(broker.audit_log, "record", fail_after)
        result = call(broker, "execute", "execute", request_id=request_id, ticket=planned["result"]["ticket"])
        assert result["ok"] is False
        assert broker.stopped
        assert (root / "demo" / "workspace" / "created").is_dir()
        monkeypatch.setattr(broker.audit_log, "record", original)
        events = broker.audit_log.events()
        assert any(event["event"] == "execution_started" for event in events)
        assert not any(event["event"] == "execution_completed" for event in events)
        assert call(broker, "retry", "begin")["error"]["code"] == "internal_error"
    finally:
        broker.close()


def test_audit_db_identity_sidecars_schema_and_triggers_are_guarded(root):
    audit = ActionAudit(root)
    db = audit.path
    replacement = root / "replacement.sqlite3"
    replacement.write_bytes(db.read_bytes())
    os.replace(replacement, db)
    with pytest.raises(AuditError):
        audit.events()
    audit.close()

    sidecar_audit = ActionAudit(root)
    sidecar = Path(str(sidecar_audit.path) + "-journal")
    sidecar.symlink_to(root)
    with pytest.raises(AuditError):
        sidecar_audit.events()
    sidecar_audit.close()
    sidecar.unlink()

    hardlink_audit = ActionAudit(root)
    sidecar_target = root / "sidecar-target"
    sidecar_target.write_bytes(b"sidecar")
    os.link(sidecar_target, Path(str(hardlink_audit.path) + "-shm"))
    with pytest.raises(AuditError):
        hardlink_audit.events()
    hardlink_audit.close()
    Path(str(hardlink_audit.path) + "-shm").unlink()

    hardlink_audit = ActionAudit(root)
    hardlink_audit.close()
    with sqlite3.connect(hardlink_audit.path) as connection:
        connection.execute("CREATE TRIGGER unsafe_trigger AFTER INSERT ON events BEGIN SELECT 1; END")
    with pytest.raises(AuditError):
        ActionAudit(root)


def test_audit_schema_result_privacy_and_budgets(root, monkeypatch):
    audit = ActionAudit(root)
    try:
        with pytest.raises(AuditError):
            audit.record(request_id="safe", event="planned", result={"status": "planned", "ticket": "secret"})
        with pytest.raises(AuditError):
            audit.record(request_id="safe", event="planned", result={"status": "planned", "arguments": {"path": "../escape"}})

        audit.record(request_id="safe", event="began", result={"status": "begun"})
        monkeypatch.setattr(audit_module, "MAX_AUDIT_EVENTS", 1)
        with pytest.raises(AuditError):
            audit.record(request_id="safe", event="began", result={"status": "begun"})
    finally:
        audit.close()

    oversized = root / "oversized-actions.sqlite3"
    # A valid audit database grown beyond the fixed storage ceiling must not
    # become usable merely because its bytes are otherwise SQLite-shaped.
    capped = ActionAudit(root)
    capped.close()
    os.truncate(capped.path, audit_module.MAX_DATABASE_BYTES + 1)
    with pytest.raises(AuditError):
        ActionAudit(root)
    assert not oversized.exists()


def test_audit_response_is_last_200_and_under_wire_cap(broker):
    long_component = "a" * 64
    long_path = "demo/workspace/" + "/".join([long_component] * 6)
    destination = "demo/workspace/" + "/".join(["b" * 64] * 6)
    for index in range(210):
        broker.audit_log.record(
            request_id=f"request-{index}",
            event="planned",
            tool="copy_file",
            policy="confirm",
            approval="required",
            result={"status": "planned", "arguments": {"source": long_path, "destination": destination}},
        )
    response = call(broker, "audit", "audit")
    assert response["ok"]
    assert len(response["result"]["events"]) <= 200
    assert response["result"]["truncated"] is True
    assert len(json.dumps(response, separators=(",", ":")).encode()) <= 64 * 1024


def test_completed_action_is_not_reclaimed_by_its_old_approval_deadline(broker, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(broker_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(broker_module.time, "time", lambda: clock[0])
    request_id = begin(broker)
    planned = call(broker, "plan", "prepare", request_id=request_id, tool="list_directory", arguments={"path": "demo/workspace"})
    assert call(broker, "execute", "execute", request_id=request_id, ticket=planned["result"]["ticket"])["ok"]
    clock[0] += 61
    assert call(broker, "next", "begin")["error"]["code"] == "busy"
    assert not any(row["event"] == "ended" for row in broker.audit_log.events())
    assert call(broker, "end", "end", request_id=request_id, status="cancelled")["ok"]
    ended = broker.audit_log.events()[-1]
    assert ended["execution"] == "completed"
    assert ended["result"] == {"status": "cancelled", "action_outcome": "completed"}


def test_begin_only_request_has_bounded_lifetime(broker, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(broker_module.time, "monotonic", lambda: clock[0])
    first = begin(broker)
    clock[0] += 181
    second = begin(broker, "new")
    assert second != first
    ended = next(row for row in broker.audit_log.events() if row["request_id"] == first and row["event"] == "ended")
    assert ended["execution"] == "none"


def test_failed_execution_has_no_extra_policy_rejection_event(broker, root):
    make_source(root)
    request_id = begin(broker)
    planned = prepare_copy(broker, request_id)
    assert call(broker, "yes", "decide", request_id=request_id, ticket=planned["ticket"], approve=True)["ok"]
    (root / "demo/workspace/source.txt").write_bytes(b"changed fixture")
    assert not call(broker, "execute", "execute", request_id=request_id, ticket=planned["ticket"])["ok"]
    events = broker.audit_log.events()
    assert events[-1]["event"] == "execution_failed"
    assert events[-1]["execution"] == "failed"


def test_close_preserves_a_successful_action_outcome(root):
    instance = Broker(root)
    request_id = begin(instance)
    planned = call(instance, "plan", "prepare", request_id=request_id, tool="list_directory", arguments={"path": "demo/workspace"})
    assert call(instance, "execute", "execute", request_id=request_id, ticket=planned["result"]["ticket"])["ok"]
    instance.close()
    audit = ActionAudit(root)
    try:
        assert audit.events()[-1]["execution"] == "completed"
        assert audit.events()[-1]["result"]["action_outcome"] == "completed"
    finally:
        audit.close()


def test_audit_requires_private_existing_directories_and_sidecars(root):
    audit = ActionAudit(root)
    state = root / ".local/state"
    state.chmod(0o755)
    try:
        with pytest.raises(AuditError):
            audit.events()
    finally:
        state.chmod(0o700)
        audit.close()
    audit = ActionAudit(root)
    sidecar = Path(str(audit.path) + "-journal")
    sidecar.write_bytes(b"synthetic sidecar")
    sidecar.chmod(0o644)
    try:
        with pytest.raises(AuditError):
            audit.events()
    finally:
        sidecar.unlink()
        audit.close()


def test_list_entry_io_failure_is_not_silently_reported_as_success(root, monkeypatch):
    make_source(root)
    actions = WorkspaceActions(root)
    listing = actions.prepare("list_directory", {"path": "demo/workspace"})
    original = actions_module.os.stat

    def fail(name, *args, **kwargs):
        if name == "source.txt" and kwargs.get("dir_fd") is not None:
            raise OSError("synthetic stat I/O failure")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(actions_module.os, "stat", fail)
    try:
        with pytest.raises(WorkspaceActionError) as error:
            actions.execute(listing)
        assert error.value.code == "list_failed"
    finally:
        actions.close()


def test_a_note_is_created_only_after_a_fresh_trusted_approval(broker, root):
    workspace = root / "demo" / "workspace"
    request_id = begin(broker)
    planned = prepare_note(broker, request_id)
    assert planned["status"] == "confirmation_required"
    assert planned["action"]["arguments"] == {"path": "demo/workspace/note.txt", "content": NOTE_SECRET}
    # The summary shown to the trusted user names the destination and size only.
    assert planned["summary"] == f"Create note demo/workspace/note.txt ({len(NOTE_SECRET)} characters)"
    assert NOTE_SECRET not in planned["summary"]
    ticket = planned["ticket"]

    # An unapproved execute is refused and burns the request, like any copy.
    unapproved = call(broker, "unapproved", "execute", request_id=request_id, ticket=ticket)
    assert unapproved["ok"] is False
    assert unapproved["error"]["code"] == "invalid_state"
    assert not (workspace / "note.txt").exists()
    assert call(broker, "end", "end", request_id=request_id, status="failed")["ok"]

    request_id = begin(broker, "second")
    planned = prepare_note(broker, request_id)
    ticket = planned["ticket"]
    # A decision for one note never approves another tool or a new request.
    assert call(broker, "for-tool", "decide", request_id=request_id, ticket=ticket, approve=False)["ok"]
    declined = call(broker, "declined", "decide", request_id=request_id, ticket=ticket, approve=True)
    assert declined["ok"] is False
    assert declined["error"]["code"] in {"invalid_ticket", "invalid_state"}
    assert not (workspace / "note.txt").exists()
    assert call(broker, "end2", "end", request_id=request_id, status="denied")["ok"]

    request_id = begin(broker, "third")
    planned = prepare_note(broker, request_id)
    ticket = planned["ticket"]
    approve(broker, request_id, ticket)
    executed = call(broker, "execute", "execute", request_id=request_id, ticket=ticket)
    assert executed["ok"]
    assert executed["result"]["status"] == "executed"
    assert executed["result"]["tool"] == "create_note"
    assert executed["result"]["details"] == {"path": "demo/workspace/note.txt", "bytes": len(NOTE_SECRET.encode("utf-8")), "created": True}
    note = workspace / "note.txt"
    assert note.read_text() == NOTE_SECRET
    assert note.stat().st_mode & 0o777 == 0o600
    assert note.stat().st_nlink == 1
    assert call(broker, "replay", "execute", request_id=request_id, ticket=ticket)["ok"] is False
    assert call(broker, "completed", "end", request_id=request_id, status="completed")["ok"]


def test_a_declined_note_leaves_no_file_and_is_audited_as_declined(root):
    broker = Broker(root)
    try:
        request_id = begin(broker)
        planned = prepare_note(broker, request_id)
        declined = call(broker, "no", "decide", request_id=request_id, ticket=planned["ticket"], approve=False)
        assert declined["result"] == {"status": "declined"}
        assert not (root / "demo" / "workspace" / "note.txt").exists()
        # A declined note cannot then be executed with its spent ticket.
        assert call(broker, "after", "execute", request_id=request_id, ticket=planned["ticket"])["ok"] is False
        assert not (root / "demo" / "workspace" / "note.txt").exists()
        events = [row for row in broker.audit_log.events() if row["request_id"] == request_id]
        assert [row["event"] for row in events] == ["began", "planned", "declined", "rejected"]
        assert events[1]["policy"] == "confirm" and events[1]["approval"] == "required"
        assert events[2]["approval"] == "declined" and events[2]["result"]["status"] == "declined"
        assert events[1]["result"]["arguments"] == {"path": "demo/workspace/note.txt"}
    finally:
        broker.close()


def test_a_note_is_rejected_before_any_approval_for_unsafe_arguments(root):
    broker = Broker(root)
    try:
        cases = [
            ({"path": "demo/workspace/note.md", "content": "body"}, {"invalid_note_name"}),
            ({"path": "demo/workspace/note.txt", "content": ""}, {"invalid_content"}),
            ({"path": "demo/workspace/note.txt", "content": "a\tb"}, {"invalid_content"}),
            ({"path": "demo/workspace/note.txt", "content": "x" * 1025}, {"invalid_content"}),
            ({"path": "demo/workspace/../escape.txt", "content": "body"}, {"invalid_path"}),
            ({"path": "demo/workspace/missing/note.txt", "content": "body"}, {"path_not_found"}),
            ({"path": "demo/workspace", "content": "body"}, {"invalid_path"}),
        ]
        for index, (arguments, expected) in enumerate(cases):
            request_id = begin(broker, f"begin-{index}")
            reply = call(broker, "prepare", "prepare", request_id=request_id, tool="create_note", arguments=arguments)
            assert reply["ok"] is False, arguments
            assert reply["error"]["code"] in expected, arguments
            assert list((root / "demo" / "workspace").iterdir()) == []
            assert call(broker, "end", "end", request_id=request_id, status="denied")["ok"]
    finally:
        broker.close()


def test_an_approved_note_never_overwrites_and_is_checked_again_at_execution(root):
    (root / "demo" / "workspace" / "note.txt").write_text("preexisting")
    broker = Broker(root)
    try:
        request_id = begin(broker)
        # The destination already exists, so the proposal never becomes a ticket.
        refused = call(broker, "prepare", "prepare", request_id=request_id, tool="create_note", arguments={"path": "demo/workspace/note.txt", "content": NOTE_SECRET})
        assert refused["error"]["code"] == "destination_exists"
        assert (root / "demo" / "workspace" / "note.txt").read_text() == "preexisting"
        assert call(broker, "end", "end", request_id=request_id, status="denied")["ok"]

        # A file that appears between approval and execution is still refused.
        request_id = begin(broker, "second")
        planned = prepare_note(broker, request_id, path="demo/workspace/second.txt")
        approve(broker, request_id, planned["ticket"])
        (root / "demo" / "workspace" / "second.txt").write_text("written by someone else")
        executed = call(broker, "execute", "execute", request_id=request_id, ticket=planned["ticket"])
        assert executed["ok"] is False
        assert executed["error"]["code"] == "destination_exists"
        assert (root / "demo" / "workspace" / "second.txt").read_text() == "written by someone else"
        assert (root / "demo" / "workspace" / "note.txt").read_text() == "preexisting"
    finally:
        broker.close()


def test_an_approved_note_never_writes_through_a_symlink(root):
    workspace = root / "demo" / "workspace"
    target = root / "outside.txt"
    target.write_text("unrelated")

    # A symlink already at the destination is refused before any approval.
    (workspace / "note.txt").symlink_to(target)
    broker = Broker(root)
    try:
        request_id = begin(broker)
        refused = call(broker, "prepare", "prepare", request_id=request_id, tool="create_note", arguments={"path": "demo/workspace/note.txt", "content": NOTE_SECRET})
        assert refused["error"]["code"] == "destination_exists"
        assert target.read_text() == "unrelated"
        assert call(broker, "end", "end", request_id=request_id, status="denied")["ok"]
    finally:
        broker.close()

    # A symlink that appears after approval is refused at execution, and the
    # link's own target is still never written through.
    (workspace / "note.txt").unlink()
    broker = Broker(root)
    try:
        request_id = begin(broker, "second")
        planned = prepare_note(broker, request_id)
        approve(broker, request_id, planned["ticket"])
        (workspace / "note.txt").symlink_to(target)
        executed = call(broker, "execute", "execute", request_id=request_id, ticket=planned["ticket"])
        assert executed["ok"] is False
        assert executed["error"]["code"] == "destination_exists"
        assert target.read_text() == "unrelated"
        assert (workspace / "note.txt").is_symlink()
    finally:
        broker.close()


def test_note_body_never_reaches_the_audit_or_its_reply(root):
    workspace = root / "demo" / "workspace"
    broker = Broker(root)
    try:
        request_id = begin(broker)
        planned = prepare_note(broker, request_id)
        approve(broker, request_id, planned["ticket"])
        assert call(broker, "execute", "execute", request_id=request_id, ticket=planned["ticket"])["ok"]
        assert call(broker, "end", "end", request_id=request_id, status="completed")["ok"]

        events = broker.audit_log.events()
        note_events = [row for row in events if row["request_id"] == request_id]
        assert [row["event"] for row in note_events] == [
            "began", "planned", "approved", "execution_started", "execution_completed", "ended",
        ]
        planned_row = next(row for row in note_events if row["event"] == "planned")
        assert planned_row["result"]["arguments"] == {"path": "demo/workspace/note.txt"}
        assert "source_identity" not in planned_row["result"]
        completed = next(row for row in note_events if row["event"] == "execution_completed")
        assert completed["result"]["tool"] == "create_note"
        assert completed["result"]["details"] == {"path": "demo/workspace/note.txt", "bytes": len(NOTE_SECRET.encode("utf-8")), "created": True}

        # No event, column, or audit reply carries any part of the body.
        encoded_events = json.dumps(events, sort_keys=True, ensure_ascii=True)
        assert NOTE_SECRET not in encoded_events
        assert "buy milk" not in encoded_events and "call the clinic" not in encoded_events
        assert "content" not in encoded_events
        reply = call(broker, "audit", "audit")
        assert NOTE_SECRET not in json.dumps(reply, sort_keys=True)
        with sqlite3.connect(broker.audit_log.path) as db:
            stored = db.execute("SELECT result_json, tool, error_code FROM events").fetchall()
        assert stored and all(NOTE_SECRET not in (row[0] or "") and row[1] != "content" for row in stored)
        with open(broker.audit_log.path, "rb") as stream:
            assert NOTE_SECRET.encode("utf-8") not in stream.read()
        # The body itself is still written, once, to the note.
        assert (workspace / "note.txt").read_text() == NOTE_SECRET
    finally:
        broker.close()


def test_note_failures_leave_no_partial_file_and_report_one_static_code(root, monkeypatch):
    for failure in ("write", "fsync", "fchmod"):
        with tempfile.TemporaryDirectory(prefix="note-failure-", dir=PROJECT_TMP) as name:
            path = Path(name)
            (path / "demo" / "workspace").mkdir(parents=True)
            broker = Broker(path)
            try:
                request_id = begin(broker)
                planned = prepare_note(broker, request_id, path=f"demo/workspace/{failure}.txt", content=f"body for {failure}")
                approve(broker, request_id, planned["ticket"])
                original = getattr(actions_module.os, failure)

                def fail(*args, **kwargs):
                    raise OSError("synthetic failure")

                monkeypatch.setattr(actions_module.os, failure, fail)
                executed = call(broker, "execute", "execute", request_id=request_id, ticket=planned["ticket"])
                monkeypatch.setattr(actions_module.os, failure, original)
                assert executed["ok"] is False
                assert executed["error"]["code"] == "note_failed"
                assert list((path / "demo" / "workspace").iterdir()) == []
                events = broker.audit_log.events()
                assert events[-1]["event"] == "execution_failed"
                assert events[-1]["error"] == "note_failed"
                assert events[-1]["execution"] == "failed"
                assert NOTE_SECRET not in json.dumps(events)
            finally:
                broker.close()


def test_note_unlink_failure_is_unknown_and_stops_the_broker(root, monkeypatch):
    broker = Broker(root)
    try:
        request_id = begin(broker)
        planned = prepare_note(broker, request_id)
        approve(broker, request_id, planned["ticket"])
        original_fsync = actions_module.os.fsync
        original_unlink = actions_module.os.unlink

        def fail_fsync(fd):
            raise OSError("synthetic fsync failure")

        def fail_unlink(path, *, dir_fd=None):
            if dir_fd is not None:
                raise OSError("synthetic unlink failure")
            return original_unlink(path)

        monkeypatch.setattr(actions_module.os, "fsync", fail_fsync)
        monkeypatch.setattr(actions_module.os, "unlink", fail_unlink)
        result = call(broker, "execute", "execute", request_id=request_id, ticket=planned["ticket"])
        assert result["ok"] is False
        assert result["error"]["code"] == "outcome_unknown"
        assert broker.stopped
        assert (root / "demo" / "workspace" / "note.txt").exists()
        monkeypatch.setattr(actions_module.os, "fsync", original_fsync)
        monkeypatch.setattr(actions_module.os, "unlink", original_unlink)
        assert call(broker, "retry", "begin")["error"]["code"] == "internal_error"
    finally:
        broker.close()


def test_a_replaced_note_is_never_removed_by_cleanup(root, monkeypatch):
    destination = root / "demo" / "workspace" / "replaced.txt"
    broker = Broker(root)
    try:
        request_id = begin(broker)
        planned = prepare_note(broker, request_id, path="demo/workspace/replaced.txt", content="original body")
        approve(broker, request_id, planned["ticket"])
        original_fchmod = actions_module.os.fchmod

        def replace_before_chmod(fd, mode):
            destination.unlink()
            destination.write_bytes(b"unrelated")
            raise OSError("synthetic chmod failure")

        monkeypatch.setattr(actions_module.os, "fchmod", replace_before_chmod)
        result = call(broker, "execute", "execute", request_id=request_id, ticket=planned["ticket"])
        monkeypatch.setattr(actions_module.os, "fchmod", original_fchmod)
        assert result["ok"] is False
        assert result["error"]["code"] == "outcome_unknown"
        assert destination.read_bytes() == b"unrelated"
    finally:
        broker.close()


def test_note_audit_failure_before_the_effect_denies_creation(root, monkeypatch):
    broker = Broker(root)
    try:
        request_id = begin(broker)
        planned = prepare_note(broker, request_id)
        approve(broker, request_id, planned["ticket"])
        original = broker.audit_log.record

        def fail_before(**kwargs):
            if kwargs.get("event") == "execution_started":
                raise AuditError("audit_unavailable")
            return original(**kwargs)

        monkeypatch.setattr(broker.audit_log, "record", fail_before)
        denied = call(broker, "execute", "execute", request_id=request_id, ticket=planned["ticket"])
        assert denied["ok"] is False
        assert not (root / "demo" / "workspace" / "note.txt").exists()
        assert broker.stopped
    finally:
        broker.close()


def test_note_confirmation_is_required_for_a_non_confirmed_tool(broker):
    """A decision is refused for a tool that never asked for one."""

    request_id = begin(broker)
    planned = call(broker, "plan", "prepare", request_id=request_id, tool="create_directory", arguments={"path": "demo/workspace/folder"})
    assert planned["ok"] and planned["result"]["status"] == "ready"
    refused = call(broker, "decide", "decide", request_id=request_id, ticket=planned["result"]["ticket"], approve=True)
    assert refused["ok"] is False
    assert refused["error"]["code"] == "invalid_state"
