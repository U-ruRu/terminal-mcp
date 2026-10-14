import sqlite3

import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.storage.sqlite import SCHEMA_VERSION, SqliteRepository
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
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
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
        assert unclaimed_state["ok"] is True
        assert unclaimed_state["task"]["state"] == "deferred"
        unclaimed_done = await service.task(
            actor,
            action="done",
            namespace="wf",
            task_id="OWNER-GATE",
            result={"summary": "must claim first"},
        )
        assert unclaimed_done["ok"] is True  # No live claim exists.
        assert unclaimed_done["task"]["state"] == "done"
        reopened = await service.task(
            actor,
            action="state",
            namespace="wf",
            task_id="OWNER-GATE",
            state="ready",
        )
        assert reopened["ok"] is True
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
