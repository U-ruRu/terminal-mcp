"""Explicit operator HTTP business keys replay; transport request ids remain irrelevant."""

from types import SimpleNamespace

import pytest

from terminal_mcp.core.persistent_backend import PersistentBackend
from terminal_mcp.storage.persistent_agents import PersistentAgentStore
from terminal_mcp.storage.sqlite import SqliteRepository


class FailingAuditBackend(PersistentBackend):
    def __init__(self, store):
        self.lifecycle = SimpleNamespace(store=store)
        self.service = SimpleNamespace(events=SimpleNamespace(emit=self._raise))

    @staticmethod
    def _raise(*args, **kwargs):
        raise RuntimeError("diagnostics sink unavailable")

    async def _audit(self, *args, **kwargs):
        raise RuntimeError("audit sink unavailable")


@pytest.mark.asyncio
async def test_explicit_key_replays_committed_receipt_after_audit_failure(tmp_path):
    repo = SqliteRepository(tmp_path / "state.db", tmp_path / "output.db")
    await repo.initialize()
    backend = FailingAuditBackend(PersistentAgentStore(repo.path))
    commits = []

    async def commit():
        commits.append(1)
        return {"ok": True, "revision": 2}

    first = await backend._idempotent(
        "operator", "slot.play", "explicit-key-0001",
        {"expected_revision": 1}, commit,
    )
    assert first == {"ok": True, "revision": 2}

    # A new backend instance represents a restarted server. The explicit
    # operation id is stable, whereas JSON-RPC correlation ids may be reused.
    restarted = FailingAuditBackend(PersistentAgentStore(repo.path))
    replay = await restarted._idempotent(
        "operator", "slot.play", "explicit-key-0001",
        {"expected_revision": 1}, commit,
    )
    assert replay == first
    assert len(commits) == 1

    conflict = await restarted._idempotent(
        "operator", "slot.play", "explicit-key-0001",
        {"expected_revision": 2}, commit,
    )
    assert conflict["ok"] is False
    assert conflict["code"] == "idempotency_conflict"
    assert len(commits) == 1
