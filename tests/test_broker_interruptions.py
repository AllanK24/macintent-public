"""Interrupted effects have unknown outcomes; dispatch is mocked before effects."""

import pytest

from macintent.broker import Broker
from macintent.audit import ActionAudit


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_interrupted_dispatch_is_audited_unknown(tmp_path, monkeypatch, failure):
    (tmp_path / "demo/workspace").mkdir(parents=True)

    def interrupted(_request):
        raise failure()

    broker = Broker(str(tmp_path), confirm_writes=True)
    monkeypatch.setattr(broker.actions, "execute", interrupted)
    try:
        def call(op, **fields):
            return broker.handle({"id": op, "op": op, **fields})

        request_id = call("begin")["result"]["request_id"]
        prepared = call("prepare", request_id=request_id, tool="create_directory", arguments={"path": "demo/workspace/Synthetic"})["result"]
        assert call("decide", request_id=request_id, ticket=prepared["ticket"], approve=True)["ok"] is True
        if failure is RuntimeError:
            assert call("execute", request_id=request_id, ticket=prepared["ticket"])["error"]["code"] == "outcome_unknown"
        else:
            with pytest.raises(failure):
                call("execute", request_id=request_id, ticket=prepared["ticket"])
        assert broker.active.execution_state == "unknown"
        assert broker.stopped is True
        events = broker.audit_log.events(200)
        failed = [event for event in events if event["event"] == "execution_failed"]
        assert len(failed) == 1
        assert failed[0]["execution"] == "unknown"
        assert failed[0]["error"] == "outcome_unknown"
        assert not any(event["result"] and event["result"].get("status") == "cancelled" for event in events)
    finally:
        broker.close()


@pytest.mark.parametrize(
    ("state", "expected_status", "expected_outcome"),
    [("started", "failed", "unknown"), ("none", "cancelled", "none")],
)
def test_close_fallback_distinguishes_started_from_undispatched(tmp_path, state, expected_status, expected_outcome):
    (tmp_path / "demo/workspace").mkdir(parents=True)
    broker = Broker(str(tmp_path))
    begun = broker.handle({"id": "begin", "op": "begin"})["result"]
    # Synthetic interruption window; no filesystem/native action is dispatched.
    broker.active.execution_state = state
    broker.close()
    audit = ActionAudit(tmp_path)
    try:
        ended = [event for event in audit.events(200) if event["event"] == "ended"]
        assert len(ended) == 1
        assert ended[0]["request_id"] == begun["request_id"]
        assert ended[0]["execution"] == expected_outcome
        assert ended[0]["result"] == {"status": expected_status, "action_outcome": expected_outcome}
    finally:
        audit.close()
