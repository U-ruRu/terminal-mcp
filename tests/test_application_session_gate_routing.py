from types import SimpleNamespace

import pytest

from terminal_mcp.application import ActorContext, TerminalApplication
from terminal_mcp.core.persistent_admission import VerifiedAdmissionContext


def actor() -> ActorContext:
    return ActorContext.from_admission(
        VerifiedAdmissionContext(
            "owner",
            "credential:owner",
            frozenset({"terminal:execute"}),
            1,
            "internal",
            "bearer",
        ),
        node_id="node-a",
    )


class Service:
    # Deliberately has no message/session methods: these calls must route through SessionGate.
    persistent = object()


class RecordingGate:
    def __init__(self):
        self.calls = []

    async def resolve_message_actor(self, actor_value, sender, code):
        self.calls.append(("resolve_message_actor", sender, code))
        return SimpleNamespace(
            failure=None,
            actor=actor_value,
            identity={"ok": True, "logical_agent_id": "agent"},
        )

    async def message(self, resolution, sender, **kwargs):
        self.calls.append(("message", sender, kwargs.get("access_code")))
        return {"ok": True, "action": "send"}

    async def stop(self, actor_value, code, *, interrupt=False):
        self.calls.append(("stop", code, interrupt))
        return {"ok": True, "action": "interrupt" if interrupt else "end"}


@pytest.mark.asyncio
async def test_message_uses_common_session_gate_instead_of_persistent_backend():
    app = TerminalApplication(Service())
    gate = RecordingGate()
    app.session_gate = gate
    app.messages.gate = gate

    result = await app.message(actor(), sender="owner", code="0042", text="hello")

    assert result == {"ok": True, "action": "send"}
    assert gate.calls == [
        ("resolve_message_actor", "owner", "0042"),
        ("message", "owner", "0042"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "interrupt"), [("end", False), ("interrupt", True)])
async def test_session_stop_uses_common_session_gate(action, interrupt):
    app = TerminalApplication(Service())
    gate = RecordingGate()
    app.session_gate = gate
    app.sessions.gate = gate

    result = await app.session(actor(), action=action, code="0042")

    assert result["ok"] is True
    assert gate.calls == [("stop", "0042", interrupt)]
