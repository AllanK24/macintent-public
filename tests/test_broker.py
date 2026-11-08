import json
import os
import tempfile
from pathlib import Path

import pytest

from macintent.broker import Broker


@pytest.fixture
def broker():
    root = Path(tempfile.mkdtemp(prefix="action-broker-", dir=os.environ["TMPDIR"]))
    workspace = root / "demo" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "source.txt").write_text("fixture")
    instance = Broker(root)
    yield instance
    instance.close()


def call(broker, ident, op, **fields):
    return broker.handle({"id": ident, "op": op, **fields})


def test_hello_schema_and_confirmation_flow(broker):
    hello = call(broker, "1", "hello")
    assert hello["ok"]
    assert hello["result"]["workspace"] == "demo/workspace"
    assert hello["result"]["max_actions"] == 1
    assert all(not tool["parameters"].get("properties", {}).get("approve") for tool in hello["result"]["tools"])

    request = call(broker, "2", "begin")["result"]["request_id"]
    prepared = call(broker, "3", "prepare", request_id=request, tool="copy_file", arguments={"source": "demo/workspace/source.txt", "destination": "demo/workspace/copy.txt"})
    assert prepared["result"]["status"] == "confirmation_required"
    ticket = prepared["result"]["ticket"]
    assert call(broker, "4", "decide", request_id=request, ticket=ticket, approve=True)["result"] == {"status": "approved"}
    executed = call(broker, "5", "execute", request_id=request, ticket=ticket)
    assert executed["result"]["status"] == "executed"
    assert call(broker, "6", "execute", request_id=request, ticket=ticket)["ok"] is False


def test_traversal_and_duplicate_requests_fail_closed(broker):
    request = call(broker, "1", "begin")["result"]["request_id"]
    bad = call(broker, "2", "prepare", request_id=request, tool="list_directory", arguments={"path": "demo/workspace/../"})
    assert bad["ok"] is False
    assert call(broker, "3", "prepare", request_id=request, tool="list_directory", arguments={"path": "demo/workspace"})["ok"] is False
    assert call(broker, "4", "begin")["error"]["code"] == "busy"


def test_reject_and_audit_are_bounded(broker):
    request = call(broker, "1", "begin")["result"]["request_id"]
    assert call(broker, "2", "reject", request_id=request, reason="cancelled")["result"] == {"status": "denied"}
    events = call(broker, "3", "audit")["result"]["events"]
    assert len(events) <= 200
    assert events[-1]["event"] == "rejected"


def test_note_is_published_confirmed_and_written_privately(broker):
    workspace = broker.actions.root_path / "demo" / "workspace"
    hello = call(broker, "1", "hello")["result"]
    assert "create_note" in {tool["name"] for tool in hello["tools"]}

    request = call(broker, "2", "begin")["result"]["request_id"]
    prepared = call(broker, "3", "prepare", request_id=request, tool="create_note", arguments={"path": "demo/workspace/note.txt", "content": "buy milk"})
    assert prepared["result"]["status"] == "confirmation_required"
    ticket = prepared["result"]["ticket"]
    assert call(broker, "4", "execute", request_id=request, ticket=ticket)["error"]["code"] == "invalid_state"
    assert call(broker, "5", "end", request_id=request, status="failed")["ok"]

    request = call(broker, "6", "begin")["result"]["request_id"]
    prepared = call(broker, "7", "prepare", request_id=request, tool="create_note", arguments={"path": "demo/workspace/note.txt", "content": "buy milk"})
    ticket = prepared["result"]["ticket"]
    assert call(broker, "8", "decide", request_id=request, ticket=ticket, approve=True)["result"] == {"status": "approved"}
    executed = call(broker, "9", "execute", request_id=request, ticket=ticket)
    assert executed["result"]["details"] == {"path": "demo/workspace/note.txt", "bytes": 8, "created": True}
    assert (workspace / "note.txt").read_bytes() == b"buy milk"
    assert (workspace / "note.txt").stat().st_mode & 0o777 == 0o600
    # The body is in no audited event.
    assert "buy milk" not in json.dumps(call(broker, "10", "audit")["result"])
