import pytest

from terminal_mcp.core.orchestration import public_agent_name
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import ClaimOwner
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
    return await service.agent_start(agent_id=proposed["proposed_agent_id"], **plan)


@pytest.mark.asyncio
async def test_create_requires_and_projects_isolation_hint(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = (await register(service, "isolation"))["self"]["agent_id"]
        missing = await service.task(
            agent, action="create", namespace="project", task_id="ISO-MISSING", title="missing"
        )
        assert missing["ok"] is False
        assert "isolation_hint" in missing["error"]

        too_long = await service.task(
            agent,
            action="create",
            namespace="project",
            task_id="ISO-LONG",
            title="too long",
            isolation_hint="x" * 161,
        )
        assert too_long["ok"] is False
        assert "maximum length is 160" in too_long["error"]

        created = await service.task(
            agent,
            action="create",
            namespace="project",
            task_id="ISO-1",
            title="isolation contract",
            isolation_hint="  separate worktree  ",
        )
        assert created["ok"] is True
        assert created["task"]["isolation_hint"] == "separate worktree"

        claimed = await service.task(
            agent,
            action="claim",
            namespace="project",
            task_id="ISO-1",
            claim_intent="verify task context",
        )
        assert claimed["ok"] is True
        fleet = await service.agents(agent)
        assert fleet["self"]["managed_tasks"][0]["isolation_hint"] == "separate worktree"

        changed = await service.task(
            agent,
            action="update",
            namespace="project",
            task_id="ISO-1",
            isolation_hint="none",
        )
        assert changed["ok"] is False
        assert "set only when creating" in changed["error"]
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_task_create_cooperative_claims_and_compact_listing(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        one = (await register(service, "one"))["self"]["agent_id"]
        two = (await register(service, "two"))["self"]["agent_id"]
        created = await service.task(
            one,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="REV-1",
            title="Review slice",
            lane="review",
            priority="P1",
            cooperative=True,
            resource_context={"repo": "/srv/repo"},
        )
        assert created["ok"] is True
        assert created["task"]["priority"] == "P1"
        assert "description" not in created["task"]

        first = await service.task(
            one,
            action="claim",
            namespace="project",
            task_id="REV-1",
            claim_intent="reviewing shared slice",
        )
        second = await service.task(
            two,
            action="claim",
            namespace="project",
            task_id="REV-1",
            claim_intent="cooperating on review slice",
        )
        assert first["ok"] is True
        assert second["ok"] is True
        assert first["warnings"] == []
        assert all(item["severity"] == "info" for item in second["warnings"])
        assert {item["agent_name"] for item in second["task"]["claims"]} == {
            public_agent_name(one),
            public_agent_name(two),
        }

        fleet = await service.agents(one)
        assert len(fleet["self"]["managed_tasks"]) == 1
        managed = fleet["self"]["managed_tasks"][0]
        assert {
            "namespace": managed["namespace"],
            "task_id": managed["task_id"],
            "lane": managed["lane"],
            "priority": managed["priority"],
            "state": managed["state"],
        } == {
            "namespace": "project",
            "task_id": "REV-1",
            "lane": "review",
            "priority": "P1",
            "state": "ready",
        }
        assert managed["claim_intent"] == "reviewing shared slice"
        assert managed["role"] == "owner"
        assert managed["claimed_at"]
        assert managed["claim_age_seconds"] >= 0
        health = await service.health("none")
        assert health["workflow"]["by_lane"] == {"review": 1}
        assert health["workflow"]["active_claims"] == 2
        assert health["workflow"]["stale_claims"] == 0

        recovery = await service.recovery("printf task-event", agent_id=one)
        assert recovery["ok"] is True
        listing = await service.tasks(namespace="project")
        assert listing["ok"] is True
        assert listing["summary"]["by_lane"] == {"review": 1}
        assert listing["tasks"][0]["task_id"] == "REV-1"
        assert "resource_context" not in listing["tasks"][0]
        detailed = await service.tasks(namespace="project", task_id="REV-1", show_details=True)
        assert detailed["task"]["resource_context"] == {"repo": "/srv/repo"}
        assert not any(
            item["event_type"] == "command"
            and item["payload"].get("command_hash") == recovery["cmd_hash"]
            for item in detailed["task"]["events"]
        )
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_task_addressed_message_routes_to_live_claimants_and_persists(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        sender = (await register(service, "sender"))["self"]["agent_id"]
        worker = (await register(service, "worker"))["self"]["agent_id"]
        await service.task(
            sender,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="REV-2",
            title="Task mailbox",
        )
        claimed = await service.task(
            worker,
            action="claim",
            namespace="project",
            task_id="REV-2",
            claim_intent="handling task-addressed messages",
        )
        worker_name = claimed["task"]["claims"][0]["agent_name"]

        sent = await service.message(
            sender,
            text="Please review the candidate contract",
            namespace="project",
            task_id="REV-2",
            require_reply=True,
        )
        assert sent["ok"] is True
        assert sent["namespace"] == "project"
        assert sent["task_id"] == "REV-2"
        assert sent["delivered_to"] == [worker_name]

        surfaced = await service.read("deadbeef", agent_id=worker)
        assert any(
            "task project/REV-2" in line and sent["message_hash"] in line
            for line in surfaced["pending_messages"]
        )
        ack = await service.message(worker, message_hash=sent["message_hash"])
        assert ack["read_by"] == [worker_name]
        assert ack["namespace"] == "project"
        assert ack["task_id"] == "REV-2"
        reply = await service.message(
            worker, message_hash=sent["message_hash"], text="Contract reviewed"
        )
        assert reply["namespace"] == "project"
        assert reply["task_id"] == "REV-2"

        detail = await service.tasks(namespace="project", task_id="REV-2", show_details=True)
        messages = [item for item in detail["task"]["events"] if item["event_type"] == "message"]
        by_hash = {item["payload"]["message_hash"]: item for item in messages}
        assert by_hash[sent["message_hash"]]["payload"]["text"] == (
            "Please review the candidate contract"
        )
        assert by_hash[reply["reply_message_hash"]]["payload"]["reply_to"] == sent["message_hash"]
        assert by_hash[reply["reply_message_hash"]]["payload"]["text"] == "Contract reviewed"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_task_addressed_message_is_durable_without_live_claimants(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        sender = (await register(service, "sender"))["self"]["agent_id"]
        await service.task(
            sender,
            action="create",
            isolation_hint="none",
            namespace="server",
            task_id="TASK-1",
            title="Unclaimed task",
        )
        sent = await service.message(
            sender,
            text="Durable note for the next claimant",
            namespace="server",
            task_id="TASK-1",
        )
        assert sent["ok"] is True
        assert sent["delivered_to"] == []
        detail = await service.tasks(namespace="server", task_id="TASK-1", show_details=True)
        assert any(
            item["event_type"] == "message"
            and item["payload"]["message_hash"] == sent["message_hash"]
            for item in detail["task"]["events"]
        )
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_agent_finish_releases_claim_with_durable_event(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent_id = (await register(service, "finisher"))["self"]["agent_id"]
        await service.task(
            agent_id,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="FINISH-1",
            title="Finish releases claim",
        )
        await service.task(
            agent_id,
            action="claim",
            namespace="project",
            task_id="FINISH-1",
            claim_intent="testing session finish release",
        )

        finished = await service.agent_finish(agent_id)
        assert finished["ok"] is True
        assert finished["finished"] is True

        detail = await service.tasks(namespace="project", task_id="FINISH-1", show_details=True)
        assert detail["task"]["claims"] == []
        release = next(
            event for event in detail["task"]["events"] if event["event_type"] == "claim_released"
        )
        assert release["payload"]["reason"] == "agent_finish"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_review_lane_is_ordinary_task_and_requires_result_to_finish(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = (await register(service, "review-owner"))["self"]["agent_id"]
        created = await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="CANDIDATE-1",
            title="Candidate review",
            lane="review",
            candidate_ref="opaque-candidate-ref",
        )
        assert created["ok"] is True
        assert created["task"]["lane"] == "review"
        assert created["task"]["candidate_ref"] == "opaque-candidate-ref"

        missing = await service.task(
            owner, action="done", namespace="project", task_id="CANDIDATE-1"
        )
        assert missing["ok"] is False
        assert "result" in missing["error"]
        claimed = await service.task(
            owner,
            action="claim",
            namespace="project",
            task_id="CANDIDATE-1",
            claim_intent="review candidate to completion",
        )
        assert claimed["ok"] is True

        result = {
            "verdict": "accepted",
            "evidence": {"tests": "green", "candidate_ref": "opaque-candidate-ref"},
        }
        done = await service.task(
            owner,
            action="done",
            namespace="project",
            task_id="CANDIDATE-1",
            result=result,
        )
        assert done["ok"] is True
        assert done["task"]["state"] == "done"
        detail = await service.tasks(namespace="project", task_id="CANDIDATE-1", show_details=True)
        assert detail["task"]["result"] == result
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_invalid_dependencies_do_not_partially_create_or_update_task(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent_id = (await register(service, "dependency-validator"))["self"]["agent_id"]
        created = await service.task(
            agent_id,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="BAD-DEPS",
            title="Bad dependencies",
            dependencies=[{}],
        )
        assert created["ok"] is False
        assert "task_id: required" in created["error"]
        missing = await service.tasks(namespace="project", task_id="BAD-DEPS", show_details=True)
        assert missing["ok"] is False
        assert missing["task"] is None

        await service.task(
            agent_id,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="GOOD",
            title="Original title",
        )
        updated = await service.task(
            agent_id,
            action="update",
            namespace="project",
            task_id="GOOD",
            title="Should not apply",
            dependencies=[{}],
        )
        assert updated["ok"] is False
        current = await service.tasks(namespace="project", task_id="GOOD", show_details=True)
        assert current["task"]["title"] == "Original title"

        duplicate = [
            {"namespace": "project", "task_id": "DEP"},
            {"namespace": "project", "task_id": "DEP"},
        ]
        partial_create = await service.task(
            agent_id,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="ATOMIC-CREATE",
            title="Must roll back",
            dependencies=duplicate,
        )
        assert partial_create["ok"] is False
        missing_atomic = await service.tasks(
            namespace="project", task_id="ATOMIC-CREATE", show_details=True
        )
        assert missing_atomic["task"] is None

        before = await service.tasks(namespace="project", task_id="GOOD", show_details=True)
        before_revision = before["task"]["revision"]
        partial_update = await service.task(
            agent_id,
            action="update",
            namespace="project",
            task_id="GOOD",
            title="Must also roll back",
            dependencies=duplicate,
        )
        assert partial_update["ok"] is False
        after = await service.tasks(namespace="project", task_id="GOOD", show_details=True)
        assert after["task"]["title"] == "Original title"
        assert after["task"]["revision"] == before_revision
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_release_without_claim_does_not_create_false_release_event(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent_id = (await register(service, "release-idempotent"))["self"]["agent_id"]
        await service.task(
            agent_id,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="RELEASE-1",
            title="Idempotent release",
        )
        released = await service.task(
            agent_id, action="release", namespace="project", task_id="RELEASE-1"
        )
        assert released["ok"] is True
        detail = await service.tasks(namespace="project", task_id="RELEASE-1", show_details=True)
        assert all(event["event_type"] != "claim_released" for event in detail["task"]["events"])
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_repeated_state_transitions_do_not_require_transition_context(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent_id = (await register(service, "state-idempotent"))["self"]["agent_id"]

        await service.task(
            agent_id,
            action="create",
            namespace="project",
            task_id="STATE-DONE",
            title="Idempotent done state",
            isolation_hint="none",
        )
        await service.task(
            agent_id,
            action="claim",
            namespace="project",
            task_id="STATE-DONE",
            claim_intent="complete once, then repeat state",
        )
        first_done = await service.task(
            agent_id,
            action="state",
            namespace="project",
            task_id="STATE-DONE",
            state="done",
            result={"summary": "completed"},
        )
        assert first_done["ok"] is True
        repeated_done = await service.task(
            agent_id,
            action="state",
            namespace="project",
            task_id="STATE-DONE",
            state="done",
        )
        assert repeated_done["ok"] is True
        assert repeated_done["task"]["state"] == "done"

        await service.task(
            agent_id,
            action="create",
            namespace="project",
            task_id="STATE-BLOCKED",
            title="Idempotent blocked state",
            isolation_hint="none",
        )
        await service.task(
            agent_id,
            action="claim",
            namespace="project",
            task_id="STATE-BLOCKED",
            claim_intent="block once, then repeat state",
        )
        first_blocked = await service.task(
            agent_id,
            action="state",
            namespace="project",
            task_id="STATE-BLOCKED",
            state="blocked",
            blocker_reason="Waiting for dependency",
        )
        assert first_blocked["ok"] is True
        repeated_blocked = await service.task(
            agent_id,
            action="state",
            namespace="project",
            task_id="STATE-BLOCKED",
            state="blocked",
        )
        assert repeated_blocked["ok"] is True
        assert repeated_blocked["task"]["state"] == "blocked"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_task_events_preserve_checkpoint_and_result_history(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = (await register(service, "history-owner"))["self"]["agent_id"]
        await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="HISTORY-1",
            title="Preserve review task history",
            lane="review",
            candidate_ref="sha1",
        )
        claimed = await service.task(
            owner,
            action="claim",
            namespace="project",
            task_id="HISTORY-1",
            claim_intent="preserve owner-authored review history",
        )
        assert claimed["ok"] is True
        await service.task(
            owner,
            action="checkpoint",
            namespace="project",
            task_id="HISTORY-1",
            checkpoint={"step": "one"},
        )
        await service.task(
            owner,
            action="checkpoint",
            namespace="project",
            task_id="HISTORY-1",
            checkpoint={"step": "two"},
        )
        result = {"verdict": "approved", "evidence": {"tests": 2}}
        finished = await service.task(
            owner,
            action="done",
            namespace="project",
            task_id="HISTORY-1",
            result=result,
        )
        assert finished["ok"] is True

        detail = await service.tasks(namespace="project", task_id="HISTORY-1", show_details=True)
        assert all("agent_id" not in event for event in detail["task"]["events"])
        visible_names = {
            event.get("agent_name") for event in detail["task"]["events"] if event.get("agent_name")
        }
        assert visible_names <= {public_agent_name(owner)}
        checkpoints = [
            event["payload"]["checkpoint"]
            for event in reversed(detail["task"]["events"])
            if event["event_type"] == "checkpoint"
        ]
        assert checkpoints == [{"step": "one"}, {"step": "two"}]
        result_events = [
            event for event in detail["task"]["events"] if event["payload"].get("result") == result
        ]
        assert len(result_events) == 1
        assert detail["task"]["result"] == result
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_done_atomically_releases_all_current_claims(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = (await register(service, "done-owner"))["self"]["agent_id"]
        peer = (await register(service, "done-peer"))["self"]["agent_id"]
        await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="DONE-CLAIMS",
            title="Done releases current ownership",
            cooperative=True,
        )
        await service.task(
            owner,
            action="claim",
            namespace="project",
            task_id="DONE-CLAIMS",
            claim_intent="finishing completed task",
        )
        await service.task(
            peer,
            action="claim",
            namespace="project",
            task_id="DONE-CLAIMS",
            claim_intent="supporting completion validation",
        )

        done = await service.task(
            owner,
            action="done",
            namespace="project",
            task_id="DONE-CLAIMS",
            result={"summary": "completed and claims released"},
        )
        assert done["ok"] is True
        assert done["task"]["state"] == "done"
        assert done["task"]["active"] is False
        assert done["task"]["claims"] == []

        detail = await service.tasks(namespace="project", task_id="DONE-CLAIMS", show_details=True)
        assert detail["task"]["claims"] == []
        releases = [
            event
            for event in detail["task"]["events"]
            if event["event_type"] == "claim_released"
            and event["payload"].get("reason") == "task_done"
        ]
        assert len(releases) == 2
        assert {event["agent_name"] for event in releases} == {
            public_agent_name(owner),
            public_agent_name(peer),
        }
        assert await service.task_store.claims_for_agent(owner, active_only=True) == []
        assert await service.task_store.claims_for_agent(peer, active_only=True) == []
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_archive_hides_task_releases_claims_and_keeps_dependency_blocked(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = (await register(service, "archive-owner"))["self"]["agent_id"]
        await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="ARCHIVE-1",
            title="Mistaken task",
        )
        await service.task(
            owner,
            action="claim",
            namespace="project",
            task_id="ARCHIVE-1",
            claim_intent="checking whether task should be archived",
        )

        create_archived = await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="ARCHIVE-BYPASS",
            title="Cannot bypass archive note",
            state="archived",
        )
        assert create_archived["ok"] is False
        assert "state" in create_archived["error"]

        missing_note = await service.task(
            owner, action="archive", namespace="project", task_id="ARCHIVE-1"
        )
        assert missing_note["ok"] is False
        assert "archive_note" in missing_note["error"]

        direct_state = await service.task(
            owner,
            action="state",
            namespace="project",
            task_id="ARCHIVE-1",
            state="archived",
        )
        assert direct_state["ok"] is False
        assert "state" in direct_state["error"]

        archived = await service.task(
            owner,
            action="archive",
            namespace="project",
            task_id="ARCHIVE-1",
            archive_note="Created by mistake; superseded by ARCHIVE-2.",
        )
        assert archived["ok"] is True
        assert archived["task"]["state"] == "ready"
        assert archived["task"]["archived_at"]
        assert archived["task"]["archive_note"] == "Created by mistake; superseded by ARCHIVE-2."
        assert archived["task"]["active"] is False
        assert archived["task"]["claims"] == []

        default = await service.tasks(namespace="project")
        assert all(item["task_id"] != "ARCHIVE-1" for item in default["tasks"])
        visible = await service.tasks(namespace="project", show_archived=True)
        assert [item["task_id"] for item in visible["tasks"]] == ["ARCHIVE-1"]
        direct = await service.tasks(namespace="project", task_id="ARCHIVE-1", show_details=True)
        assert direct["task"]["state"] == "ready"
        assert direct["task"]["archived_at"]

        detail = await service.tasks(namespace="project", task_id="ARCHIVE-1", show_details=True)
        archive_events = [
            event for event in detail["task"]["events"] if event["event_type"] == "archived"
        ]
        assert len(archive_events) == 1
        assert (
            archive_events[0]["payload"]["archive_note"]
            == "Created by mistake; superseded by ARCHIVE-2."
        )
        releases = [
            event
            for event in detail["task"]["events"]
            if event["event_type"] == "claim_released"
            and event["payload"].get("reason") == "task_archived"
        ]
        assert len(releases) == 1
        assert releases[0]["agent_name"] == public_agent_name(owner)

        await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="project",
            task_id="ARCHIVE-2",
            title="Replacement task",
            dependencies=[{"namespace": "project", "task_id": "ARCHIVE-1"}],
        )
        claim = await service.task(
            owner,
            action="claim",
            namespace="project",
            task_id="ARCHIVE-2",
            claim_intent="waiting on archived prerequisite",
        )
        assert claim["ok"] is False
        assert claim["code"] == "dependency_open"
        assert claim["blocking_dependencies"] == [
            {
                "namespace": "project",
                "task_id": "ARCHIVE-1",
                "state": "ready",
                "archived": True,
                "satisfied": False,
            }
        ]

        stats = await service.task_coordinator.health()
        assert "archived" not in stats["by_state"]
        assert stats["by_state"]["ready"] == 2
        assert stats["by_lane"].get("general") == 1
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_task_list_batches_runtime_state_reads(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = (await register(service, "batched-list"))["self"]["agent_id"]
        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="batch",
            task_id="DEP",
            title="Dependency",
        )
        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="batch",
            task_id="CHILD",
            title="Child",
            dependencies=[{"namespace": "batch", "task_id": "DEP"}],
        )

        store = service.task_coordinator.store
        original_connect = store._connect
        connections = 0

        def counted_connect():
            nonlocal connections
            connections += 1
            return original_connect()

        store._connect = counted_connect
        listing = await service.tasks(
            namespace="batch",
            show_done=True,
            show_archived=True,
            limit=200,
        )

        assert listing["ok"] is True
        assert connections == 2
        cards = {item["task_id"]: item for item in listing["tasks"]}
        assert cards["CHILD"]["operational_status"] == "blocked"
        assert cards["CHILD"]["blocking_dependencies"] == [
            {
                "namespace": "batch",
                "task_id": "DEP",
                "created_at": cards["CHILD"]["blocking_dependencies"][0]["created_at"],
                "state": "ready",
                "archived": False,
                "satisfied": False,
            }
        ]
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_batched_task_list_preserves_logical_agent_claim_liveness(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = (await register(service, "persistent-claim-list"))["self"]["agent_id"]
        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="batch",
            task_id="PERSISTENT",
            title="Persistent owned task",
        )
        await service.task_coordinator.store.claim_owner(
            "batch",
            "PERSISTENT",
            ClaimOwner.logical_agent("logical-test"),
            claim_intent="persistent owner",
        )
        listing = await service.tasks(namespace="batch")
        assert listing["tasks"][0]["operational_status"] == "ready"
        transitioned = await service.task_coordinator.mutate(
            "logical-test",
            action="state",
            namespace="batch",
            task_id="PERSISTENT",
            state="in_progress",
            _claim_owner=ClaimOwner.logical_agent("logical-test"),
        )
        assert transitioned["ok"] is True
        assert transitioned["task"]["state"] == "in_progress"
        assert transitioned["task"]["operational_status"] == "in_progress"
        assert listing["tasks"][0]["owner"]["agent_name"] == "logical-test"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_claim_wip_one_per_owner_is_atomic_and_same_task_is_idempotent(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = (await register(service, "wip-owner"))["self"]["agent_id"]
        for task_id in ("ONE", "TWO"):
            created = await service.task(
                agent,
                action="create",
                namespace="wip",
                task_id=task_id,
                title=task_id,
                isolation_hint="none",
            )
            assert created["ok"] is True
        owner = ClaimOwner.logical_agent("logical-wip")
        first = await service.task_coordinator.mutate(
            "logical-wip",
            action="claim",
            namespace="wip",
            task_id="ONE",
            claim_intent="first",
            _claim_owner=owner,
        )
        assert first["ok"] is True
        same = await service.task_coordinator.mutate(
            "logical-wip",
            action="claim",
            namespace="wip",
            task_id="ONE",
            claim_intent="updated",
            _claim_owner=owner,
        )
        assert same["ok"] is True
        busy = await service.task_coordinator.mutate(
            "logical-wip",
            action="claim",
            namespace="wip",
            task_id="TWO",
            claim_intent="second",
            _claim_owner=owner,
        )
        assert busy["ok"] is False
        assert busy["code"] == "agent_busy"
        assert busy["warnings"][0]["context"]["current_task_id"] == "ONE"
    finally:
        await terminal.stop()
