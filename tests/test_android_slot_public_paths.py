"""Android operator mutations followed by actual public MCP attach, not internal starts."""

from datetime import timedelta

from fastapi.testclient import TestClient
from test_access_mesh_mcp_runtime import call, settings
from test_access_mesh_operator_http import HEADERS
from test_access_slot_inheritance_http import create, defaults, mutate
from test_android_slot_http_window_qa import T0, read_slot
from test_android_slot_policy_continuation import set_clock

from terminal_mcp.app import create_app


def test_public_attach_and_operator_read_observe_the_same_active_window(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        issued = create(client, "public-view")
        assert (
            call(client, "executor", "session", {"session_number": issued["access_code"]}).get("ok")
            is True
        )
        view = read_slot(client, issued["slot_id"])
        slot = app.state.access_mesh.store.slot("firstbyte", issued["slot_id"])
        actual = app.state.access_mesh.store.observed_identity(slot, now=T0)
        assert view["session_lifecycle"] == actual["session_lifecycle"]
        assert view["session_lifecycle"]["state"] == "active"
        set_clock(app, T0 + timedelta(seconds=60))
        mutate(
            client,
            "policy",
            "public-future-policy",
            slot_id=issued["slot_id"],
            expected_revision=view["revision"],
            policy={"duration_seconds": 10800, "warning_seconds": 600},
        )
        changed = read_slot(client, issued["slot_id"])
        assert changed["policy"]["duration_seconds"] == 10800
        assert changed["current_policy"] == view["current_policy"]
        assert (
            changed["session_lifecycle"]["hard_expires_at"]
            == view["session_lifecycle"]["hard_expires_at"]
        )
        stored = app.state.access_mesh.store.slot("firstbyte", issued["slot_id"])
        assert stored.policy_window.started_at is not None
        assert stored.policy_window.hard_expires_at is not None


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


def test_explicit_deadline_for_publicly_created_slot_preserves_start(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        issued = create(client, "public-deadline-create")
        assert (
            call(client, "executor", "session", {"session_number": issued["access_code"]}).get("ok")
            is True
        )
        set_clock(app, T0 + timedelta(seconds=60))
        end = (T0 + timedelta(hours=2)).isoformat()
        mutate(
            client,
            "deadline",
            "public-deadline",
            slot_id=issued["slot_id"],
            expected_revision=1,
            deadline_at=end,
        )
        after = read_slot(client, issued["slot_id"])
        assert after["session_lifecycle"]["started_at"] == T0.isoformat(timespec="microseconds")
        assert after["session_lifecycle"]["hard_expires_at"] == (T0 + timedelta(hours=2)).isoformat(
            timespec="microseconds"
        )
        assert defaults(client)["revision"] == 1


def test_operator_end_fences_the_actual_number_window(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        issued = create(client, "public-end-create")
        mutate(client, "end", "public-end", slot_id=issued["slot_id"], expected_revision=1)
        outcome = call(client, "executor", "session", {"session_number": issued["access_code"]})
        assert outcome.get("ok") is False
        assert read_slot(client, issued["slot_id"])["session_lifecycle"]["state"] not in {
            "active",
            "warning",
            "draining",
        }


def test_rotation_retry_preserves_window_binding_and_stable_recipient_identity(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="https://terminal.example") as client:
        set_clock(app, T0)
        issued = create(client, "rotation-stability-create")
        assert (
            call(client, "executor", "session", {"session_number": issued["access_code"]}).get("ok")
            is True
        )
        store = app.state.access_mesh.store
        slot = store.slot("firstbyte", issued["slot_id"])
        before = store.observed_identity(slot, now=T0)
        claims = store.numbers.snapshot()
        receipt = mutate(
            client, "rotate", "rotation-stability-key", slot_id=slot.slot_id, expected_revision=1
        )
        replay = mutate(
            client, "rotate", "rotation-stability-key", slot_id=slot.slot_id, expected_revision=1
        )
        assert bool(receipt == replay)
        assert bool(claims == store.numbers.snapshot())
        after = store.observed_identity(store.slot("firstbyte", slot.slot_id), now=T0)
        assert after["work_session_id"] == before["work_session_id"]
        assert after["hard_expires_at"] == before["hard_expires_at"]
        assert after["public_name"] == before["public_name"]
        # Existing bindings survive; the retired code cannot admit a fresh connection.
        assert (
            call(client, "executor", "session", {"session_number": issued["access_code"]}).get("ok")
            is True
        )
        assert (
            call(
                client,
                "executor",
                "session",
                {"session_number": issued["access_code"]},
                conversation="retired-code-fresh",
            ).get("ok")
            is False
        )
        assert (
            call(
                client,
                "executor",
                "session",
                {"session_number": receipt["access_code"]},
                conversation="rotated-code-fresh",
            ).get("ok")
            is True
        )
        assert read_slot(client, slot.slot_id)["revision"] == 2


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
