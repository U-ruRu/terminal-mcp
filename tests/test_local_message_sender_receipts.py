"""Sender progress is a projection of committed recipient facts."""

from terminal_mcp.storage.access_mesh_messages import AccessMeshMessageStore


def receipt(**changes):
    return {
        "first_seen_at": None,
        "last_seen_at": None,
        "seen_count": 0,
        "read_at": None,
        "replied_at": None,
        "reply_message_hash": None,
        **changes,
    }


def test_direct_sender_status_preserves_recipient_reply():
    replied = receipt(
        first_seen_at="2026-10-08T10:00:01Z",
        last_seen_at="2026-10-08T10:00:02Z",
        seen_count=2,
        read_at="2026-10-08T10:00:03Z",
        replied_at="2026-10-08T10:00:04Z",
        reply_message_hash="node-a:localmsg:reply",
    )

    assert AccessMeshMessageStore._sender_status([replied]) == replied


def test_broadcast_status_waits_for_every_recipient():
    seen = receipt(first_seen_at="2026-10-08T10:00:01Z", seen_count=1)

    status = AccessMeshMessageStore._sender_status([seen, receipt()])

    assert status["first_seen_at"] is None
    assert status["seen_count"] == 0


def test_broadcast_read_time_is_last_recipient_ack():
    first = receipt(read_at="2026-10-08T10:00:01Z")
    last = receipt(read_at="2026-10-08T10:00:05Z")

    assert AccessMeshMessageStore._sender_status([first, last])["read_at"] == last["read_at"]
