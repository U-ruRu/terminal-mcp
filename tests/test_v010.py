import asyncio
import sqlite3

import pytest

from terminal_mcp.core.service import TerminalService
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
