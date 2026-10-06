import sqlite3
from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import public_agent_name, utc_now, utc_text
from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SqliteRepository
from terminal_mcp.storage.tasks import TaskStore
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime(tmp_path):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3")
    await repo.initialize()
    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1, queue_reconcile_sec=0.01)
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


async def detail(service, namespace, task_id):
    result = await service.tasks(namespace=namespace, task_id=task_id, show_details=True)
    assert result["ok"] is True
    return result["task"]


def event_with(task, event_type, **payload):
    matches = [event for event in task["events"] if event["event_type"] == event_type]
    for event in matches:
        if all(event["payload"].get(key) == value for key, value in payload.items()):
            return event
    return None


def owner_error(result):
    return "owner" in (str(result.get("code", "")) + " " + str(result.get("error", ""))).lower()


@pytest.mark.asyncio
async def test_claim_intent_owner_participants_owner_handoff_and_owner_only_mutations(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = await register(service, "owner")
        peer = await register(service, "peer")
        created = await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="COOP",
            title="cooperative ownership",
            cooperative=True,
        )
        assert created["ok"] is True

        missing_intent = await service.task(owner, action="claim", namespace="wf", task_id="COOP")
        assert missing_intent["ok"] is False
        assert "claim_intent" in missing_intent["error"]

        first = await service.task(
            owner,
            action="claim",
            namespace="wf",
            task_id="COOP",
            claim_intent="implementing parser changes",
        )
        assert first["ok"] is True
        second = await service.task(
            peer,
            action="claim",
            namespace="wf",
            task_id="COOP",
            claim_intent="running regression suite",
        )
        assert second["ok"] is True

        task = await detail(service, "wf", "COOP")
        assert task["owner"]["agent_name"] == public_agent_name(owner)
        assert [item["agent_name"] for item in task["participants"]] == [public_agent_name(peer)]
        claims = {item["agent_name"]: item for item in task["claims"]}
        assert claims[public_agent_name(owner)]["role"] == "owner"
        assert claims[public_agent_name(peer)]["role"] == "participant"
        assert claims[public_agent_name(owner)]["claim_intent"] == "implementing parser changes"
        assert claims[public_agent_name(peer)]["claim_intent"] == "running regression suite"
        assert claims[public_agent_name(owner)]["claim_age_seconds"] >= 0
        assert claims[public_agent_name(peer)]["claim_age_seconds"] >= 0
        original_claimed_at = claims[public_agent_name(owner)]["claimed_at"]

        refreshed = await service.task(
            owner,
            action="claim",
            namespace="wf",
            task_id="COOP",
            claim_intent="preparing follow-up fix",
        )
        assert refreshed["ok"] is True
        refreshed_task = await detail(service, "wf", "COOP")
        refreshed_owner = next(
            item
            for item in refreshed_task["claims"]
            if item["agent_name"] == public_agent_name(owner)
        )
        assert refreshed_owner["claimed_at"] == original_claimed_at
        assert refreshed_owner["claim_intent"] == "preparing follow-up fix"
        assert len(refreshed_task["claims"]) == 2

        metadata = await service.task(
            peer,
            action="update",
            namespace="wf",
            task_id="COOP",
            description="participant observation",
            tags=["shared", "runtime"],
        )
        assert metadata["ok"] is True

        denied_checkpoint = await service.task(
            peer,
            action="checkpoint",
            namespace="wf",
            task_id="COOP",
            checkpoint={"step": "participant must not own workflow"},
        )
        assert denied_checkpoint["ok"] is False
        assert owner_error(denied_checkpoint)

        denied_state = await service.task(
            peer,
            action="state",
            namespace="wf",
            task_id="COOP",
            state="blocked",
            blocker_reason="participant cannot block task",
        )
        assert denied_state["ok"] is False
        assert owner_error(denied_state)

        denied_done = await service.task(
            peer,
            action="done",
            namespace="wf",
            task_id="COOP",
            result="participant cannot complete task",
        )
        assert denied_done["ok"] is False
        assert owner_error(denied_done)

        denied_dependencies = await service.task(
            peer,
            action="update",
            namespace="wf",
            task_id="COOP",
            dependencies=[{"namespace": "wf", "task_id": "MISSING"}],
        )
        assert denied_dependencies["ok"] is False
        assert owner_error(denied_dependencies)

        collapse = await service.task(
            owner,
            action="update",
            namespace="wf",
            task_id="COOP",
            cooperative=False,
        )
        assert collapse["ok"] is False
        assert "claim" in collapse["error"].lower()

        participant_comment = await service.task(
            peer,
            action="comment",
            namespace="wf",
            task_id="COOP",
            comment_text="Participant found a reproducible edge case.",
        )
        assert participant_comment["ok"] is True

        missing_release_reason = await service.task(
            owner, action="release", namespace="wf", task_id="COOP"
        )
        assert missing_release_reason["ok"] is False
        assert "release_reason" in missing_release_reason["error"]

        released = await service.task(
            owner,
            action="release",
            namespace="wf",
            task_id="COOP",
            release_reason="Implementation slice complete; peer continues validation.",
        )
        assert released["ok"] is True
        after = await detail(service, "wf", "COOP")
        assert after["owner"]["agent_name"] == public_agent_name(peer)
        assert after["participants"] == []
        release_event = event_with(after, "claim_released")
        assert release_event is not None
        assert release_event["payload"]["release_reason"] == (
            "Implementation slice complete; peer continues validation."
        )

        new_owner_checkpoint = await service.task(
            peer,
            action="checkpoint",
            namespace="wf",
            task_id="COOP",
            checkpoint={"step": "now owner"},
        )
        assert new_owner_checkpoint["ok"] is True
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_participant_cannot_mutate_owner_only_identity_fields(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = await register(service, "identity owner")
        peer = await register(service, "identity participant")
        created = await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="IDENTITY",
            title="identity gates",
            cooperative=True,
            lane="implementation",
            priority="P2",
            resource_context={"scope": "original"},
            candidate_ref="candidate-1",
        )
        assert created["ok"] is True
        assert (
            await service.task(
                owner,
                action="claim",
                namespace="wf",
                task_id="IDENTITY",
                claim_intent="owning identity fields",
            )
        )["ok"] is True
        assert (
            await service.task(
                peer,
                action="claim",
                namespace="wf",
                task_id="IDENTITY",
                claim_intent="editing safe metadata only",
            )
        )["ok"] is True

        denied = await service.task(
            peer,
            action="update",
            namespace="wf",
            task_id="IDENTITY",
            lane="review",
            priority="P0",
            resource_context={"scope": "participant"},
            candidate_ref="candidate-peer",
        )
        assert denied["ok"] is False
        assert denied["code"] == "owner_required"

        task = await detail(service, "wf", "IDENTITY")
        assert task["lane"] == "implementation"
        assert task["priority"] == "P2"
        assert task["resource_context"] == {"scope": "original"}
        assert task["candidate_ref"] == "candidate-1"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_candidate_ref_is_owner_mutable_before_review_then_frozen(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        implementer = await register(service, "candidate owner")
        reviewer = await register(service, "candidate reviewer")
        assert (
            await service.task(
                implementer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="IMPL-FREEZE",
                title="implementation candidate",
                candidate_ref="candidate-1",
            )
        )["ok"] is True
        assert (
            await service.task(
                implementer,
                action="claim",
                namespace="wf",
                task_id="IMPL-FREEZE",
                claim_intent="preparing candidate",
            )
        )["ok"] is True

        mutable = await service.task(
            implementer,
            action="update",
            namespace="wf",
            task_id="IMPL-FREEZE",
            candidate_ref="candidate-2",
        )
        assert mutable["ok"] is True
        assert mutable["task"]["candidate_ref"] == "candidate-2"
        mutable_detail = await detail(service, "wf", "IMPL-FREEZE")
        assert event_with(mutable_detail, "updated", candidate_ref="candidate-2") is not None

        assert (
            await service.task(
                reviewer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="REVIEW-FREEZE",
                title="review candidate",
                lane="review",
                candidate_ref="candidate-2",
            )
        )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="claim",
                namespace="wf",
                task_id="REVIEW-FREEZE",
                claim_intent="reviewing candidate-2",
            )
        )["ok"] is True
        linked = await service.task(
            reviewer,
            action="relate",
            namespace="wf",
            task_id="REVIEW-FREEZE",
            relation_kind="review_of",
            related_namespace="wf",
            related_task_id="IMPL-FREEZE",
        )
        assert linked["ok"] is True

        frozen = await service.task(
            implementer,
            action="update",
            namespace="wf",
            task_id="IMPL-FREEZE",
            candidate_ref="candidate-3",
        )
        assert frozen["ok"] is False
        assert frozen["code"] == "candidate_ref_frozen"

        task = await detail(service, "wf", "IMPL-FREEZE")
        assert task["candidate_ref"] == "candidate-2"
        assert event_with(task, "updated", candidate_ref="candidate-3") is None
        assert any(
            relation["direction"] == "incoming"
            and relation["kind"] == "review_of"
            and relation["task_id"] == "REVIEW-FREEZE"
            for relation in task["relations"]
        )
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_review_of_atomically_binds_parent_candidate(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        implementer = await register(service, "bind implementer")
        reviewer = await register(service, "bind reviewer")
        assert (
            await service.task(
                implementer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="BIND-IMPL",
                title="bind parent",
                candidate_ref="candidate-bind",
            )
        )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="BIND-REVIEW",
                title="bind review",
                lane="review",
            )
        )["ok"] is True

        linked = await service.task(
            reviewer,
            action="relate",
            namespace="wf",
            task_id="BIND-REVIEW",
            relation_kind="review_of",
            related_namespace="wf",
            related_task_id="BIND-IMPL",
        )
        assert linked["ok"] is True
        review = await detail(service, "wf", "BIND-REVIEW")
        assert review["candidate_ref"] == "candidate-bind"
        bound = event_with(review, "candidate_bound", candidate_ref="candidate-bind")
        assert bound is not None
        assert bound["payload"]["parent_namespace"] == "wf"
        assert bound["payload"]["parent_task_id"] == "BIND-IMPL"
        relation = event_with(review, "relation_added", candidate_ref="candidate-bind")
        assert relation is not None
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_review_of_rejects_missing_parent_candidate_without_partial_state(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        implementer = await register(service, "missing candidate implementer")
        reviewer = await register(service, "missing candidate reviewer")
        assert (
            await service.task(
                implementer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="NO-CANDIDATE",
                title="parent without candidate",
            )
        )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="NO-CANDIDATE-REVIEW",
                title="review without candidate",
                lane="review",
            )
        )["ok"] is True

        rejected = await service.task(
            reviewer,
            action="relate",
            namespace="wf",
            task_id="NO-CANDIDATE-REVIEW",
            relation_kind="review_of",
            related_namespace="wf",
            related_task_id="NO-CANDIDATE",
        )
        assert rejected["ok"] is False
        assert rejected["code"] == "missing_parent_candidate"
        review = await detail(service, "wf", "NO-CANDIDATE-REVIEW")
        assert review["candidate_ref"] is None
        assert not any(relation["kind"] == "review_of" for relation in review["relations"])
        assert event_with(review, "candidate_bound") is None
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_review_of_rejects_conflicting_preset_candidate_without_partial_state(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        implementer = await register(service, "conflict implementer")
        reviewer = await register(service, "conflict reviewer")
        assert (
            await service.task(
                implementer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="CONFLICT-IMPL",
                title="conflict parent",
                candidate_ref="candidate-parent",
            )
        )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="CONFLICT-REVIEW",
                title="conflict review",
                lane="review",
                candidate_ref="candidate-other",
            )
        )["ok"] is True

        rejected = await service.task(
            reviewer,
            action="relate",
            namespace="wf",
            task_id="CONFLICT-REVIEW",
            relation_kind="review_of",
            related_namespace="wf",
            related_task_id="CONFLICT-IMPL",
        )
        assert rejected["ok"] is False
        assert rejected["code"] == "candidate_conflict"
        review = await detail(service, "wf", "CONFLICT-REVIEW")
        assert review["candidate_ref"] == "candidate-other"
        assert not any(relation["kind"] == "review_of" for relation in review["relations"])
        assert event_with(review, "candidate_bound") is None
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_review_of_rejects_second_parent_without_partial_state(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        implementer = await register(service, "ambiguous implementer")
        reviewer = await register(service, "ambiguous reviewer")
        for task_id in ("PARENT-A", "PARENT-B"):
            assert (
                await service.task(
                    implementer,
                    action="create",
                    isolation_hint="none",
                    namespace="wf",
                    task_id=task_id,
                    title=task_id,
                    candidate_ref="candidate-shared",
                )
            )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="AMBIGUOUS-REVIEW",
                title="ambiguous review",
                lane="review",
            )
        )["ok"] is True

        first = await service.task(
            reviewer,
            action="relate",
            namespace="wf",
            task_id="AMBIGUOUS-REVIEW",
            relation_kind="review_of",
            related_namespace="wf",
            related_task_id="PARENT-A",
        )
        assert first["ok"] is True
        second = await service.task(
            reviewer,
            action="relate",
            namespace="wf",
            task_id="AMBIGUOUS-REVIEW",
            relation_kind="review_of",
            related_namespace="wf",
            related_task_id="PARENT-B",
        )
        assert second["ok"] is False
        assert second["code"] == "ambiguous_review_parent"

        review = await detail(service, "wf", "AMBIGUOUS-REVIEW")
        assert review["candidate_ref"] == "candidate-shared"
        parents = [
            relation["task_id"]
            for relation in review["relations"]
            if relation["direction"] == "outgoing" and relation["kind"] == "review_of"
        ]
        assert parents == ["PARENT-A"]
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_candidate_ref_is_frozen_after_done(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = await register(service, "completed candidate owner")
        assert (
            await service.task(
                owner,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="DONE-CANDIDATE",
                title="completed candidate",
                candidate_ref="candidate-final",
            )
        )["ok"] is True
        assert (
            await service.task(
                owner,
                action="claim",
                namespace="wf",
                task_id="DONE-CANDIDATE",
                claim_intent="finishing candidate",
            )
        )["ok"] is True
        completed = await service.task(
            owner,
            action="done",
            namespace="wf",
            task_id="DONE-CANDIDATE",
            result={"candidate": "candidate-final"},
        )
        assert completed["ok"] is True

        frozen = await service.task(
            owner,
            action="update",
            namespace="wf",
            task_id="DONE-CANDIDATE",
            candidate_ref="candidate-after-done",
        )
        assert frozen["ok"] is False
        assert frozen["code"] == "candidate_ref_frozen"

        task = await detail(service, "wf", "DONE-CANDIDATE")
        assert task["candidate_ref"] == "candidate-final"
        assert event_with(task, "updated", candidate_ref="candidate-after-done") is None
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_claimed_blocked_and_done_require_context_and_leave_history(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        owner = await register(service, "workflow owner")
        await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="STATEFUL",
            title="stateful workflow",
        )
        claimed = await service.task(
            owner,
            action="claim",
            namespace="wf",
            task_id="STATEFUL",
            claim_intent="investigating failing acceptance check",
        )
        assert claimed["ok"] is True

        no_reason = await service.task(
            owner,
            action="state",
            namespace="wf",
            task_id="STATEFUL",
            state="blocked",
        )
        assert no_reason["ok"] is False
        assert "blocker_reason" in no_reason["error"]

        blocked = await service.task(
            owner,
            action="state",
            namespace="wf",
            task_id="STATEFUL",
            state="blocked",
            blocker_reason="Upstream fixture still returns an invalid schema.",
        )
        assert blocked["ok"] is True
        assert blocked["task"]["state"] == "blocked"
        blocked_detail = await detail(service, "wf", "STATEFUL")
        blocker_events = [
            event
            for event in blocked_detail["events"]
            if event["payload"].get("blocker_reason")
            == "Upstream fixture still returns an invalid schema."
        ]
        assert blocker_events
        assert blocker_events[0]["agent_name"] == public_agent_name(owner)

        ready = await service.task(
            owner,
            action="state",
            namespace="wf",
            task_id="STATEFUL",
            state="ready",
        )
        assert ready["ok"] is True

        missing_result = await service.task(
            owner, action="done", namespace="wf", task_id="STATEFUL"
        )
        assert missing_result["ok"] is False
        assert "result" in missing_result["error"]

        completed = await service.task(
            owner,
            action="done",
            namespace="wf",
            task_id="STATEFUL",
            result={"summary": "acceptance criteria satisfied", "tests": 12},
        )
        assert completed["ok"] is True
        assert completed["task"]["state"] == "done"
        assert completed["task"]["result"]["tests"] == 12
        assert completed["task"]["claims"] == []
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_comments_and_relations_are_append_only_durable_history(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        author = await register(service, "comment author")
        await service.task(
            author,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="SOURCE",
            title="source task",
            description="Current description stays separate from history.",
        )
        await service.task(
            author,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="TARGET",
            title="target task",
        )
        claim = await service.task(
            author,
            action="claim",
            namespace="wf",
            task_id="SOURCE",
            claim_intent="recording durable findings",
        )
        assert claim["ok"] is True

        comment = await service.task(
            author,
            action="comment",
            namespace="wf",
            task_id="SOURCE",
            comment_text="Finding: replay differs at sequence 42.",
        )
        assert comment["ok"] is True
        related = await service.task(
            author,
            action="relate",
            namespace="wf",
            task_id="SOURCE",
            relation_kind="supports",
            related_namespace="wf",
            related_task_id="TARGET",
        )
        assert related["ok"] is True

        source = await detail(service, "wf", "SOURCE")
        assert source["description"] == "Current description stays separate from history."
        comment_event = event_with(source, "comment")
        assert comment_event is not None
        assert comment_event["payload"]["text"] == "Finding: replay differs at sequence 42."
        assert comment_event["agent_name"] == public_agent_name(author)
        outgoing = [item for item in source["relations"] if item["direction"] == "outgoing"]
        assert any(
            item["kind"] == "supports"
            and item["namespace"] == "wf"
            and item["task_id"] == "TARGET"
            and item["created_at"]
            for item in outgoing
        )

        target = await detail(service, "wf", "TARGET")
        incoming = [item for item in target["relations"] if item["direction"] == "incoming"]
        assert any(
            item["kind"] == "supports" and item["namespace"] == "wf" and item["task_id"] == "SOURCE"
            for item in incoming
        )
        released = await service.task(
            author,
            action="release",
            namespace="wf",
            task_id="SOURCE",
            release_reason="Findings recorded; preserving context for next owner.",
        )
        assert released["ok"] is True
        archived = await service.task(
            author,
            action="archive",
            namespace="wf",
            task_id="SOURCE",
            archive_note="Historical investigation retained for audit.",
        )
        assert archived["ok"] is True
        finished = await service.agent_finish(author)
        assert finished["ok"] is True
    finally:
        await terminal.stop()

    reopened = SqliteRepository(repo.path, tmp_path / "output-reopened.sqlite3")
    await reopened.initialize()
    reopened_terminal = LinuxTerminalAdapter(reopened, "/bin/bash", tmp_path, 0.1)
    reopened_service = TerminalService(reopened, reopened_terminal, 5000)
    try:
        source = await detail(reopened_service, "wf", "SOURCE")
        assert source["archived_at"]
        assert source["archive_note"] == "Historical investigation retained for audit."
        assert event_with(source, "comment") is not None
        assert any(
            item["kind"] == "supports"
            and item["direction"] == "outgoing"
            and item["task_id"] == "TARGET"
            for item in source["relations"]
        )
    finally:
        await reopened_terminal.stop()


@pytest.mark.asyncio
async def test_review_completion_rejects_candidate_mismatch_and_keeps_release_blocked(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        implementer = await register(service, "stale implementer")
        reviewer = await register(service, "stale reviewer")
        releaser = await register(service, "stale releaser")
        assert (
            await service.task(
                implementer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="STALE-IMPL",
                title="stale parent",
                candidate_ref="candidate-1",
            )
        )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="STALE-REVIEW",
                title="stale review",
                lane="review",
            )
        )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="relate",
                namespace="wf",
                task_id="STALE-REVIEW",
                relation_kind="review_of",
                related_namespace="wf",
                related_task_id="STALE-IMPL",
            )
        )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="claim",
                namespace="wf",
                task_id="STALE-REVIEW",
                claim_intent="reviewing candidate-1",
            )
        )["ok"] is True
        assert (
            await service.task(
                releaser,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="STALE-RELEASE",
                title="release waits for review",
                lane="release",
                dependencies=[{"namespace": "wf", "task_id": "STALE-REVIEW"}],
            )
        )["ok"] is True

        # Simulate a legacy/concurrent stale review state that bypassed WF-001's domain freeze.
        await TaskStore(repo.path).update_task("wf", "STALE-IMPL", candidate_ref="candidate-2")

        blocked = await service.task(
            reviewer,
            action="state",
            namespace="wf",
            task_id="STALE-REVIEW",
            state="blocked",
            blocker_reason="Candidate changed while this review was in flight.",
        )
        assert blocked["ok"] is True
        parent = await detail(service, "wf", "STALE-IMPL")
        blocked_feedback = event_with(parent, "review_feedback", outcome="blocked")
        assert blocked_feedback is not None
        assert blocked_feedback["payload"]["candidate_ref"] == "candidate-1"

        assert (
            await service.task(
                reviewer,
                action="state",
                namespace="wf",
                task_id="STALE-REVIEW",
                state="ready",
            )
        )["ok"] is True
        rejected = await service.task(
            reviewer,
            action="done",
            namespace="wf",
            task_id="STALE-REVIEW",
            result={"verdict": "approved"},
        )
        assert rejected["ok"] is False
        assert rejected["code"] == "candidate_mismatch"

        review = await detail(service, "wf", "STALE-REVIEW")
        assert review["state"] == "ready"
        parent = await detail(service, "wf", "STALE-IMPL")
        assert event_with(parent, "review_feedback", outcome="done") is None
        release = await detail(service, "wf", "STALE-RELEASE")
        assert release["operational_status"] == "blocked"
        assert release["blocking_dependencies"][0]["task_id"] == "STALE-REVIEW"
        assert release["blocking_dependencies"][0]["state"] == "ready"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_review_completion_rejects_missing_candidate_on_either_side(tmp_path):
    for missing_side in ("review", "parent"):
        repo, terminal, service = await runtime(tmp_path / missing_side)
        try:
            implementer = await register(service, f"missing {missing_side} implementer")
            reviewer = await register(service, f"missing {missing_side} reviewer")
            assert (
                await service.task(
                    implementer,
                    action="create",
                    isolation_hint="none",
                    namespace="wf",
                    task_id="MISSING-IMPL",
                    title="missing candidate parent",
                    candidate_ref="candidate-1",
                )
            )["ok"] is True
            assert (
                await service.task(
                    reviewer,
                    action="create",
                    isolation_hint="none",
                    namespace="wf",
                    task_id="MISSING-REVIEW",
                    title="missing candidate review",
                    lane="review",
                )
            )["ok"] is True
            assert (
                await service.task(
                    reviewer,
                    action="relate",
                    namespace="wf",
                    task_id="MISSING-REVIEW",
                    relation_kind="review_of",
                    related_namespace="wf",
                    related_task_id="MISSING-IMPL",
                )
            )["ok"] is True
            assert (
                await service.task(
                    reviewer,
                    action="claim",
                    namespace="wf",
                    task_id="MISSING-REVIEW",
                    claim_intent="checking missing candidate gate",
                )
            )["ok"] is True

            if missing_side == "review":
                await TaskStore(repo.path).update_task("wf", "MISSING-REVIEW", candidate_ref=None)
            else:
                await TaskStore(repo.path).update_task("wf", "MISSING-IMPL", candidate_ref=None)

            rejected = await service.task(
                reviewer,
                action="done",
                namespace="wf",
                task_id="MISSING-REVIEW",
                result={"verdict": "approved"},
            )
            assert rejected["ok"] is False
            assert rejected["code"] == "candidate_mismatch"
            review = await detail(service, "wf", "MISSING-REVIEW")
            assert review["state"] == "ready"
            parent = await detail(service, "wf", "MISSING-IMPL")
            assert event_with(parent, "review_feedback", outcome="done") is None
        finally:
            await terminal.stop()


@pytest.mark.asyncio
async def test_review_completion_checks_candidate_proposed_in_same_done_mutation(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        implementer = await register(service, "proposed candidate implementer")
        reviewer = await register(service, "proposed candidate reviewer")
        assert (
            await service.task(
                implementer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="PROPOSED-IMPL",
                title="proposed candidate parent",
                candidate_ref="candidate-1",
            )
        )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id="PROPOSED-REVIEW",
                title="proposed candidate review",
                lane="review",
            )
        )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="relate",
                namespace="wf",
                task_id="PROPOSED-REVIEW",
                relation_kind="review_of",
                related_namespace="wf",
                related_task_id="PROPOSED-IMPL",
            )
        )["ok"] is True
        assert (
            await service.task(
                reviewer,
                action="claim",
                namespace="wf",
                task_id="PROPOSED-REVIEW",
                claim_intent="checking same-call candidate mutation",
            )
        )["ok"] is True

        rejected = await service.task(
            reviewer,
            action="done",
            namespace="wf",
            task_id="PROPOSED-REVIEW",
            candidate_ref="candidate-other",
            result={"verdict": "approved"},
        )
        assert rejected["ok"] is False
        assert rejected["code"] == "candidate_mismatch"
        review = await detail(service, "wf", "PROPOSED-REVIEW")
        assert review["state"] == "ready"
        assert review["candidate_ref"] == "candidate-1"
        parent = await detail(service, "wf", "PROPOSED-IMPL")
        assert event_with(parent, "review_feedback", outcome="done") is None
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_review_relation_propagates_blocking_and_success_feedback(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        implementer = await register(service, "implementer")
        reviewer = await register(service, "reviewer")
        await service.task(
            implementer,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="IMPL",
            title="implementation",
            candidate_ref="candidate-1",
        )
        await service.task(
            reviewer,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="REVIEW",
            title="review implementation",
            lane="review",
            candidate_ref="candidate-1",
        )
        link = await service.task(
            reviewer,
            action="relate",
            namespace="wf",
            task_id="REVIEW",
            relation_kind="review_of",
            related_namespace="wf",
            related_task_id="IMPL",
        )
        assert link["ok"] is True
        claim = await service.task(
            reviewer,
            action="claim",
            namespace="wf",
            task_id="REVIEW",
            claim_intent="checking candidate-1 contracts",
        )
        assert claim["ok"] is True

        finding = await service.task(
            reviewer,
            action="comment",
            namespace="wf",
            task_id="REVIEW",
            comment_text="Blocking finding: replay provenance is incomplete.",
        )
        assert finding["ok"] is True
        blocked = await service.task(
            reviewer,
            action="state",
            namespace="wf",
            task_id="REVIEW",
            state="blocked",
            blocker_reason="Replay provenance must be fixed before approval.",
        )
        assert blocked["ok"] is True

        impl = await detail(service, "wf", "IMPL")
        feedback = event_with(impl, "review_feedback", outcome="blocked")
        assert feedback is not None
        assert feedback["agent_name"] == public_agent_name(reviewer)
        assert feedback["payload"]["review_namespace"] == "wf"
        assert feedback["payload"]["review_task_id"] == "REVIEW"
        assert feedback["payload"]["candidate_ref"] == "candidate-1"
        assert feedback["payload"]["blocker_reason"] == (
            "Replay provenance must be fixed before approval."
        )
        assert feedback["created_at"]

        ready = await service.task(
            reviewer,
            action="state",
            namespace="wf",
            task_id="REVIEW",
            state="ready",
        )
        assert ready["ok"] is True
        done = await service.task(
            reviewer,
            action="done",
            namespace="wf",
            task_id="REVIEW",
            result={"verdict": "approved", "candidate": "candidate-1"},
        )
        assert done["ok"] is True
        assert done["task"]["lane"] == "review"

        impl = await detail(service, "wf", "IMPL")
        feedback = event_with(impl, "review_feedback", outcome="done")
        assert feedback is not None
        assert feedback["payload"]["candidate_ref"] == "candidate-1"
        assert feedback["payload"]["result"] == {
            "verdict": "approved",
            "candidate": "candidate-1",
        }
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_generic_relation_can_be_removed_without_rewriting_task_history(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = await register(service, "relation owner")
        for task_id in ("A", "B"):
            await service.task(
                agent,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id=task_id,
                title=task_id,
            )
        assert (
            await service.task(
                agent,
                action="relate",
                namespace="wf",
                task_id="A",
                relation_kind="follows_up",
                related_namespace="wf",
                related_task_id="B",
            )
        )["ok"]
        removed = await service.task(
            agent,
            action="unrelate",
            namespace="wf",
            task_id="A",
            relation_kind="follows_up",
            related_namespace="wf",
            related_task_id="B",
        )
        assert removed["ok"] is True
        a = await detail(service, "wf", "A")
        assert not any(item["kind"] == "follows_up" for item in a["relations"])
        assert event_with(a, "relation_added") is not None
        assert event_with(a, "relation_removed") is not None
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_dependency_integrity_cycles_missing_and_claimability_observability(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = await register(service, "dependency graph")
        self_dep = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="SELF",
            title="self dependency",
            dependencies=[{"namespace": "wf", "task_id": "SELF"}],
        )
        assert self_dep["ok"] is False
        assert "self" in self_dep["error"].lower()
        assert (await service.tasks(namespace="wf", task_id="SELF"))["ok"] is False

        for task_id in ("A", "B"):
            await service.task(
                agent,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id=task_id,
                title=task_id,
            )
        claimed_a = await service.task(
            agent, action="claim", namespace="wf", task_id="A", claim_intent="edit dependency graph"
        )
        assert claimed_a["ok"] is True
        a_depends_b = await service.task(
            agent,
            action="update",
            namespace="wf",
            task_id="A",
            dependencies=[{"namespace": "wf", "task_id": "B"}],
        )
        assert a_depends_b["ok"] is True
        await service.task(
            agent,
            action="release",
            namespace="wf",
            task_id="A",
            release_reason="dependency edge recorded",
        )
        claimed_b = await service.task(
            agent, action="claim", namespace="wf", task_id="B", claim_intent="test cycle rejection"
        )
        assert claimed_b["ok"] is True
        cycle = await service.task(
            agent,
            action="update",
            namespace="wf",
            task_id="B",
            dependencies=[{"namespace": "wf", "task_id": "A"}],
        )
        assert cycle["ok"] is False
        assert "cycle" in cycle["error"].lower()
        await service.task(
            agent,
            action="release",
            namespace="wf",
            task_id="B",
            release_reason="cycle rejection verified",
        )
        assert (await detail(service, "wf", "B"))["dependencies"] == []

        missing = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="MISSING-GATED",
            title="waits for future task",
            dependencies=[{"namespace": "wf", "task_id": "FUTURE"}],
        )
        assert missing["ok"] is True
        missing_detail = await detail(service, "wf", "MISSING-GATED")
        assert len(missing_detail["dependencies"]) == 1
        missing_dep = missing_detail["dependencies"][0]
        assert missing_dep["namespace"] == "wf"
        assert missing_dep["task_id"] == "FUTURE"
        assert missing_dep["state"] == "missing"
        assert missing_dep["satisfied"] is False
        denied = await service.task(
            agent,
            action="claim",
            namespace="wf",
            task_id="MISSING-GATED",
            claim_intent="waiting for future prerequisite",
        )
        assert denied["ok"] is False
        assert denied["code"] == "dependency_open"
        assert denied["blocking_dependencies"][0]["state"] == "missing"

        listing = await service.tasks(namespace="wf")
        assert listing["summary"]["missing_dependency_count"] == 1
        assert listing["summary"]["claimable_count"] == 1
        b = await detail(service, "wf", "B")
        assert listing["summary"]["oldest_claimable_ready_since"] == b["ready_since"]
        assert listing["recommended"]["task_id"] == "B"

        forward = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="FORWARD",
            title="forward dependency on not-yet-created task",
            dependencies=[{"namespace": "wf", "task_id": "FUTURE-CYCLE"}],
        )
        assert forward["ok"] is True
        materialized_cycle = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="FUTURE-CYCLE",
            title="would close dangling cycle",
            dependencies=[{"namespace": "wf", "task_id": "FORWARD"}],
        )
        assert materialized_cycle["ok"] is False
        assert "cycle" in materialized_cycle["error"].lower()
        assert (await service.tasks(namespace="wf", task_id="FUTURE-CYCLE"))["ok"] is False
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_archive_is_lifecycle_dimension_and_preserves_workflow_state(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = await register(service, "archive lifecycle")
        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="READY-ARCH",
            title="archive ready task",
        )
        await service.task(
            agent,
            action="claim",
            namespace="wf",
            task_id="READY-ARCH",
            claim_intent="deciding whether task is obsolete",
        )
        archived_ready = await service.task(
            agent,
            action="archive",
            namespace="wf",
            task_id="READY-ARCH",
            archive_note="Superseded by a newer task.",
        )
        assert archived_ready["ok"] is True
        assert archived_ready["task"]["state"] == "ready"
        assert archived_ready["task"]["archived_at"]
        assert archived_ready["task"]["archive_note"] == "Superseded by a newer task."
        assert archived_ready["task"]["claims"] == []
        assert archived_ready["task"]["owner"] is None

        active = await service.tasks(namespace="wf")
        assert all(item["task_id"] != "READY-ARCH" for item in active["tasks"])
        archived = await service.tasks(namespace="wf", show_archived=True)
        assert any(item["task_id"] == "READY-ARCH" for item in archived["tasks"])
        direct = await detail(service, "wf", "READY-ARCH")
        assert direct["state"] == "ready"
        assert direct["archived_at"]

        done = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="DONE-ARCH",
            title="completed then archived",
            state="done",
            result="Goal achieved before archive.",
        )
        assert done["ok"] is True
        done_archive = await service.task(
            agent,
            action="archive",
            namespace="wf",
            task_id="DONE-ARCH",
            archive_note="Historical completed task.",
        )
        assert done_archive["ok"] is True
        assert done_archive["task"]["state"] == "done"
        assert done_archive["task"]["result"] == "Goal achieved before archive."

        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="NEEDS-DONE",
            title="depends on archived completion",
            dependencies=[{"namespace": "wf", "task_id": "DONE-ARCH"}],
        )
        satisfied = await service.task(
            agent,
            action="claim",
            namespace="wf",
            task_id="NEEDS-DONE",
            claim_intent="dependency was completed successfully",
        )
        assert satisfied["ok"] is True

        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="UNFINISHED-ARCH",
            title="unfinished archive",
        )
        await service.task(
            agent,
            action="archive",
            namespace="wf",
            task_id="UNFINISHED-ARCH",
            archive_note="Cancelled before goal was achieved.",
        )
        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="NEEDS-UNFINISHED",
            title="blocked by unfinished archive",
            dependencies=[{"namespace": "wf", "task_id": "UNFINISHED-ARCH"}],
        )
        blocked = await service.task(
            agent,
            action="claim",
            namespace="wf",
            task_id="NEEDS-UNFINISHED",
            claim_intent="checking archived prerequisite",
        )
        assert blocked["ok"] is False
        assert blocked["code"] == "dependency_open"
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_agent_observation_enforces_single_wip_and_finish_releases_claim(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = await register(service, "single WIP agent")
        for task_id in ("ONE", "TWO"):
            await service.task(
                agent,
                action="create",
                isolation_hint="none",
                namespace="wf",
                task_id=task_id,
                title=task_id,
            )

        first = await service.task(
            agent,
            action="claim",
            namespace="wf",
            task_id="ONE",
            claim_intent="implementing first slice",
        )
        assert first["ok"] is True
        second = await service.task(
            agent,
            action="claim",
            namespace="wf",
            task_id="TWO",
            claim_intent="must wait for first slice release",
        )
        assert second["ok"] is False
        assert second["code"] == "agent_busy"

        first_task = await detail(service, "wf", "ONE")
        assert first_task["owner"]["agent_name"] == public_agent_name(agent)
        assert first_task["participants"] == []

        fleet = await service.agents(agent_id=agent)
        refs = {item["task_id"]: item for item in fleet["self"]["managed_tasks"]}
        assert set(refs) == {"ONE"}
        assert refs["ONE"]["claim_intent"] == "implementing first slice"
        assert refs["ONE"]["role"] == "owner"
        assert refs["ONE"]["claimed_at"]
        assert refs["ONE"]["claim_age_seconds"] >= 0
        assert set(fleet["task_scope_options"]) == {"none", "all", "wf/ONE"}

        finished = await service.agent_finish(agent)
        assert finished["ok"] is True
        for task_id in ("ONE", "TWO"):
            task = await detail(service, "wf", task_id)
            assert task["claims"] == []
            assert task["owner"] is None
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_expired_agent_session_releases_live_claim(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        agent = await register(service, "expiring owner")
        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="EXPIRE",
            title="claim follows session lifetime",
        )
        claimed = await service.task(
            agent,
            action="claim",
            namespace="wf",
            task_id="EXPIRE",
            claim_intent="active until session expires",
        )
        assert claimed["ok"] is True

        old = utc_text(utc_now() - timedelta(seconds=301))
        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET last_activity_at=? WHERE agent_id=?",
                (old, agent),
            )
            db.commit()

        expired = await service.task(
            agent,
            action="comment",
            namespace="wf",
            task_id="EXPIRE",
            comment_text="must not be accepted after expiry",
        )
        assert expired["ok"] is False
        assert expired["registration_required"] is True
        task = await detail(service, "wf", "EXPIRE")
        assert task["claims"] == []
        assert task["owner"] is None
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_v8_archived_rows_migrate_to_separate_archive_lifecycle(tmp_path):
    database = tmp_path / "legacy-v8-archive.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE work_items(
                namespace TEXT NOT NULL, task_id TEXT NOT NULL, title TEXT NOT NULL,
                lane TEXT NOT NULL CHECK(lane IN (
                    'implementation','review','release','integration','general'
                )),
                priority INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL CHECK(state IN (
                    'ready','blocked','deferred','done','archived'
                )),
                description TEXT NOT NULL DEFAULT '', next_action TEXT NOT NULL DEFAULT '',
                resource_json TEXT NOT NULL DEFAULT '{}', reviews_json TEXT NOT NULL DEFAULT '[]',
                cooperative INTEGER NOT NULL DEFAULT 0 CHECK(cooperative IN (0,1)),
                checkpoint_json TEXT NOT NULL DEFAULT '{}', candidate_ref TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(namespace, task_id)
            );
            CREATE TABLE work_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL, task_id TEXT NOT NULL,
                event_type TEXT NOT NULL, agent_id TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                FOREIGN KEY(namespace,task_id)
                    REFERENCES work_items(namespace,task_id) ON DELETE CASCADE
            );
            INSERT INTO work_items(
                namespace,task_id,title,lane,priority,state,created_at,updated_at
            ) VALUES
                ('wf','READY-ARCH','ready archive','general',1,'archived',
                 '2026-01-01T00:00:00.000Z','2026-01-03T00:00:00.000Z'),
                ('wf','DONE-ARCH','done archive','general',1,'archived',
                 '2026-01-01T00:00:00.000Z','2026-01-04T00:00:00.000Z');
            INSERT INTO work_events(namespace,task_id,event_type,agent_id,payload_json,created_at)
            VALUES
                ('wf','READY-ARCH','created','Legacy-A',
                 '{"state":"ready"}','2026-01-01T00:00:00.000Z'),
                ('wf','READY-ARCH','archived','Legacy-A',
                 '{"state":"archived","note":"obsolete ready work"}',
                 '2026-01-03T00:00:00.000Z'),
                ('wf','DONE-ARCH','created','Legacy-B',
                 '{"state":"ready"}','2026-01-01T00:00:00.000Z'),
                ('wf','DONE-ARCH','updated','Legacy-B',
                 '{"state":"done","result":"legacy goal reached"}',
                 '2026-01-02T00:00:00.000Z'),
                ('wf','DONE-ARCH','archived','Legacy-B',
                 '{"state":"archived","note":"retain completed history"}',
                 '2026-01-04T00:00:00.000Z');
            PRAGMA user_version=8;
            """
        )

    repo = SqliteRepository(database, tmp_path / "output.sqlite3")
    await repo.initialize()
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 20
        columns = {row[1] for row in db.execute("PRAGMA table_info(work_items)")}
        assert {
            "archived_at",
            "archive_note",
            "result_json",
            "ready_since",
            "state_changed_at",
            "isolation_hint",
        } <= columns
        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='work_items'"
        ).fetchone()[0]
        assert "'archived'" not in table_sql

        ready = db.execute(
            "SELECT state,archived_at,archive_note FROM work_items WHERE task_id='READY-ARCH'"
        ).fetchone()
        assert ready == (
            "ready",
            "2026-01-03T00:00:00.000Z",
            "obsolete ready work",
        )
        done = db.execute(
            "SELECT state,archived_at,archive_note FROM work_items WHERE task_id='DONE-ARCH'"
        ).fetchone()
        assert done == (
            "done",
            "2026-01-04T00:00:00.000Z",
            "retain completed history",
        )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT DISTINCT isolation_hint FROM work_items").fetchall() == [
            ("none",)
        ]

    terminal = LinuxTerminalAdapter(repo, "/bin/bash", tmp_path, 0.1)
    service = TerminalService(repo, terminal, 5000)
    try:
        archived = await service.tasks(namespace="wf", show_archived=True)
        by_id = {item["task_id"]: item for item in archived["tasks"]}
        assert by_id["READY-ARCH"]["state"] == "ready"
        assert by_id["DONE-ARCH"]["state"] == "done"
        active = await service.tasks(namespace="wf")
        assert active["tasks"] == []
    finally:
        await terminal.stop()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_dependency_blockers_project_operational_status_and_claimability(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = await register(service, "dependency status")

        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="status-deps",
            task_id="DEP",
            title="open dependency",
            priority="P3",
        )
        open_task = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="status-deps",
            task_id="OPEN",
            title="blocked by open dependency",
            priority="P0",
            dependencies=[{"namespace": "status-deps", "task_id": "DEP"}],
        )
        assert open_task["task"]["state"] == "ready"
        assert open_task["task"]["operational_status"] == "blocked"
        assert open_task["task"]["blocking_dependencies"][0]["task_id"] == "DEP"

        missing_task = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="status-deps",
            task_id="MISSING",
            title="blocked by missing dependency",
            priority="P0",
            dependencies=[{"namespace": "status-deps", "task_id": "FUTURE"}],
        )
        assert missing_task["task"]["state"] == "ready"
        assert missing_task["task"]["operational_status"] == "blocked"
        assert missing_task["task"]["blocking_dependencies"][0]["state"] == "missing"

        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="status-deps",
            task_id="READY",
            title="claimable candidate",
            priority="P1",
        )
        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="status-deps",
            task_id="DONE-DEP",
            title="completed prerequisite",
            state="done",
            result={"summary": "complete"},
        )
        satisfied = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="status-deps",
            task_id="SAT",
            title="satisfied dependency",
            priority="P2",
            dependencies=[{"namespace": "status-deps", "task_id": "DONE-DEP"}],
        )
        assert satisfied["task"]["operational_status"] == "ready"
        assert satisfied["task"]["blocking_dependencies"] == []

        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="status-deps",
            task_id="FORCED",
            title="forced active work",
            priority="P0",
            dependencies=[{"namespace": "status-deps", "task_id": "DEP"}],
        )
        forced = await service.task(
            agent,
            action="claim",
            namespace="status-deps",
            task_id="FORCED",
            claim_intent="work before prerequisite closes",
            force=True,
            force_reason="Parallel preparation is explicitly required",
        )
        assert forced["ok"] is True
        assert forced["task"]["operational_status"] == "blocked"
        assert forced["task"]["blocking_dependencies"][0]["task_id"] == "DEP"

        listing = await service.tasks(namespace="status-deps")
        cards = {item["task_id"]: item for item in listing["tasks"]}
        assert cards["OPEN"]["operational_status"] == "blocked"
        assert cards["MISSING"]["operational_status"] == "blocked"
        assert cards["READY"]["operational_status"] == "ready"
        assert cards["SAT"]["operational_status"] == "ready"
        assert cards["FORCED"]["operational_status"] == "blocked"
        assert listing["summary"]["by_operational_status"] == {
            "blocked": 3,
            "ready": 3,
        }
        assert listing["summary"]["missing_dependency_count"] == 1
        assert listing["summary"]["claimable_count"] == 3
        assert listing["summary"]["pressure"] == {"general": 7}
        assert listing["recommended"]["task_id"] == "READY"
        assert listing["recommended"]["operational_status"] == "ready"

        blocked = await service.tasks(namespace="status-deps", operational_status="blocked")
        assert {item["task_id"] for item in blocked["tasks"]} == {"OPEN", "MISSING", "FORCED"}
        assert blocked["summary"]["claimable_count"] == 0
        assert blocked["summary"]["pressure"] == {}

        ready = await service.tasks(namespace="status-deps", operational_status="ready")
        assert {item["task_id"] for item in ready["tasks"]} == {"DEP", "READY", "SAT"}
        assert ready["summary"]["claimable_count"] == 3
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_unclaimed_terminal_transitions_require_live_owner(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        actor = await register(service, "terminal transition actor")
        peer = await register(service, "terminal transition peer")
        await service.task(
            actor,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="OWNER-GATE",
            title="owner gate",
        )
        unclaimed_state = await service.task(
            actor, action="state", namespace="wf", task_id="OWNER-GATE", state="deferred"
        )
        assert unclaimed_state["ok"] is False
        assert unclaimed_state["code"] == "owner_required"
        unclaimed_done = await service.task(
            actor,
            action="done",
            namespace="wf",
            task_id="OWNER-GATE",
            result={"summary": "must claim first"},
        )
        assert unclaimed_done["ok"] is False
        assert unclaimed_done["code"] == "owner_required"
        safe_metadata = await service.task(
            peer,
            action="update",
            namespace="wf",
            task_id="OWNER-GATE",
            description="Safe metadata remains editable without ownership.",
        )
        assert safe_metadata["ok"] is True
        claimed = await service.task(
            actor,
            action="claim",
            namespace="wf",
            task_id="OWNER-GATE",
            claim_intent="own terminal transitions",
        )
        assert claimed["ok"] is True
        owned_state = await service.task(
            actor, action="state", namespace="wf", task_id="OWNER-GATE", state="deferred"
        )
        assert owned_state["ok"] is True
        owned_done = await service.task(
            actor,
            action="done",
            namespace="wf",
            task_id="OWNER-GATE",
            result={"summary": "owner completed task"},
        )
        assert owned_done["ok"] is True
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_expired_owner_and_force_cannot_bypass_owner_gate(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        owner = await register(service, "expiring workflow owner")
        peer = await register(service, "active workflow peer")
        await service.task(
            owner,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="EXPIRING-OWNER",
            title="expiring owner",
        )
        await service.task(
            owner,
            action="claim",
            namespace="wf",
            task_id="EXPIRING-OWNER",
            claim_intent="own until expiry",
        )
        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET last_activity_at=? WHERE agent_id=?",
                (utc_text(utc_now() - timedelta(seconds=301)), owner),
            )
            db.commit()
        after_expiry = await service.task(
            peer, action="state", namespace="wf", task_id="EXPIRING-OWNER", state="deferred"
        )
        assert after_expiry["ok"] is False
        assert after_expiry["code"] == "owner_required"
        await service.task(
            peer,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="FORCE-NO-OWNER-DEP",
            title="open dependency",
        )
        await service.task(
            peer,
            action="create",
            isolation_hint="none",
            namespace="wf",
            task_id="FORCE-NO-OWNER",
            title="force cannot own",
            dependencies=[{"namespace": "wf", "task_id": "FORCE-NO-OWNER-DEP"}],
        )
        forced = await service.task(
            peer,
            action="done",
            namespace="wf",
            task_id="FORCE-NO-OWNER",
            result={"summary": "force must not bypass ownership"},
            force=True,
            force_reason="dependency override only",
        )
        assert forced["ok"] is False
        assert forced["code"] == "owner_required"
        reclaimed = await service.task(
            peer,
            action="claim",
            namespace="wf",
            task_id="EXPIRING-OWNER",
            claim_intent="take ownership after expiry",
        )
        assert reclaimed["ok"] is True
        resumed = await service.task(
            peer, action="state", namespace="wf", task_id="EXPIRING-OWNER", state="deferred"
        )
        assert resumed["ok"] is True
    finally:
        await terminal.stop()
