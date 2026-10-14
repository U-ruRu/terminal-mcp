import asyncio
import sqlite3

import pytest

from terminal_mcp.core.service import TerminalService
from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.storage.output import OutputStore
from terminal_mcp.storage.sqlite import SCHEMA_VERSION, SqliteRepository
from terminal_mcp.terminal.linux import LinuxTerminalAdapter


async def runtime(tmp_path, **repo_kwargs):
    repo = SqliteRepository(tmp_path / "db.sqlite3", tmp_path / "output.sqlite3", **repo_kwargs)
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


async def wait_done(service, cmd_hash, agent_id=None, attempts=300):
    for _ in range(attempts):
        result = await service.read(cmd_hash, 1000, 0, agent_id=agent_id)
        if result["status"] in {"completed", "failed", "cancelled"}:
            return result
        await asyncio.sleep(0.01)
    return await service.read(cmd_hash, 1000, 0, agent_id=agent_id)


@pytest.mark.asyncio
async def test_run_task_scope_is_explicit_and_no_automatic_fanout(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    await terminal.start()
    try:
        agent = await register(service, "runner")
        other = await register(service, "other")
        with sqlite3.connect(repo.path) as db:
            before_ad_hoc = db.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
        missing_ad_hoc = await service.run("printf should-not-queue", agent_id=agent)
        assert missing_ad_hoc["ok"] is False
        assert missing_ad_hoc["task_scope_options"] == ["none"]
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == before_ad_hoc

        overview = await service.agents(agent_id=agent)
        assert overview["task_scope_options"] == ["none"]

        ad_hoc = await service.run("printf adhoc", agent_id=agent, task_scope="none")
        assert ad_hoc["ok"] is True
        assert ad_hoc["task_scope_options"] == ["none"]
        await wait_done(service, ad_hoc["cmd_hash"], agent)

        for task_id in ("T1", "T2"):
            await service.task(
                agent,
                action="create",
                isolation_hint="none",
                namespace="ns",
                task_id=task_id,
                title=task_id,
                cooperative=True,
            )
        first_claim = await service.task(
            agent,
            action="claim",
            namespace="ns",
            task_id="T1",
            claim_intent="running scoped command for T1",
        )
        assert first_claim["ok"] is True
        second_claim = await service.task(
            agent,
            action="claim",
            namespace="ns",
            task_id="T2",
            claim_intent="WIP must reject second task",
        )
        assert second_claim["ok"] is False
        assert second_claim["code"] == "agent_busy"

        overview = await service.agents(agent_id=agent)
        assert set(overview["task_scope_options"]) == {"none", "all", "ns/T1"}

        with sqlite3.connect(repo.path) as db:
            before = db.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
        missing = await service.run("printf should-not-queue", agent_id=agent)
        assert missing["ok"] is False
        assert missing["task_targets"] == []
        assert set(missing["task_scope_options"]) == {"none", "all", "ns/T1"}
        with sqlite3.connect(repo.path) as db:
            assert db.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == before

        none = await service.run("printf none", agent_id=agent, task_scope="none")
        assert none["ok"] is True
        await wait_done(service, none["cmd_hash"], agent)

        all_run = await service.run("printf all", agent_id=agent, task_scope="all")
        assert all_run["ok"] is True
        await wait_done(service, all_run["cmd_hash"], agent)

        one = await service.run("printf one", agent_id=agent, task_scope="ns/T1")
        assert one["ok"] is True
        await wait_done(service, one["cmd_hash"], agent)

        await service.task(
            other,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="FOREIGN",
            title="foreign",
        )
        await service.task(
            other,
            action="claim",
            namespace="ns",
            task_id="FOREIGN",
            claim_intent="owning foreign scoped task",
        )
        denied = await service.run("printf denied", agent_id=agent, task_scope="ns/FOREIGN")
        assert denied["ok"] is False

        details = {}
        for task_id in ("T1", "T2"):
            details[task_id] = (
                await service.tasks(namespace="ns", task_id=task_id, show_details=True)
            )["task"]["events"]

        def command_hashes(events):
            return {
                item["payload"]["command_hash"]
                for item in events
                if item["event_type"] == "command"
            }

        assert none["cmd_hash"] not in command_hashes(details["T1"])
        assert none["cmd_hash"] not in command_hashes(details["T2"])
        assert all_run["cmd_hash"] in command_hashes(details["T1"])
        assert all_run["cmd_hash"] not in command_hashes(details["T2"])
        assert one["cmd_hash"] in command_hashes(details["T1"])
        assert one["cmd_hash"] not in command_hashes(details["T2"])
        assert ad_hoc["cmd_hash"] not in command_hashes(details["T1"])
        assert ad_hoc["cmd_hash"] not in command_hashes(details["T2"])
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_output_prune_default_creates_half_capacity_headroom_with_safe_busy_timeout(tmp_path):
    store = OutputStore(tmp_path / "output.sqlite3")
    assert store.prune_rows == 500_000
    async with store._connect("test_output_busy_timeout") as db:
        timeout = await (await db.execute("PRAGMA busy_timeout")).fetchone()
    assert timeout[0] == 5000


@pytest.mark.asyncio
async def test_line_prune_creates_headroom_and_preserves_chunks(tmp_path):
    store = OutputStore(
        tmp_path / "output.sqlite3",
        target_bytes=10_000_000,
        max_bytes=20_000_000,
        max_rows=10,
        prune_rows=5,
    )
    await store.initialize()
    hashes = [f"{index:08x}" for index in range(12)]
    for cmd_hash in hashes:
        await store.append_lines(cmd_hash, [cmd_hash])
    before = await store.stats()
    assert before["lines"] == 12

    pruned = await store.prune(set())
    after = await store.stats()
    assert pruned
    assert after["lines"] <= 5
    assert before["lines"] - after["lines"] >= 7
    for cmd_hash in hashes:
        count = await store.count_lines(cmd_hash)
        assert count in {0, 1}


@pytest.mark.asyncio
async def test_v8_to_v9_migration_preserves_result_and_initializes_task_metadata(tmp_path):
    database = tmp_path / "legacy-v8.sqlite3"
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
            INSERT INTO work_items(
                namespace,task_id,title,lane,priority,state,candidate_ref,created_at,updated_at
            ) VALUES(
                'project','READY-1','Ready','general',1,'ready','legacy-sha',
                '2026-01-01T00:00:00.000Z','2026-01-03T00:00:00.000Z'
            );
            INSERT INTO work_items(
                namespace,task_id,title,lane,priority,state,created_at,updated_at
            ) VALUES(
                'project','BLOCKED-1','Blocked','general',1,'blocked',
                '2026-01-01T00:00:00.000Z','2026-01-04T00:00:00.000Z'
            );
            CREATE TABLE work_reviews(
                namespace TEXT NOT NULL, task_id TEXT NOT NULL,
                candidate_ref TEXT NOT NULL DEFAULT '',
                dimension TEXT NOT NULL CHECK(dimension IN ('A','C','R')),
                verdict TEXT NOT NULL, agent_id TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                reviewed_at TEXT NOT NULL,
                PRIMARY KEY(namespace,task_id,candidate_ref,dimension),
                FOREIGN KEY(namespace,task_id)
                    REFERENCES work_items(namespace,task_id) ON DELETE CASCADE
            );
            INSERT INTO work_reviews(
                namespace,task_id,candidate_ref,dimension,verdict,agent_id,
                evidence_json,warnings_json,reviewed_at
            ) VALUES(
                'project','READY-1','legacy-sha','C','NON_BLOCKING','Reviewer-1',
                '{"tests":1}','[]','2026-01-03T01:00:00.000Z'
            );
            PRAGMA user_version=8;
            """
        )

    repo = SqliteRepository(database, tmp_path / "output.sqlite3")
    await repo.initialize()
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row[1] for row in db.execute("PRAGMA table_info(work_items)")}
        assert {"result_json", "state_changed_at", "ready_since", "tags_json"} <= columns
        ready = db.execute(
            "SELECT result_json,state_changed_at,ready_since,tags_json "
            "FROM work_items WHERE task_id='READY-1'"
        ).fetchone()
        assert ready == (
            None,
            "2026-01-03T00:00:00.000Z",
            "2026-01-03T00:00:00.000Z",
            "[]",
        )
        blocked = db.execute(
            "SELECT state_changed_at,ready_since,tags_json "
            "FROM work_items WHERE task_id='BLOCKED-1'"
        ).fetchone()
        assert blocked == ("2026-01-04T00:00:00.000Z", None, "[]")
        legacy_review = db.execute(
            "SELECT candidate_ref,dimension,verdict,agent_id,evidence_json "
            "FROM work_reviews WHERE namespace='project' AND task_id='READY-1'"
        ).fetchone()
        assert legacy_review == (
            "legacy-sha",
            "C",
            "NON_BLOCKING",
            "Reviewer-1",
            '{"tests":1}',
        )
        migrated_task = db.execute(
            "SELECT output_refs_json,output_state_id FROM work_items "
            "WHERE namespace='project' AND task_id='READY-1'"
        ).fetchone()
        migrated_review = db.execute(
            "SELECT output_refs_json,output_state_id FROM work_reviews "
            "WHERE namespace='project' AND task_id='READY-1'"
        ).fetchone()
        assert migrated_task[0] == '["legacy-sha"]'
        assert migrated_review[0] == '["legacy-sha"]'
        assert migrated_task[1] == migrated_review[1]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_mcp_schema_has_unified_task_contract():
    class FakeService:
        pass

    mcp = build_mcp(FakeService())
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert set(tools) == {"session", "observe", "message", "task", "cmd", "context", "health"}

    task = tools["task"].parameters
    # Planning intentionally permits partial inputs; strict validation runs in the tool.
    assert not task.get("required")
    assert set(task["properties"]) == {"request"}
    assert "action" in task["properties"]["request"]["properties"]
    runtime_task = task["x-runtime-schema"]
    assert runtime_task["required"] == ["request"]
    request = runtime_task["properties"]["request"]
    assert request["discriminator"]["propertyName"] == "action"
    assert set(request["discriminator"]["mapping"]) == {
        "create",
        "claim",
        "release",
        "update",
        "checkpoint",
        "comment",
        "relate",
        "unrelate",
        "state",
        "archive",
        "review",
    }
    assert len(request["oneOf"]) == 11
    assert "payload" not in task["properties"]
    claim = runtime_task["$defs"]["TaskClaimRequest"]
    claim_code = claim["properties"]["code"]["anyOf"][0]
    assert claim_code["minLength"] == 4
    assert claim_code["maxLength"] == 4
    assert "code" not in claim["required"]

    observe = tools["observe"].parameters["properties"]
    assert observe["subject"]["enum"] == ["sessions", "tasks", "namespaces"]
    assert "code" not in observe
    assert observe["state"]["anyOf"][0]["enum"] == [
        "ready",
        "in_progress",
        "qa",
        "blocked",
        "deferred",
        "done",
    ]
    assert observe["operational_status"]["anyOf"][0]["enum"] == [
        "ready",
        "in_progress",
        "qa",
        "blocked",
        "deferred",
        "done",
    ]
    assert "tags" in observe

    cmd = tools["cmd"].parameters
    mapping = cmd["properties"]["request"]["discriminator"]["mapping"]
    assert set(mapping) == {"read", "run", "cancel", "recovery"}
    run = cmd["$defs"]["CmdRunRequest"]
    assert {"action", "command"} <= set(run["required"])
    assert "code" not in run["required"]
    assert "task_scope" in run["properties"]
    read = cmd["$defs"]["CmdReadRequest"]
    assert "code" in read["properties"]
    assert "code" not in read["required"]

    message = tools["message"].parameters
    assert "sender" not in message.get("properties", {})
    assert "required" not in message or "sender" not in message["required"]
    assert message["properties"]["code"]["anyOf"][0]["minLength"] == 4
    assert message["properties"]["code"]["anyOf"][0]["maxLength"] == 4
    assert "active unified session" in tools["message"].description
    assert "Provider-bound managed callers" in tools["message"].description
    assert "omit code" in tools["message"].description


@pytest.mark.asyncio
async def test_non_cooperative_concurrent_claim_has_single_winner(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        one = await register(service, "race-one")
        two = await register(service, "race-two")
        created = await service.task(
            one,
            action="create",
            isolation_hint="none",
            namespace="race",
            task_id="ONE-OWNER",
            title="single live owner",
        )
        assert created["ok"] is True
        left, right = await asyncio.gather(
            service.task(
                one,
                action="claim",
                namespace="race",
                task_id="ONE-OWNER",
                claim_intent="racing for exclusive ownership one",
            ),
            service.task(
                two,
                action="claim",
                namespace="race",
                task_id="ONE-OWNER",
                claim_intent="racing for exclusive ownership two",
            ),
        )
        assert sum(result["ok"] is True for result in (left, right)) == 1
        loser = left if not left["ok"] else right
        assert loser["code"] == "already_claimed"
        detail = await service.tasks(namespace="race", task_id="ONE-OWNER", show_details=True)
        assert len(detail["task"]["claims"]) == 1
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_archived_dependency_stays_blocking_and_done_create_requires_result(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = await register(service, "dependency")
        missing_result = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="DONE-NO-RESULT",
            title="invalid done create",
            state="done",
        )
        assert missing_result["ok"] is False
        assert "result" in missing_result["error"]
        empty_create = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="DONE-EMPTY-RESULT",
            title="invalid empty result",
            state="done",
            result="   ",
        )
        assert empty_create["ok"] is False
        assert "result" in empty_create["error"]

        with_result = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="DONE-WITH-RESULT",
            title="valid done create",
            state="done",
            result="completed",
        )
        assert with_result["ok"] is True
        assert with_result["task"]["result"] == "completed"

        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="ARCHIVED-DEP",
            title="archived incomplete dependency",
        )
        archived = await service.task(
            agent,
            action="archive",
            namespace="ns",
            task_id="ARCHIVED-DEP",
            archive_note="Superseded without completion",
        )
        assert archived["ok"] is True

        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="NEEDS-ARCHIVED",
            title="must remain gated",
            dependencies=[{"namespace": "ns", "task_id": "ARCHIVED-DEP"}],
        )
        rejected = await service.task(
            agent,
            action="claim",
            namespace="ns",
            task_id="NEEDS-ARCHIVED",
            claim_intent="waiting on archived unfinished dependency",
        )
        assert rejected["ok"] is False
        assert rejected["code"] == "dependency_open"
        dependency = rejected["blocking_dependencies"][0]
        assert dependency["state"] == "ready"
        assert dependency["archived"] is True
        assert dependency["satisfied"] is False
    finally:
        await terminal.stop()
