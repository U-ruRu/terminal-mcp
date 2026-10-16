"""Android operator mutations followed by actual public MCP attach, not internal starts."""

from datetime import timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings
from test_access_mesh_operator_http import HEADERS
from test_access_slot_inheritance_http import create, mutate
from test_android_slot_http_window_qa import T0, read_slot
from test_android_slot_policy_continuation import set_clock

from terminal_mcp.app import create_app


def test_rotated_operator_code_connects_through_public_mcp(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = create(client, "public-rotation-create")
        rotated = mutate(
            client, "rotate", "public-rotation", slot_id=issued["slot_id"], expected_revision=1
        )
        assert bool(rotated["access_code"] != issued["access_code"])
        assert (
            call(client, "executor", "session", {"session_number": rotated["access_code"]}).get(
                "ok"
            )
            is True
        )
        # Read-back contains generation, never a bearer code.
        view = read_slot(client, issued["slot_id"])
        assert view["code_generation"] == 2
        assert "access_code" not in view


def test_subscription_hold_remains_an_explicit_rotation_guard(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = create(client, "held-code-create")
        store = app.state.access_mesh.store
        with store._connect() as db:
            db.execute(
                "INSERT INTO access_mesh_recipient_holds VALUES(?,?,?,?,?,?,'held',1)",
                (
                    "firstbyte",
                    "synthetic-subscription",
                    issued["logical_agent_id"],
                    "firstbyte",
                    issued["slot_id"],
                    issued["access_code"],
                ),
            )
        before = read_slot(client, issued["slot_id"])
        body = {
            "action": "rotate",
            "slot_id": issued["slot_id"],
            "expected_revision": 1,
            "idempotency_key": "held-code-rotate",
        }
        response = client.post("/actions/access/mutate", headers=HEADERS, json=body).json()
        assert response.get("ok") is False
        assert response.get("code") == "access_mesh_subscription_hold"
        assert read_slot(client, issued["slot_id"])["revision"] == before["revision"]
        assert (
            call(client, "executor", "session", {"session_number": issued["access_code"]}).get("ok")
            is True
        )
        with store._connect() as db:
            assert db.execute("SELECT state,revision FROM access_mesh_recipient_holds").fetchone()[
                :
            ] == ("held", 1)


def test_rotation_failure_rolls_back_tag_and_receipt_together(tmp_path, monkeypatch):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = create(client, "rotation-rollback-create")
        store = app.state.access_mesh.store
        original = store._after_apply

        def broken(db, event, **kwargs):
            original(db, event, **kwargs)
            if event.kind == "AccessCodeRotated":
                raise RuntimeError("synthetic post-receipt rollback")

        monkeypatch.setattr(store, "_after_apply", broken)
        body = {
            "action": "rotate",
            "slot_id": issued["slot_id"],
            "expected_revision": 1,
            "idempotency_key": "rotation-rollback-key",
        }
        assert (
            client.post("/actions/access/mutate", headers=HEADERS, json=body).json().get("ok")
            is False
        )
        assert read_slot(client, issued["slot_id"])["revision"] == 1
        assert bool(
            store.code_slot("firstbyte", issued["access_code"]).slot_id == issued["slot_id"]
        )
        monkeypatch.setattr(store, "_after_apply", original)
        result = client.post("/actions/access/mutate", headers=HEADERS, json=body).json()
        assert result.get("ok") is True
        assert read_slot(client, issued["slot_id"])["code_generation"] == 2


def test_explicit_deadline_does_not_claim_to_shorten_merged_max_window(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        issued = create(client, "deadline-no-false-shortening")
        before = read_slot(client, issued["slot_id"])
        response = client.post(
            "/actions/access/mutate",
            headers=HEADERS,
            json={
                "action": "deadline",
                "slot_id": issued["slot_id"],
                "expected_revision": 1,
                "idempotency_key": "deadline-shortening",
                "deadline_at": (T0 + timedelta(seconds=60)).isoformat(),
            },
        ).json()
        assert response.get("ok") is False
        assert response.get("code") == "access_mesh_invalid_time"
        assert read_slot(client, issued["slot_id"]) == before
