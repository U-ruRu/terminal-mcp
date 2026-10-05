import asyncio
import sqlite3
from datetime import timedelta

import pytest

from terminal_mcp.core.orchestration import public_agent_name, utc_now, utc_text
from terminal_mcp.core.service import TerminalService
from terminal_mcp.mcp.server import build_mcp
from terminal_mcp.storage.output import OutputStore
from terminal_mcp.storage.sqlite import SqliteRepository
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
async def test_claim_contracts_dependency_force_and_audit(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        one = await register(service, "one")
        two = await register(service, "two")
        await service.task(
            one,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="DEP",
            title="dependency",
        )
        await service.task(
            one,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="EXCLUSIVE",
            title="exclusive",
        )
        first = await service.task(
            one,
            action="claim",
            namespace="ns",
            task_id="EXCLUSIVE",
            claim_intent="owning exclusive task",
        )
        assert first["ok"] is True
        second = await service.task(
            two,
            action="claim",
            namespace="ns",
            task_id="EXCLUSIVE",
            claim_intent="attempting conflicting ownership",
        )
        assert second["ok"] is False
        assert second["code"] == "already_claimed"
        assert len(second["task"]["claims"]) == 1

        forced_owner = await service.task(
            two,
            action="claim",
            namespace="ns",
            task_id="EXCLUSIVE",
            claim_intent="verifying force does not bypass ownership",
            force=True,
            force_reason="Force is dependency-only and must not bypass ownership",
        )
        assert forced_owner["ok"] is False
        assert forced_owner["code"] == "already_claimed"
        assert len(forced_owner["task"]["claims"]) == 1

        await service.task(
            one,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="COOP",
            title="cooperative",
            cooperative=True,
        )
        busy = await service.task(
            one,
            action="claim",
            namespace="ns",
            task_id="COOP",
            claim_intent="must respect one-task WIP",
        )
        assert busy["ok"] is False
        assert busy["code"] == "agent_busy"
        assert (
            await service.task(
                one,
                action="release",
                namespace="ns",
                task_id="EXCLUSIVE",
                release_reason="move to cooperative task",
            )
        )["ok"]
        assert (
            await service.task(
                one,
                action="claim",
                namespace="ns",
                task_id="COOP",
                claim_intent="cooperative owner",
            )
        )["ok"]
        assert (
            await service.task(
                two,
                action="claim",
                namespace="ns",
                task_id="COOP",
                claim_intent="cooperative participant",
            )
        )["ok"]
        coop = await service.tasks(namespace="ns", task_id="COOP", show_details=True)
        assert len(coop["task"]["claims"]) == 2
        for owner in (one, two):
            released = await service.task(
                owner,
                action="release",
                namespace="ns",
                task_id="COOP",
                release_reason="continue contract coverage",
            )
            assert released["ok"] is True

        await service.task(
            one,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="BLOCKED-BY-DEP",
            title="dependency gated",
            dependencies=[{"namespace": "ns", "task_id": "DEP"}],
        )
        blocked = await service.task(
            two,
            action="claim",
            namespace="ns",
            task_id="BLOCKED-BY-DEP",
            claim_intent="waiting for dependency",
        )
        assert blocked["ok"] is False
        assert blocked["code"] == "dependency_open"
        assert blocked["blocking_dependencies"] == [
            {
                "namespace": "ns",
                "task_id": "DEP",
                "state": "ready",
                "archived": False,
                "satisfied": False,
            }
        ]

        missing_reason = await service.task(
            two,
            action="claim",
            namespace="ns",
            task_id="BLOCKED-BY-DEP",
            claim_intent="testing forced dependency gate",
            force=True,
        )
        assert missing_reason["ok"] is False
        assert "force_reason" in missing_reason["error"]

        forced = await service.task(
            two,
            action="claim",
            namespace="ns",
            task_id="BLOCKED-BY-DEP",
            claim_intent="emergency integration validation",
            force=True,
            force_reason="Emergency integration validation requires parallel access",
        )
        assert forced["ok"] is True
        detail = await service.tasks(namespace="ns", task_id="BLOCKED-BY-DEP", show_details=True)
        forced_events = [
            event for event in detail["task"]["events"] if event["payload"].get("force_reason")
        ]
        assert forced_events
        payload = forced_events[0]["payload"]
        assert payload["force_reason"] == (
            "Emergency integration validation requires parallel access"
        )
        assert payload["blocking_dependencies"][0]["task_id"] == "DEP"
        assert forced_events[0]["agent_name"] == public_agent_name(two)
    finally:
        await terminal.stop()


@pytest.mark.asyncio
async def test_result_review_ready_timestamps_and_reopen(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        agent = await register(service, "reviewer")
        created = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="ns",
            task_id="REV-10",
            title="Unified review",
            lane="review",
            candidate_ref="opaque-candidate",
            tags=["architecture", "runtime"],
        )
        task = created["task"]
        assert task["state_changed_at"] == task["created_at"]
        assert task["ready_since"] == task["created_at"]
        original_ready_since = task["ready_since"]
        claimed = await service.task(
            agent,
            action="claim",
            namespace="ns",
            task_id="REV-10",
            claim_intent="review workflow transitions",
        )
        assert claimed["ok"] is True

        checkpointed = await service.task(
            agent,
            action="checkpoint",
            namespace="ns",
            task_id="REV-10",
            checkpoint={"note": "inspected"},
        )
        assert checkpointed["task"]["ready_since"] == original_ready_since
        updated = await service.task(
            agent,
            action="update",
            namespace="ns",
            task_id="REV-10",
            next_action="continue review",
        )
        assert updated["task"]["ready_since"] == original_ready_since

        blocked = await service.task(
            agent,
            action="state",
            namespace="ns",
            task_id="REV-10",
            state="blocked",
            blocker_reason="review needs another inspection pass",
        )
        assert blocked["task"]["ready_since"] is None
        assert blocked["task"]["state_changed_at"] != task["state_changed_at"]

        await asyncio.sleep(0.002)
        ready = await service.task(
            agent, action="state", namespace="ns", task_id="REV-10", state="ready"
        )
        assert ready["task"]["ready_since"] is not None
        assert ready["task"]["ready_since"] != original_ready_since

        missing = await service.task(agent, action="done", namespace="ns", task_id="REV-10")
        assert missing["ok"] is False
        for empty_result in ("", "   ", {}):
            rejected = await service.task(
                agent,
                action="done",
                namespace="ns",
                task_id="REV-10",
                result=empty_result,
            )
            assert rejected["ok"] is False
            assert "result" in rejected["error"]
        unchanged = await service.tasks(namespace="ns", task_id="REV-10", show_details=True)
        assert unchanged["task"]["state"] == "ready"

        result = {"verdict": "approved", "evidence": ["tests", "inspection"]}
        done = await service.task(
            agent, action="done", namespace="ns", task_id="REV-10", result=result
        )
        assert done["ok"] is True
        detail = await service.tasks(namespace="ns", task_id="REV-10", show_details=True)
        assert detail["task"]["result"] == result
        assert detail["task"]["lane"] == "review"

        idempotent = await service.task(agent, action="done", namespace="ns", task_id="REV-10")
        assert idempotent["ok"] is True
        assert idempotent["task"]["result"] == result
    finally:
        await terminal.stop()

    reopened = SqliteRepository(repo.path, tmp_path / "output-reopened.sqlite3")
    await reopened.initialize()
    reopened_terminal = LinuxTerminalAdapter(reopened, "/bin/bash", tmp_path, 0.1)
    reopened_service = TerminalService(reopened, reopened_terminal, 5000)
    try:
        detail = await reopened_service.tasks(namespace="ns", task_id="REV-10", show_details=True)
        assert detail["task"]["result"] == {
            "verdict": "approved",
            "evidence": ["tests", "inspection"],
        }
    finally:
        await reopened_terminal.stop()


@pytest.mark.asyncio
async def test_tags_filter_discovery_pressure_and_oldest_ready(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        one = await register(service, "one")
        two = await register(service, "two")

        async def create(
            task_id, *, lane, priority="P2", tags=None, cooperative=False, dependencies=None
        ):
            return await service.task(
                one,
                action="create",
                isolation_hint="none",
                namespace="ns",
                task_id=task_id,
                title=task_id,
                lane=lane,
                priority=priority,
                tags=tags or [],
                cooperative=cooperative,
                dependencies=dependencies,
            )

        await create("DEP", lane="general")
        await create(
            "DEP-GATED",
            lane="release",
            priority="P0",
            tags=["architecture"],
            dependencies=[{"namespace": "ns", "task_id": "DEP"}],
        )
        await create(
            "EXCLUSIVE",
            lane="review",
            priority="P0",
            tags=["architecture", "correctness"],
        )
        await service.task(
            one,
            action="claim",
            namespace="ns",
            task_id="EXCLUSIVE",
            claim_intent="occupying exclusive routing work",
        )
        await create(
            "COOP",
            lane="implementation",
            priority="P2",
            tags=["runtime"],
            cooperative=True,
        )
        await service.task(
            one,
            action="claim",
            namespace="ns",
            task_id="COOP",
            claim_intent="cooperative routing owner",
        )
        await service.task(
            two,
            action="claim",
            namespace="ns",
            task_id="COOP",
            claim_intent="cooperative routing participant",
        )
        await create("OLD", lane="general", priority="P1", tags=["architecture", "runtime"])
        await create("NEW", lane="general", priority="P1", tags=["architecture"])

        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE work_items SET ready_since=? WHERE namespace='ns' AND task_id='OLD'",
                ("2026-01-01T00:00:00.000Z",),
            )
            db.execute(
                "UPDATE work_items SET ready_since=? WHERE namespace='ns' AND task_id='NEW'",
                ("2026-01-02T00:00:00.000Z",),
            )
            db.commit()

        filtered = await service.tasks(namespace="ns", tags=["architecture"])
        assert {item["task_id"] for item in filtered["tasks"]} == {
            "DEP-GATED",
            "EXCLUSIVE",
            "OLD",
            "NEW",
        }
        assert filtered["tag_counts"]["runtime"] >= 2
        assert filtered["tag_counts"]["correctness"] == 1
        intersection = await service.tasks(namespace="ns", tags=["architecture", "runtime"])
        assert {item["task_id"] for item in intersection["tasks"]} == {"OLD"}

        listing = await service.tasks(namespace="ns")
        assert listing["recommended"]["task_id"] == "OLD"
        pressure = listing["summary"]["pressure"]
        assert "release" not in pressure
        assert "review" not in pressure
        assert pressure["implementation"] > 0
        assert pressure["general"] > 0

        updated = await service.task(
            one,
            action="update",
            namespace="ns",
            task_id="NEW",
            tags=["architecture", "correctness", "custom:user-tag"],
        )
        assert updated["task"]["tags"] == [
            "architecture",
            "correctness",
            "custom:user-tag",
        ]
        detail = await service.tasks(namespace="ns", task_id="NEW", show_details=True)
        assert detail["task"]["tags"] == [
            "architecture",
            "correctness",
            "custom:user-tag",
        ]
    finally:
        await terminal.stop()


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
async def test_broadcast_alias_and_post_finish_message_grace(tmp_path):
    repo, terminal, service = await runtime(tmp_path)
    try:
        sender = await register(service, "sender")
        recipient = await register(service, "recipient")
        outsider = await register(service, "outsider")
        recipient_name = public_agent_name(recipient)

        implicit = await service.message(sender, text="implicit")
        explicit = await service.message(sender, text="explicit", target="broadcast")
        assert set(implicit["delivered_to"]) == set(explicit["delivered_to"])

        unsafe_alert = await service.message(sender, text="unsafe alert", alert=True)
        assert unsafe_alert["ok"] is False
        assert unsafe_alert["error"].startswith(
            "message.alert: ALERT requires an explicit destination"
        )

        created = await service.task(
            recipient,
            action="create",
            isolation_hint="none",
            namespace="messages",
            task_id="ALERT-TARGET",
            title="Task alert target",
        )
        assert created["ok"] is True
        claimed = await service.task(
            recipient,
            action="claim",
            namespace="messages",
            task_id="ALERT-TARGET",
            claim_intent="receive task alert",
        )
        assert claimed["ok"] is True
        task_alert = await service.message(
            sender,
            text="task alert",
            namespace="messages",
            task_id="ALERT-TARGET",
            alert=True,
        )
        assert task_alert["ok"] is True
        assert task_alert["delivered_to"] == [recipient_name]

        explicit_alert = await service.message(
            sender, text="explicit broadcast alert", target="broadcast", alert=True
        )
        assert explicit_alert["ok"] is True
        assert set(explicit_alert["delivered_to"]) == set(explicit["delivered_to"])

        ack_message = await service.message(sender, text="ack me", target=recipient_name)
        reply_message = await service.message(sender, text="reply me", target=recipient_name)
        alert_message = await service.message(
            sender, text="urgent reply", target=recipient_name, alert=True
        )
        assert ack_message["ok"] and reply_message["ok"] and alert_message["ok"]
        alert_record = await service.agent_coordinator.store.message_record(
            alert_message["message_hash"]
        )
        assert alert_record["alert"] is True
        assert alert_record["require_reply"] is True

        foreign = await service.message(outsider, message_hash=ack_message["message_hash"])
        assert foreign["ok"] is False

        finished = await service.agent_finish(recipient)
        assert finished["ok"] is True
        pending = finished["pending_communication"]
        assert alert_message["message_hash"] in pending["unacknowledged"]
        assert alert_message["message_hash"] in pending["reply_required"]
        assert alert_message["message_hash"] in pending["alerts"]

        sender_status = await service.message(sender, message_hash=alert_message["message_hash"])
        assert recipient_name in sender_status["inactive_recipients"]
        assert "message_grace_remaining_seconds" not in sender_status

        foreign_after_finish = await service.message(
            outsider, message_hash=ack_message["message_hash"]
        )
        assert foreign_after_finish["ok"] is False
        ack = await service.message(recipient, message_hash=ack_message["message_hash"])
        assert ack["ok"] is True
        assert 0 <= ack["message_grace_remaining_seconds"] <= 300
        reply = await service.message(
            recipient,
            message_hash=reply_message["message_hash"],
            text="finished reply",
        )
        assert reply["ok"] is True

        with sqlite3.connect(repo.path) as db:
            db.execute(
                "UPDATE agent_sessions SET ended_at=? WHERE agent_id=?",
                (utc_text(utc_now() - timedelta(seconds=301)), recipient),
            )
            db.commit()
        expired = await service.message(recipient, message_hash=ack_message["message_hash"])
        assert expired["ok"] is False
        assert expired["session_expired"] is True
        assert expired["registration_required"] is True
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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 19
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
    assert task["required"] == ["code", "action", "namespace"]
    assert set(task["properties"]["action"]["enum"]) == {
        "create",
        "claim",
        "release",
        "update",
        "checkpoint",
        "comment",
        "relate",
        "unrelate",
        "state",
        "done",
        "archive",
        "review",
    }
    assert task["properties"]["code"]["minLength"] == 4
    assert task["properties"]["code"]["maxLength"] == 4

    observe = tools["observe"].parameters["properties"]
    assert observe["subject"]["enum"] == ["sessions", "tasks", "namespaces"]
    assert "code" not in observe
    assert observe["state"]["anyOf"][0]["enum"] == [
        "ready",
        "in_progress",
        "blocked",
        "deferred",
        "done",
    ]
    assert observe["operational_status"]["anyOf"][0]["enum"] == [
        "ready",
        "in_progress",
        "blocked",
        "deferred",
        "done",
    ]
    assert "tags" in observe

    cmd = tools["cmd"].parameters
    mapping = cmd["properties"]["request"]["discriminator"]["mapping"]
    assert set(mapping) == {"read", "run", "cancel", "recovery"}
    run = cmd["$defs"]["CmdRunRequest"]
    assert {"action", "code", "command"} <= set(run["required"])
    assert "task_scope" in run["properties"]
    read = cmd["$defs"]["CmdReadRequest"]
    assert "code" in read["properties"]
    assert "code" not in read["required"]

    message = tools["message"].parameters
    assert message["required"] == ["sender"]
    assert message["properties"]["code"]["anyOf"][0]["minLength"] == 4
    assert message["properties"]["code"]["anyOf"][0]["maxLength"] == 4
    assert "active unified session" in tools["message"].description
    assert "same Access code used by cmd/task/context" in tools["message"].description


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


@pytest.mark.asyncio
async def test_terminal_completion_enforces_dependencies_and_force_audit(tmp_path):
    _, terminal, service = await runtime(tmp_path)
    try:
        agent = await register(service, "dependency completion")

        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="deps",
            task_id="OPEN",
            title="open prerequisite",
        )
        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="deps",
            task_id="DIRECT",
            title="direct completion",
            dependencies=[{"namespace": "deps", "task_id": "OPEN"}],
        )

        unclaimed_direct = await service.task(
            agent,
            action="done",
            namespace="deps",
            task_id="DIRECT",
            result={"summary": "must claim before completion"},
        )
        assert unclaimed_direct["ok"] is False
        assert unclaimed_direct["code"] == "owner_required"
        direct_claim = await service.task(
            agent,
            action="claim",
            namespace="deps",
            task_id="DIRECT",
            claim_intent="exercise completion dependency gate",
            force=True,
            force_reason="Need an owner to verify terminal dependency enforcement",
        )
        assert direct_claim["ok"] is True
        direct = await service.task(
            agent,
            action="done",
            namespace="deps",
            task_id="DIRECT",
            result={"summary": "must remain blocked"},
        )
        assert direct["ok"] is False
        assert direct["code"] == "dependency_open"
        assert direct["blocking_dependencies"][0]["task_id"] == "OPEN"
        assert direct["task"]["state"] == "ready"

        via_state = await service.task(
            agent,
            action="state",
            namespace="deps",
            task_id="DIRECT",
            state="done",
            result={"summary": "state path must also remain blocked"},
        )
        assert via_state["ok"] is False
        assert via_state["code"] == "dependency_open"
        assert via_state["blocking_dependencies"][0]["task_id"] == "OPEN"

        via_update = await service.task(
            agent,
            action="update",
            namespace="deps",
            task_id="DIRECT",
            state="done",
            result={"summary": "update path must also remain blocked"},
        )
        assert via_update["ok"] is False
        assert via_update["code"] == "dependency_open"

        missing_reason = await service.task(
            agent,
            action="done",
            namespace="deps",
            task_id="DIRECT",
            result={"summary": "force requires reason"},
            force=True,
        )
        assert missing_reason["ok"] is False
        assert missing_reason["code"] == "dependency_open"
        assert "force_reason" in missing_reason["error"]
        released_direct = await service.task(
            agent,
            action="release",
            namespace="deps",
            task_id="DIRECT",
            release_reason="continue dependency coverage",
        )
        assert released_direct["ok"] is True

        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="deps",
            task_id="CLAIMED",
            title="forced claimed completion",
            dependencies=[{"namespace": "deps", "task_id": "OPEN"}],
        )
        forced_claim = await service.task(
            agent,
            action="claim",
            namespace="deps",
            task_id="CLAIMED",
            claim_intent="claim does not authorize completion bypass",
            force=True,
            force_reason="Need to prepare work before dependency finishes",
        )
        assert forced_claim["ok"] is True

        claimed_done = await service.task(
            agent,
            action="done",
            namespace="deps",
            task_id="CLAIMED",
            result={"summary": "still blocked at completion"},
        )
        assert claimed_done["ok"] is False
        assert claimed_done["code"] == "dependency_open"
        assert claimed_done["task"]["state"] == "ready"
        released_claimed = await service.task(
            agent,
            action="release",
            namespace="deps",
            task_id="CLAIMED",
            release_reason="continue dependency coverage",
        )
        assert released_claimed["ok"] is True

        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="deps",
            task_id="MISSING",
            title="missing prerequisite",
            dependencies=[{"namespace": "deps", "task_id": "DOES-NOT-EXIST"}],
        )
        missing_claim = await service.task(
            agent,
            action="claim",
            namespace="deps",
            task_id="MISSING",
            claim_intent="exercise missing dependency completion",
            force=True,
            force_reason="Need an owner to verify missing-dependency completion gate",
        )
        assert missing_claim["ok"] is True
        missing = await service.task(
            agent,
            action="done",
            namespace="deps",
            task_id="MISSING",
            result={"summary": "missing dependency blocks completion"},
        )
        assert missing["ok"] is False
        assert missing["code"] == "dependency_open"
        assert missing["blocking_dependencies"][0]["state"] == "missing"
        released_missing = await service.task(
            agent,
            action="release",
            namespace="deps",
            task_id="MISSING",
            release_reason="continue dependency coverage",
        )
        assert released_missing["ok"] is True

        create_done = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="deps",
            task_id="CREATE-DONE",
            title="create already done",
            state="done",
            result={"summary": "must not bypass dependency gate"},
            dependencies=[{"namespace": "deps", "task_id": "OPEN"}],
        )
        assert create_done["ok"] is False
        assert create_done["code"] == "dependency_open"
        assert create_done["blocking_dependencies"][0]["task_id"] == "OPEN"
        assert (await service.tasks(namespace="deps", task_id="CREATE-DONE"))["ok"] is False

        forced_create = await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="deps",
            task_id="CREATE-DONE-FORCED",
            title="forced create already done",
            state="done",
            result={"summary": "forced creation"},
            dependencies=[{"namespace": "deps", "task_id": "OPEN"}],
            force=True,
            force_reason="External completion must be represented immediately",
        )
        assert forced_create["ok"] is True
        assert {item["code"] for item in forced_create["warnings"]} == {
            "dependency_open",
            "dependency_forced",
        }
        forced_create_detail = await service.tasks(
            namespace="deps",
            task_id="CREATE-DONE-FORCED",
            show_details=True,
            show_done=True,
        )
        create_overrides = [
            event
            for event in forced_create_detail["task"]["events"]
            if event["event_type"] == "dependency_override"
        ]
        assert create_overrides
        assert create_overrides[0]["payload"]["operation"] == "done"
        assert create_overrides[0]["payload"]["force_reason"] == (
            "External completion must be represented immediately"
        )

        reclaim_direct = await service.task(
            agent,
            action="claim",
            namespace="deps",
            task_id="DIRECT",
            claim_intent="record explicit forced completion",
            force=True,
            force_reason="Dependency override is tested at completion",
        )
        assert reclaim_direct["ok"] is True

        forced_done = await service.task(
            agent,
            action="done",
            namespace="deps",
            task_id="DIRECT",
            result={"summary": "explicit emergency completion"},
            force=True,
            force_reason="Dependency is externally satisfied and completion must be recorded now",
        )
        assert forced_done["ok"] is True
        assert {item["code"] for item in forced_done["warnings"]} == {
            "dependency_open",
            "dependency_forced",
        }
        detail = await service.tasks(
            namespace="deps", task_id="DIRECT", show_details=True, show_done=True
        )
        overrides = [
            event
            for event in detail["task"]["events"]
            if event["event_type"] == "dependency_override"
        ]
        assert overrides
        assert overrides[0]["payload"]["force_reason"] == (
            "Dependency is externally satisfied and completion must be recorded now"
        )
        assert overrides[0]["payload"]["blocking_dependencies"][0]["task_id"] == "OPEN"
        assert overrides[0]["payload"]["operation"] == "done"

        open_claim = await service.task(
            agent,
            action="claim",
            namespace="deps",
            task_id="OPEN",
            claim_intent="complete prerequisite as owner",
        )
        assert open_claim["ok"] is True
        satisfied_dep = await service.task(
            agent,
            action="done",
            namespace="deps",
            task_id="OPEN",
            result={"summary": "dependency completed"},
        )
        assert satisfied_dep["ok"] is True
        await service.task(
            agent,
            action="create",
            isolation_hint="none",
            namespace="deps",
            task_id="SATISFIED",
            title="satisfied prerequisite",
            dependencies=[{"namespace": "deps", "task_id": "OPEN"}],
        )
        satisfied_claim = await service.task(
            agent,
            action="claim",
            namespace="deps",
            task_id="SATISFIED",
            claim_intent="complete after dependency satisfied",
        )
        assert satisfied_claim["ok"] is True
        satisfied = await service.task(
            agent,
            action="done",
            namespace="deps",
            task_id="SATISFIED",
            result={"summary": "normal completion"},
        )
        assert satisfied["ok"] is True
        assert satisfied["task"]["state"] == "done"
    finally:
        await terminal.stop()
