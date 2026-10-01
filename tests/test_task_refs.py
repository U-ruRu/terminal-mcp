import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000)
    return repo, terminal, service


async def register(service, summary):
    plan = {
        "task_summary": summary,
        "intent": summary,
        "details": [summary],
        "work_scope": [f"test:{summary}"],
    }
    proposed = await service.agent_start(**plan)
    started = await service.agent_start(agent_id=proposed["proposed_agent_id"], **plan)
    return started["self"]["agent_id"]


@pytest.mark.asyncio
async def test_refs_versioning_review_history_and_audit(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = await register(service, "owner")
        reviewer = await register(service, "reviewer")
        created = await service.task(
            owner,
            action="create",
            namespace="project",
            task_id="REFS-1",
            title="refs",
            isolation_hint="none",
            input_refs=["kb:q_1", "kb:q_1", "git:abc"],
            output_refs=["git:one", "git:one"],
        )
        assert created["ok"] is True
        task = created["task"]
        assert task["input_refs"] == ["kb:q_1", "git:abc"]
        assert task["output_refs"] == ["git:one"]
        first_state = task["output_state_id"]
        assert isinstance(first_state, int)

        claimed = await service.task(
            owner,
            action="claim",
            namespace="project",
            task_id="REFS-1",
            claim_intent="change refs",
        )
        assert claimed["ok"] is True

        changed = await service.task(
            owner,
            action="update",
            namespace="project",
            task_id="REFS-1",
            input_refs=["file:context.md"],
            output_refs=["git:two", "kb:dec_2", "git:two"],
        )
        assert changed["ok"] is True
        second_state = changed["task"]["output_state_id"]
        assert second_state != first_state
        assert changed["task"]["input_refs"] == ["file:context.md"]
        assert changed["task"]["output_refs"] == ["git:two", "kb:dec_2"]

        identical = await service.task(
            owner,
            action="update",
            namespace="project",
            task_id="REFS-1",
            output_refs=["git:two", "kb:dec_2"],
        )
        assert identical["ok"] is True
        assert identical["task"]["output_state_id"] == second_state

        reviewed = await service.task(
            reviewer,
            action="review",
            namespace="project",
            task_id="REFS-1",
            dimensions=["C"],
            verdict="NON_BLOCKING",
            evidence={"tests": "ok"},
        )
        assert reviewed["ok"] is True

        changed_again = await service.task(
            owner,
            action="update",
            namespace="project",
            task_id="REFS-1",
            output_refs=["git:three"],
        )
        third_state = changed_again["task"]["output_state_id"]
        assert third_state not in {first_state, second_state}

        details = await service.tasks(
            namespace="project",
            task_id="REFS-1",
            show_details=True,
            show_done=True,
        )
        detailed = details.get("task") or details["tasks"][0]
        assert [s["output_state_id"] for s in detailed["output_states"]] == [
            first_state,
            second_state,
            third_state,
        ]
        old_review = next(r for r in detailed["reviews"] if r["dimension"] == "C")
        assert old_review["output_state_id"] == second_state
        assert old_review["output_refs"] == ["git:two", "kb:dec_2"]
        assert old_review["reviewer"]

        output_events = [e for e in detailed["events"] if e["event_type"] == "output_refs_updated"]
        assert output_events[0]["payload"] == {
            "previous_output_state_id": second_state,
            "new_output_state_id": third_state,
            "previous_output_refs": ["git:two", "kb:dec_2"],
            "new_output_refs": ["git:three"],
        }
        input_events = [e for e in detailed["events"] if e["event_type"] == "input_refs_updated"]
        assert input_events[-1]["payload"]["input_refs"] == ["file:context.md"]

        cycled = await service.task(
            owner,
            action="update",
            namespace="project",
            task_id="REFS-1",
            output_refs=["git:two", "kb:dec_2"],
        )
        fourth_state = cycled["task"]["output_state_id"]
        assert fourth_state not in {first_state, second_state, third_state}
        assert (
            await service.task(
                reviewer,
                action="review",
                namespace="project",
                task_id="REFS-1",
                dimensions=["C"],
                verdict="NON_BLOCKING",
            )
        )["ok"]
        cycled_details = await service.tasks(
            namespace="project",
            task_id="REFS-1",
            show_details=True,
            show_done=True,
        )
        c_reviews = [
            review
            for review in cycled_details["task"]["reviews"]
            if review["dimension"] == "C" and review["output_refs"] == ["git:two", "kb:dec_2"]
        ]
        assert {review["output_state_id"] for review in c_reviews} == {
            second_state,
            fourth_state,
        }
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_ref_validation_and_review_requirements_follow_current_output_state(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = await register(service, "owner")
        reviewer = await register(service, "reviewer")

        empty = await service.task(
            owner,
            action="create",
            namespace="project",
            task_id="EMPTY-REFS",
            title="empty refs",
            isolation_hint="none",
        )
        assert empty["ok"] is True
        assert empty["task"]["input_refs"] == []
        assert empty["task"]["output_refs"] == []
        assert isinstance(empty["task"]["output_state_id"], int)

        invalid_values = [
            [""],
            [" leading"],
            ["trailing "],
            ["x" * 513],
            [f"ref:{i}" for i in range(65)],
        ]
        for index, refs in enumerate(invalid_values):
            result = await service.task(
                owner,
                action="create",
                namespace="project",
                task_id=f"BAD-{index}",
                title="bad refs",
                isolation_hint="none",
                input_refs=refs,
            )
            assert result["ok"] is False

        created = await service.task(
            owner,
            action="create",
            namespace="project",
            task_id="REQ-1",
            title="requirements",
            isolation_hint="none",
            output_refs=["git:v1"],
        )
        assert created["ok"] is True
        await service.task_coordinator.store.update_task("project", "REQ-1", reviews=["C"])
        assert (
            await service.task(
                owner,
                action="claim",
                namespace="project",
                task_id="REQ-1",
                claim_intent="finish reviewed output",
            )
        )["ok"]

        missing = await service.task(
            owner,
            action="done",
            namespace="project",
            task_id="REQ-1",
            result={"ok": True},
        )
        assert missing["ok"] is False
        assert missing["code"] == "review_requirements_unsatisfied"

        assert (
            await service.task(
                reviewer,
                action="review",
                namespace="project",
                task_id="REQ-1",
                dimensions=["C"],
                verdict="NON_BLOCKING",
            )
        )["ok"]

        changed = await service.task(
            owner,
            action="update",
            namespace="project",
            task_id="REQ-1",
            output_refs=["git:v2"],
        )
        assert changed["ok"]
        stale = await service.task(
            owner,
            action="done",
            namespace="project",
            task_id="REQ-1",
            result={"ok": True},
        )
        assert stale["ok"] is False
        assert stale["code"] == "review_requirements_unsatisfied"

        assert (
            await service.task(
                reviewer,
                action="review",
                namespace="project",
                task_id="REQ-1",
                dimensions=["C"],
                verdict="NON_BLOCKING",
            )
        )["ok"]
        done = await service.task(
            owner,
            action="done",
            namespace="project",
            task_id="REQ-1",
            result={"ok": True},
        )
        assert done["ok"] is True
    finally:
        await terminal.stop()
