"""Sender receipt truth on isolated local databases; no production mutations."""

from datetime import timedelta

import pytest
from test_access_mesh_messages_v2 import _public_name, actor
from test_access_mesh_messages_v2 import mesh_pair as mesh_pair_fixture

mesh_pair = mesh_pair_fixture


def item(result, message_hash):
    return next(m for m in result["messages"] if m["message_hash"] == message_hash)


@pytest.mark.asyncio
async def test_sender_history_reflects_recipient_seen_ack_and_reply(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    sent = await fb.message(
        actor("alice"),
        target=_public_name(fb.mesh.store.slots["carol"]),
        text="receipt regression",
        mode="ack",
    )
    h = sent["message_hash"]
    before = item(await fb.message(actor("alice"), history=True, detail="full"), h)
    assert before["first_seen_at"] is None
    await fb.message(actor("carol"), detail="full")
    seen = fb.store.recipient(h, "carol")
    sender_seen = item(await fb.message(actor("alice"), history=True, detail="full"), h)
    assert sender_seen["first_seen_at"] == seen["first_seen_at"]
    assert sender_seen["seen_count"] == seen["seen_count"] == 1
    fb.mesh.now += timedelta(seconds=1)
    await fb.message(actor("carol"), message_hash=h)
    acked = fb.store.recipient(h, "carol")
    sender_acked = item(await fb.message(actor("alice"), history=True, detail="full"), h)
    assert sender_acked["read_at"] == acked["read_at"]
    fb.mesh.now += timedelta(seconds=1)
    reply = await fb.message(actor("carol"), message_hash=h, text="receipt reply")
    sender_replied = item(await fb.message(actor("alice"), history=True, detail="full"), h)
    assert sender_replied["state"] == "replied"
    assert sender_replied["reply_message_hash"] == reply["message_hash"]
    assert sender_replied["replied_at"] == fb.store.recipient(h, "carol")["replied_at"]


@pytest.mark.asyncio
async def test_sender_history_never_surfaces_recipient_or_repeats_seen(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    sent = await fb.message(actor("alice"), text="read only outgoing", mode="ack")
    h = sent["message_hash"]
    before = fb.store.recipient(h, "carol")
    for detail in ("summary", "full"):
        await fb.message(actor("alice"), history=True, detail=detail)
    assert fb.store.recipient(h, "carol") == before
    await fb.message(actor("carol"))
    first = fb.store.recipient(h, "carol")
    fb.mesh.now += timedelta(seconds=2)
    await fb.message(actor("carol"))
    second = fb.store.recipient(h, "carol")
    assert second["first_seen_at"] == first["first_seen_at"]
    assert second["seen_count"] == first["seen_count"] + 1
    for _ in range(2):
        history = await fb.message(actor("alice"), history=True, detail="full")
        assert item(history, h)["seen_count"] == second["seen_count"]
    assert fb.store.recipient(h, "carol") == second


@pytest.mark.asyncio
async def test_broadcast_receipts_do_not_turn_one_reader_into_all_readers(mesh_pair):
    node = mesh_pair.nodes["bacloud"]
    sent = await node.message(actor("bob"), text="broadcast receipt truth", mode="ack")
    h = sent["message_hash"]
    assert len(sent["delivered_to"]) == 2
    await node.message(actor("alice"))
    await node.message(actor("alice"), message_hash=h)
    partial = item(await node.message(actor("bob"), history=True, detail="full"), h)
    assert partial["state"] == "delivered"
    assert partial["first_seen_at"] is None and partial["read_at"] is None
    receipts = {r["public_name"]: r for r in partial["deliveries"]}
    alice = receipts[_public_name(node.mesh.store.slots["alice"])]
    carol = receipts[_public_name(node.mesh.store.slots["carol"])]
    assert alice["first_seen_at"] and alice["read_at"]
    assert carol["first_seen_at"] is None and carol["read_at"] is None
    node.mesh.now += timedelta(seconds=2)
    await node.message(actor("carol"))
    await node.message(actor("carol"), message_hash=h)
    complete = item(await node.message(actor("bob"), history=True, detail="full"), h)
    assert complete["state"] == "read"
    assert complete["read_at"] == node.store.recipient(h, "carol")["read_at"]
    assert complete["reply_message_hash"] is None
    # A broadcast stays a single cursor item, regardless of recipient count.
    assert (
        len(
            [
                m
                for m in (await node.message(actor("bob"), history=True))["messages"]
                if m["message_hash"] == h
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_recipient_history_surfaces_only_returned_cursor_page(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    hashes = [
        (await fb.message(actor("alice"), text=f"page {i}", mode="ack"))["message_hash"]
        for i in range(3)
    ]
    first = await fb.message(actor("carol"), history=True, detail="full", limit=1)
    first_hash = first["messages"][0]["message_hash"]
    assert first["next_cursor"]
    assert first["messages"][0]["first_seen_at"] is not None
    for h in hashes:
        r = fb.store.recipient(h, "carol")
        assert (r["first_seen_at"] is not None) == (h == first_hash)
        assert r["read_at"] is None  # Showing ACK-mode never grants ACK.
    second = await fb.message(
        actor("carol"), history=True, detail="full", limit=1, cursor=first["next_cursor"]
    )
    assert second["messages"][0]["message_hash"] != first_hash
    unseen = set(hashes) - {first_hash, second["messages"][0]["message_hash"]}
    assert all(fb.store.recipient(h, "carol")["first_seen_at"] is None for h in unseen)


@pytest.mark.asyncio
async def test_history_preflight_failure_cannot_mark_incoming_seen(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    h = (await fb.message(actor("alice"), text="reject outgoing response", mode="ack"))[
        "message_hash"
    ]
    before = fb.store.recipient(h, "carol")
    failure = {"ok": False, "code": "rejected_by_test"}
    result = await fb.message(
        actor("carol"), history=True, detail="full", response_preflight=lambda result: failure
    )
    assert result == failure
    assert fb.store.recipient(h, "carol") == before


@pytest.mark.asyncio
async def test_sender_summary_and_strict_full_wire_preserve_truth(mesh_pair):
    from terminal_mcp.mcp.output_contracts import MessageRecord, _message_record

    fb = mesh_pair.nodes["firstbyte"]
    h = (await fb.message(actor("alice"), text="wire proof", mode="alert"))["message_hash"]
    await fb.message(actor("carol"))
    summary = item(await fb.message(actor("alice"), history=True), h)
    assert summary["state"] == "seen"
    assert summary["acknowledged"] is False and summary["replied"] is False
    await fb.message(actor("carol"), message_hash=h)
    summary = item(await fb.message(actor("alice"), history=True), h)
    assert summary["acknowledged"] is True and summary["replied"] is False
    reply = await fb.message(actor("carol"), message_hash=h, text="wire reply")
    raw = item(await fb.message(actor("alice"), history=True, detail="full"), h)
    wire = _message_record(raw)
    MessageRecord.model_validate(wire)
    assert wire["deliveries"][0]["reply_message_hash"] == reply["message_hash"]
    assert wire["deliveries"][0]["first_seen_at"] == raw["first_seen_at"]
    assert "recipient_agent_id" not in str(wire) and "logical_agent_id" not in str(wire)


@pytest.mark.asyncio
async def test_restart_preserves_sender_receipts_and_ttl_boundary(mesh_pair):
    from terminal_mcp.storage.access_mesh_messages import AccessMeshMessageStore

    fb = mesh_pair.nodes["firstbyte"]
    h = (await fb.message(actor("alice"), text="durable receipt", mode="ack"))["message_hash"]
    await fb.message(actor("carol"))
    await fb.message(actor("carol"), message_hash=h)
    before = item(await fb.message(actor("alice"), history=True, detail="full"), h)
    fb.store = AccessMeshMessageStore(fb.store.path, fb.node_id, now_provider=fb.mesh.clock)
    after = item(await fb.message(actor("alice"), history=True, detail="full"), h)
    assert after == before
    fb.mesh.now += timedelta(hours=24)
    assert fb.store.page("alice", history=True) == []


@pytest.mark.asyncio
async def test_outgoing_history_cursor_never_duplicates_broadcasts(mesh_pair):
    node = mesh_pair.nodes["bacloud"]
    hashes = [
        (await node.message(actor("bob"), text=f"broadcast {i}", mode="ack"))["message_hash"]
        for i in range(5)
    ]
    await node.message(actor("alice"))
    cursor = None
    got = []
    while True:
        page = await node.message(actor("bob"), history=True, detail="full", limit=2, cursor=cursor)
        got.extend(m["message_hash"] for m in page["messages"])
        assert all(len(m["deliveries"]) == 2 for m in page["messages"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(got) == len(set(got)) == len(hashes)
    assert set(got) == set(hashes)
    assert all(node.store.recipient(h, "carol")["first_seen_at"] is None for h in hashes)


@pytest.mark.asyncio
async def test_recipient_history_does_not_downgrade_notify_reply(mesh_pair):
    fb = mesh_pair.nodes["firstbyte"]
    h = (await fb.message(actor("alice"), text="already replied notify"))["message_hash"]
    reply = await fb.message(actor("carol"), message_hash=h, text="explicit reply")
    for _ in range(2):
        row = item(await fb.message(actor("carol"), history=True, detail="full"), h)
        assert row["state"] == "replied"
        assert row["reply_message_hash"] == reply["message_hash"]
        assert row["replied_at"] == fb.store.recipient(h, "carol")["replied_at"]


@pytest.mark.asyncio
async def test_recipient_projection_never_leaks_other_recipients(mesh_pair):
    node = mesh_pair.nodes["bacloud"]
    h = (await node.message(actor("bob"), text="private recipient facts", mode="ack"))[
        "message_hash"
    ]
    await node.message(actor("alice"))
    incoming = item(await node.message(actor("carol"), history=True, detail="full"), h)
    assert "deliveries" not in incoming
    assert incoming["read_at"] is None
    outgoing = item(await node.message(actor("bob"), history=True, detail="full"), h)
    assert len(outgoing["deliveries"]) == 2
