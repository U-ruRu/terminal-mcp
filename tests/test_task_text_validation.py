import pytest

from terminal_mcp.core.public_errors import normalize_public_error
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,field,value",
    [
        ("create", "isolation_hint", " "),
        ("claim", "claim_intent", " \n"),
        ("release", "release_reason", " "),
        ("comment", "comment_text", " "),
    ],
)
async def test_stateful_text_prerequisites_are_validation_failures(
    tmp_path, monkeypatch, action, field, value
):
    repo = SqliteRepository(tmp_path / "test.sqlite3")
    await repo.initialize()
    store = TaskStore(repo.path)
    coordinator = TaskCoordinator(store)
    if action != "create":
        await store.create_task("test", "one", "fixture")
    if action == "release":
        await store.claim("test", "one", "agent", claim_intent="fixture")

    async def claims(*args):
        return await store.claims("test", "one", active_only=True)

    monkeypatch.setattr(coordinator, "_live_claims", claims)
    before = await store.get_task("test", "one")
    events = await store.list_events("test", "one")
    kwargs = {} if value is None else {field: value}
    result = await coordinator.mutate(
        "agent", action=action, namespace="test", task_id="one", **kwargs
    )
    assert result["ok"] is False
    public = normalize_public_error(result).as_dict()
    assert public["code"] == "input_validation_failed"
    assert result["path"] == field
    assert public["details"]["validation_errors"][0]["path"] == field
    assert await store.get_task("test", "one") == before
    assert await store.list_events("test", "one") == events
    if action == "release":
        assert len(await store.claims("test", "one", active_only=True)) == 1
