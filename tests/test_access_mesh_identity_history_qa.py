"""Acceptance: a Mesh collision changes future identity, never historical authors."""

import sqlite3
from datetime import timedelta

import httpx
import pytest
from test_access_mesh_replication_http import T0, Routes, actor, node

from terminal_mcp.application.access_mesh_messages import AccessMeshMessaging
from terminal_mcp.core.managed_sessions import ManagedOperation
from terminal_mcp.core.tasks import TaskCoordinator
from terminal_mcp.storage.agents import AgentStore


@pytest.mark.asyncio
async def test_collision_preserves_task_claim_comment_message_and_command_history(tmp_path):
    routes = Routes()
    async with httpx.AsyncClient(transport=routes) as client:
        first, first_rep = await node(
            tmp_path,
            "firstbyte",
            "bacloud",
            routes,
            client,
        )
        second, second_rep = await node(
            tmp_path,
            "bacloud",
            "firstbyte",
            routes,
            client,
        )
        # Issuances happened on opposite sides of a partition. The later
        # issue (bacloud) wins when peers reconcile their numbers.
        second.clock = lambda: T0 + timedelta(seconds=10)
        second.store.clock = second.clock
        first_claim = await first.issue(actor(), code="0042")
        second_claim = await second.issue(actor(), code="0042")
        old_id, winner_id = (
            first_claim["logical_agent_id"],
            second_claim["logical_agent_id"],
        )
        assert old_id != winner_id
        assert (await first.attach(actor(), session_number="0042")) == {"ok": True}
        assert (await second.attach(actor(), session_number="0042")) == {"ok": True}
        old_bound = await first.resolve(actor(), ManagedOperation.COMMAND_RUN)
        latest_bound = await second.resolve(actor(), ManagedOperation.COMMAND_RUN)
        assert old_bound["logical_agent_id"] == old_id
        assert latest_bound["logical_agent_id"] == winner_id

        tasks = TaskCoordinator(first.task_store)
        created = await tasks.mutate(
            old_id,
            action="create",
            namespace="mesh-history",
            task_id="historical-work",
            title="Before Mesh convergence",
            description="Immutable history",
            isolation_hint="none",
        )
        assert created["ok"], created
        claimed = await tasks.mutate(
            old_id,
            action="claim",
            namespace="mesh-history",
            task_id="historical-work",
            claim_intent="before convergence",
        )
        assert claimed["ok"], claimed
        comment = await tasks.mutate(
            old_id,
            action="comment",
            namespace="mesh-history",
            task_id="historical-work",
            comment_text="Original author",
        )
        assert comment["ok"], comment

        # Model a previously accepted distributed message. Its sender is the
        # pre-collision LogicalAgent; no currently connected recipient is needed
        # to assert audit records are never migrated to the winner.
        messages = AccessMeshMessaging(first, AgentStore(first.store.path))
        first.messages = messages
        message_hash = "firstbyte:meshmsg:historic-accepted-before-merge"
        committed = messages.store.accept(
            {
                "message_hash": message_hash,
                "origin_node_id": "firstbyte",
                "created_at": T0.isoformat(),
                "sender_id": old_id,
                "sender_name": first_claim["public_name"],
                "text": "before convergence",
                "target": "broadcast",
                "mode": "notify",
                "scope": "local",
                "require_reply": False,
                "alert": False,
                "namespace": None,
                "task_id": None,
                "reply_to": None,
            },
            [],
        )
        assert committed["ok"], committed

        with sqlite3.connect(first.store.path) as db:
            db.execute(
                "INSERT INTO commands(hash,cmd,status,started_at) VALUES(?,?,?,?)",
                ("historical-hash", "true", "finished", T0.isoformat()),
            )
            db.execute(
                "INSERT INTO command_agent_attribution"
                "(command_hash,agent_id,created_at,command_type,command_preview,"
                "logical_agent_id,work_session_id,session_epoch) VALUES(?,?,?,?,?,?,?,?)",
                (
                    "historical-hash",
                    old_id,
                    T0.isoformat(),
                    "shell",
                    "true",
                    old_id,
                    old_bound["work_session_id"],
                    old_bound["session_epoch"],
                ),
            )
            before_task = [
                tuple(row)
                for row in db.execute(
                    "SELECT event_type,agent_id,logical_agent_id,payload_json "
                    "FROM work_events WHERE namespace='mesh-history' ORDER BY id"
                )
            ]
            before_claim = [
                tuple(row)
                for row in db.execute(
                    "SELECT owner_id,agent_id,claim_intent FROM work_claims "
                    "WHERE namespace='mesh-history'"
                )
            ]
            before_message = [
                tuple(row)
                for row in db.execute(
                    "SELECT message_hash,sender_agent_id,text "
                    "FROM coordination_messages WHERE message_hash=?",
                    (message_hash,),
                )
            ]
            before_command = db.execute(
                "SELECT agent_id,logical_agent_id,work_session_id "
                "FROM command_agent_attribution WHERE command_hash='historical-hash'"
            ).fetchone()

        # Reconnect and converge. Existing attachment must now use the later
        # LogicalAgent; stored audit events remain exactly as they were.
        first.clock = second.clock = lambda: T0 + timedelta(seconds=20)
        first.store.clock = second.store.clock = first.clock
        await first_rep.tick()
        await second_rep.tick()
        now = await first.resolve(actor(), ManagedOperation.COMMAND_RUN)
        assert now["logical_agent_id"] == winner_id
        assert (
            first.store.attached_slot(first.connection_key(actor())).logical_agent_id == winner_id
        )
        assert (
            second.store.attached_slot(second.connection_key(actor())).logical_agent_id == winner_id
        )

        next_comment = await tasks.mutate(
            now["logical_agent_id"],
            action="comment",
            namespace="mesh-history",
            task_id="historical-work",
            comment_text="After convergence",
        )
        assert next_comment["ok"], next_comment
        with sqlite3.connect(first.store.path) as db:
            old_events = db.execute(
                "SELECT event_type,agent_id,logical_agent_id,payload_json "
                "FROM work_events WHERE namespace='mesh-history' ORDER BY id"
            ).fetchall()
            assert [tuple(row) for row in old_events[: len(before_task)]] == before_task
            assert old_events[-1][1] == winner_id
            assert [
                tuple(row)
                for row in db.execute(
                    "SELECT owner_id,agent_id,claim_intent FROM work_claims "
                    "WHERE namespace='mesh-history'"
                )
            ] == before_claim
            assert [
                tuple(row)
                for row in db.execute(
                    "SELECT message_hash,sender_agent_id,text "
                    "FROM coordination_messages WHERE message_hash=?",
                    (message_hash,),
                )
            ] == before_message
            assert (
                db.execute(
                    "SELECT agent_id,logical_agent_id,work_session_id "
                    "FROM command_agent_attribution WHERE command_hash='historical-hash'"
                ).fetchone()
                == before_command
            )
