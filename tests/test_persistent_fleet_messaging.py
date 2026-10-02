from types import SimpleNamespace

import pytest

from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.storage.persistent_agents import PersistentStoreError


class FakeTaskStore:
    async def get_task(self, namespace, task_id):
        return None

    async def active_claims(self, namespace, task_id):
        return []


class FakeFleetBridge:
    def __init__(self):
        self.deliveries = []
        self.permits = []
        self.inbox = []
        self.slots = []

    def ensure_permit_valid(self, permit):
        self.permits.append(permit)

    async def inbox_obligations(
        self,
        logical_agent_id,
        *,
        work_session_id,
        session_epoch,
    ):
        return list(self.inbox)

    async def deliver_message(
        self,
        *,
        logical_agent_id,
        sender_agent_id,
        text,
        require_reply=False,
        alert=False,
    ):
        if logical_agent_id == "la_inactive":
            raise PersistentStoreError("recipient_not_active")
        self.deliveries.append(
            {
                "logical_agent_id": logical_agent_id,
                "sender_agent_id": sender_agent_id,
                "text": text,
                "require_reply": require_reply,
                "alert": alert,
            }
        )
        return {"message_ref": f"home:msg:{len(self.deliveries)}"}

    async def list_access_slots(self):
        return list(self.slots)

    async def surface_obligations(
        self,
        *,
        logical_agent_id,
        work_session_id,
        session_epoch,
        obligations,
        retention_calls=5,
    ):
        refs = {item["message_ref"] for item in obligations}
        for item in self.inbox:
            if item.get("message_ref") not in refs:
                continue
            item["seen_count"] = int(item.get("seen_count") or 0) + 1
            item["first_seen_at"] = item.get("first_seen_at") or "2026-10-02T00:00:01Z"
            item["last_seen_at"] = "2026-10-02T00:00:01Z"
            if not item.get("require_reply") and not item.get("alert"):
                item["read_at"] = item.get("read_at") or "2026-10-02T00:00:01Z"

    async def message_inbox(
        self,
        logical_agent_id,
        *,
        work_session_id,
        session_epoch,
        show_all=False,
        recent_seconds=300,
        limit=50,
    ):
        return list(self.inbox)[:limit]


def make_backend():
    service = SimpleNamespace(
        repo=None,
        terminal=None,
        task_coordinator=None,
        task_store=FakeTaskStore(),
        agent_coordinator=None,
    )
    bridge = FakeFleetBridge()
    backend = PersistentBackend(service, SimpleNamespace(), fleet_bridge=bridge)
    state = {
        "identity": {
            "ok": True,
            "logical_agent_id": "la_sender",
            "work_session_id": "ws_sender",
            "session_epoch": 4,
            "public_name": "Sender",
        },
        "targets": {
            "Recipient": {
                "logical_agent_id": "la_recipient",
                "public_name": "Recipient",
                "status": "active",
            },
            "Inactive": {
                "logical_agent_id": "la_inactive",
                "public_name": "Inactive",
                "status": "active",
            },
            "Sender": {
                "logical_agent_id": "la_sender",
                "public_name": "Sender",
                "status": "active",
            },
        },
    }
    calls = []

    async def access_identity(code):
        assert code == "0042"
        return dict(state["identity"])

    async def execution_authority(
        logical_agent_id,
        work_session_id,
        session_epoch,
        *,
        scope,
        access_code=None,
    ):
        calls.append(
            (
                logical_agent_id,
                work_session_id,
                session_epoch,
                scope,
                access_code,
            )
        )
        return None, object()

    async def resolve_name(name):
        try:
            return dict(state["targets"][name])
        except KeyError as exc:
            raise PersistentStoreError("access_identity_not_found") from exc

    async def access_get(logical_agent_id):
        for item in state["targets"].values():
            if item["logical_agent_id"] == logical_agent_id:
                return dict(item)
        return None

    backend.access_identity = access_identity
    backend._execution_authority = execution_authority
    backend._resolve_access_name = resolve_name
    backend._access_get = access_get
    return backend, bridge, state, calls


@pytest.mark.asyncio
async def test_roaming_direct_message_uses_one_logical_identity_and_message_fence():
    backend, bridge, _, calls = make_backend()

    result = await backend.access_message(
        "Sender",
        access_code="0042",
        text="hello",
        target="Recipient",
    )

    assert result["ok"] is True
    assert result["sender"] == "Sender"
    assert result["delivered_to"] == ["Recipient"]
    assert result["scope"] == "direct"
    assert bridge.deliveries == [
        {
            "logical_agent_id": "la_recipient",
            "sender_agent_id": "la_sender",
            "text": "hello",
            "require_reply": False,
            "alert": False,
        }
    ]
    assert calls == [("la_sender", "ws_sender", 4, "message", "0042")]
    assert len(bridge.permits) == 1


@pytest.mark.asyncio
async def test_persistent_message_rejects_sender_spoof_and_self_message_explicitly():
    backend, bridge, _, _ = make_backend()

    spoof = await backend.access_message(
        "Recipient",
        access_code="0042",
        text="spoof",
        target="Recipient",
    )
    self_message = await backend.access_message(
        "Sender",
        access_code="0042",
        text="self",
        target="Sender",
    )

    assert spoof == {
        "ok": False,
        "code": "sender_not_authorized",
        "error": "sender_not_authorized",
    }
    assert self_message == {
        "ok": False,
        "code": "no_active_recipients",
        "error": "no_active_recipients",
    }
    assert bridge.deliveries == []


@pytest.mark.asyncio
async def test_persistent_broadcast_is_fleet_scoped_and_skips_inactive_sessions():
    backend, bridge, _, _ = make_backend()
    bridge.slots = [
        {
            "logical_agent_id": "la_sender",
            "public_name": "Sender",
            "status": "active",
        },
        {
            "logical_agent_id": "la_recipient",
            "public_name": "Recipient",
            "status": "active",
        },
        {
            "logical_agent_id": "la_inactive",
            "public_name": "Inactive",
            "status": "active",
        },
    ]

    result = await backend.access_message(
        "Sender",
        access_code="0042",
        text="fleet note",
        target="broadcast",
    )

    assert result["ok"] is True
    assert result["scope"] == "fleet"
    assert result["delivered_to"] == ["Recipient"]
    assert [item["logical_agent_id"] for item in bridge.deliveries] == ["la_recipient"]


@pytest.mark.asyncio
async def test_missing_and_inactive_recipient_have_distinct_failure_semantics():
    backend, _, _, _ = make_backend()

    missing = await backend.access_message(
        "Sender",
        access_code="0042",
        text="hello",
        target="Missing",
    )
    inactive = await backend.access_message(
        "Sender",
        access_code="0042",
        text="hello",
        target="Inactive",
    )

    assert missing["code"] == "recipient_not_found"
    assert inactive["code"] == "recipient_not_active"


@pytest.mark.asyncio
async def test_ended_sender_session_is_fenced_and_next_session_reuses_logical_identity():
    backend, bridge, state, calls = make_backend()
    state["identity"] = {
        "ok": False,
        "code": "session_not_found",
        "error": "session_not_found",
    }

    ended = await backend.access_message(
        "Sender",
        access_code="0042",
        text="must fail",
        target="Recipient",
    )
    assert ended["code"] == "session_not_found"
    assert calls == []
    assert bridge.deliveries == []

    state["identity"] = {
        "ok": True,
        "logical_agent_id": "la_sender",
        "work_session_id": "ws_next",
        "session_epoch": 5,
        "public_name": "Sender",
    }
    resumed = await backend.access_message(
        "Sender",
        access_code="0042",
        text="new session",
        target="Recipient",
    )

    assert resumed["ok"] is True
    assert bridge.deliveries[-1]["sender_agent_id"] == "la_sender"
    assert calls[-1] == ("la_sender", "ws_next", 5, "message", "0042")


@pytest.mark.asyncio
async def test_roaming_inbox_preserves_logical_sender_identity():
    backend, bridge, _, _ = make_backend()
    bridge.inbox = [
        {
            "message_ref": "home:msg:1",
            "sender_agent_id": "la_recipient",
            "text": "back",
            "require_reply": False,
            "alert": False,
            "created_at": "2026-10-02T00:00:00Z",
        }
    ]

    result = await backend.access_message("Sender", access_code="0042")

    assert result["ok"] is True
    assert result["messages"] == [
        {
            "message_hash": "home:msg:1",
            "sender": "Recipient",
            "text": "back",
            "mode": "notify",
            "state": "read",
            "created_at": "2026-10-02T00:00:00Z",
            "first_seen_at": "2026-10-02T00:00:01Z",
            "last_seen_at": "2026-10-02T00:00:01Z",
            "seen_count": 1,
            "read_at": "2026-10-02T00:00:01Z",
            "replied_at": None,
            "reply_message_hash": None,
            "namespace": None,
            "task_id": None,
        }
    ]
    assert result["inbox"] == result["messages"]
