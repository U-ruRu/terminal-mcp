"""An explicitly resumed window must obey a later second end in the same cycle."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings

from terminal_mcp.app import create_app
from terminal_mcp.core.access_mesh_grants import SlotPolicy


def test_two_separate_ends_of_one_cycle_remain_durable_and_fence_each_restart(tmp_path):
    app = create_app(settings(tmp_path))
    mesh = app.state.access_mesh
    now = [datetime.now(UTC).replace(microsecond=0)]
    mesh.clock = mesh.store.clock = lambda: now[0]
    mesh.defaults = SlotPolicy(60, 0, True)
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = call(client, "access", "session", {"action": "start"}, request_id=801)
        assert issued["ok"], issued
        number = issued["session_number"]
        assert call(client, "executor", "session", {"session_number": number}) == {"ok": True}
        now[0] += timedelta(seconds=5)
        assert call(client, "access", "session", {"action": "end"}, request_id=802) == {
            "ok": True,
        }
        now[0] += timedelta(seconds=5)
        resumed = call(client, "access", "session", {"action": "start"}, request_id=803)
        assert resumed == {"ok": True, "session_number": number}, resumed
        assert call(
            client,
            "executor",
            "command_run",
            {
                "command": "true",
            },
            request_id=804,
        )["ok"]

        now[0] += timedelta(seconds=5)
        again = call(client, "access", "session", {"action": "end"}, request_id=805)
        assert again == {"ok": True}, again
        assert len(mesh.store.numbers.end_snapshot()) == 2
        rejected = call(
            client,
            "executor",
            "command_run",
            {
                "command": "true",
            },
            request_id=806,
        )
        assert rejected["ok"] is False, rejected

        now[0] += timedelta(seconds=5)
        restarted = call(client, "access", "session", {"action": "start"}, request_id=807)
        assert restarted == {"ok": True, "session_number": number}
        assert call(
            client,
            "executor",
            "command_run",
            {
                "command": "true",
            },
            request_id=808,
        )["ok"]


def test_upgrade_legacy_single_end_table_preserves_history_and_allows_repeat(tmp_path):
    from terminal_mcp.storage.access_mesh import AccessMeshStore

    database = tmp_path / "ends-legacy.db"
    options = {
        "local_node_id": "firstbyte",
        "trusted_issuers": frozenset({"firstbyte", "bacloud"}),
        "proof_key": b"test-end-schema-migration-not-production",
    }
    previous = AccessMeshStore(database, **options)
    original = (
        "0319",
        "firstbyte:historical-slot:2026-10-09T10:00:00+00:00",
        "firstbyte",
        "historical-slot",
        "ae_original",
        "2026-10-09T10:00:05+00:00",
    )
    with previous._connect() as db:
        db.execute("DROP TABLE access_mesh_number_ends")
        db.execute(
            "CREATE TABLE access_mesh_number_ends("
            "number TEXT NOT NULL, cycle_key TEXT NOT NULL, "
            "issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL, "
            "event_id TEXT NOT NULL, ended_at TEXT NOT NULL, "
            "PRIMARY KEY(number,cycle_key))"
        )
        db.execute("INSERT INTO access_mesh_number_ends VALUES(?,?,?,?,?,?)", original)
    reopened = AccessMeshStore(database, **options)
    assert len(reopened.numbers.end_snapshot()) == 1
    with reopened._connect() as db:
        columns = list(db.execute("PRAGMA table_info(access_mesh_number_ends)"))
        assert next(col[5] for col in columns if col[1] == "event_id") == 3
    appended = reopened.numbers.record_end(
        number=original[0],
        cycle_key=original[1],
        issuer_id=original[2],
        slot_id=original[3],
        event_id="ae_later",
        ended_at="2026-10-09T10:00:15+00:00",
    )
    assert appended["ok"]
    assert len(reopened.numbers.end_snapshot()) == 2
    assert any(e["event_id"] == "ae_original" for e in reopened.numbers.end_snapshot())
    assert any(e["event_id"] == "ae_later" for e in reopened.numbers.end_snapshot())
    again = AccessMeshStore(database, **options)
    assert len(again.numbers.end_snapshot()) == 2
