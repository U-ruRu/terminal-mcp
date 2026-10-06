from types import SimpleNamespace

import pytest

from terminal_mcp.core.persistent_backend import PersistentBackend


class FakeContextStore:
    def __init__(self):
        self.seen = set()
        self.marked = []

    async def namespace_seen(self, work_session_id, namespace):
        return (work_session_id, namespace) in self.seen

    async def list(self, *, namespace, primary_first, limit):
        assert primary_first is True
        assert limit == 20
        return [
            {
                "id": 1,
                "summary": "Primary",
                "content": "Primary content",
                "primary": True,
                "namespace": namespace,
            },
            {
                "id": 2,
                "summary": "Additional",
                "content": "Additional content",
                "primary": False,
                "namespace": namespace,
            },
        ]

    async def mark_namespace_seen(self, work_session_id, namespace, *, seen_at):
        self.seen.add((work_session_id, namespace))
        self.marked.append((work_session_id, namespace, seen_at))


@pytest.mark.asyncio
async def test_namespace_context_auto_surfaces_at_most_once_per_work_session():
    store = FakeContextStore()
    backend = object.__new__(PersistentBackend)
    backend.service = SimpleNamespace(context_store=store)

    first = await backend._namespace_context_once("ws-1", "project")
    assert first["namespace"] == "project"
    assert [item["summary"] for item in first["primary"]] == ["Primary"]
    assert [item["summary"] for item in first["additional"]] == ["Additional"]
    assert len(store.marked) == 1

    assert await backend._namespace_context_once("ws-1", "project") is None
    assert len(store.marked) == 1

    second_session = await backend._namespace_context_once("ws-2", "project")
    assert second_session is not None
    assert len(store.marked) == 2
