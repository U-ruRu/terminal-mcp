"""Durable message-recipient pins, independent of WorkSession authority.

Only the subscription's node can advance its hold or release tombstone.
No callback, credential, secret or message body is included in the wire state.
"""

from terminal_mcp.core.access_mesh_grants import AccessMeshError

HOLD_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS access_mesh_recipient_holds(
        source_node_id TEXT NOT NULL, hold_id TEXT NOT NULL,
        recipient_agent_id TEXT NOT NULL, issuer_id TEXT NOT NULL,
        slot_id TEXT NOT NULL, number TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('held','released')),
        revision INTEGER NOT NULL CHECK(revision>0),
        PRIMARY KEY(source_node_id,hold_id))""",
    """CREATE INDEX IF NOT EXISTS access_mesh_recipient_hold_number
        ON access_mesh_recipient_holds(number,state)""",
)
HOLD_FIELDS = {
    "source_node_id",
    "hold_id",
    "recipient_agent_id",
    "issuer_id",
    "slot_id",
    "number",
    "state",
    "revision",
}


def initialize_holds(db):
    for statement in HOLD_SCHEMA:
        db.execute(statement)


def slot_held(db, issuer_id, slot_id):
    return (
        db.execute(
            "SELECT 1 FROM access_mesh_recipient_holds "
            "WHERE issuer_id=? AND slot_id=? AND state='held' LIMIT 1",
            (issuer_id, slot_id),
        ).fetchone()
        is not None
    )


async def acquire_hold(db, node_id, sub_id):
    # Non-Mesh legacy recipients have no public-number claim to pin.
    exists = await (
        await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='access_mesh_number_claims'"
        )
    ).fetchone()
    if not exists:
        return
    row = await (
        await db.execute(
            "SELECT b.recipient_agent_id,b.recipient_issuer_id,b.recipient_slot_id,c.number "
            "FROM mcp_event_message_bindings b "
            "JOIN mcp_event_subscriptions s ON s.id=b.subscription_id "
            "JOIN access_mesh_number_claims c ON c.issuer_id=b.recipient_issuer_id "
            "AND c.slot_id=b.recipient_slot_id AND c.logical_agent_id=b.recipient_agent_id "
            "WHERE b.subscription_id=? AND s.name='message.received'",
            (sub_id,),
        )
    ).fetchone()
    if row is None:
        return
    old = await (
        await db.execute(
            "SELECT recipient_agent_id,issuer_id,slot_id,number "
            "FROM access_mesh_recipient_holds WHERE source_node_id=? AND hold_id=?",
            (node_id, sub_id),
        )
    ).fetchone()
    if old is not None and tuple(old) != tuple(row):
        raise AccessMeshError("subscription_recipient_identity_conflict")
    await db.execute(
        "INSERT INTO access_mesh_recipient_holds VALUES(?,?,?,?,?,?,'held',1) "
        "ON CONFLICT(source_node_id,hold_id) DO UPDATE SET state='held',"
        "revision=revision+CASE WHEN state='released' THEN 1 ELSE 0 END",
        (node_id, sub_id, *row),
    )


async def release_holds(db, node_id, selector, params):
    # Versioned tombstones survive restarts and prevent stale peer snapshots
    # from reacquiring a removed subscription. Removal is reference-counted
    # by distinct rows, never a destructive slot/number mutation.
    await db.execute(
        "UPDATE access_mesh_recipient_holds SET state='released',revision=revision+1 "
        "WHERE source_node_id=? AND state='held' AND hold_id IN (" + selector + ")",
        (node_id, *params),
    )


async def reconcile_holds(db, node_id):
    # Additive upgrade: preserve the exact stored association and callback,
    # expiration and signing configuration. Never start a fresh lease.
    exists = await (
        await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='access_mesh_number_claims'"
        )
    ).fetchone()
    if exists:
        rows = await (
            await db.execute(
                "SELECT b.subscription_id FROM mcp_event_message_bindings b "
                "JOIN mcp_event_subscriptions s ON s.id=b.subscription_id "
                "WHERE s.name='message.received'"
            )
        ).fetchall()
        for row in rows:
            await acquire_hold(db, node_id, row[0])


class MeshRecipientHolds:
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            initialize_holds(db)

    def local_recipients(self):
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT h.recipient_agent_id,h.issuer_id,h.slot_id,h.number "
                "FROM access_mesh_recipient_holds h "
                "JOIN access_mesh_slot_replicas s USING(issuer_id,slot_id) "
                "JOIN access_mesh_number_claims c USING(issuer_id,slot_id) "
                "WHERE (h.source_node_id=? OR EXISTS("
                "SELECT 1 FROM access_mesh_attachments a "
                "WHERE a.issuer_id=h.issuer_id AND a.slot_id=h.slot_id "
                "AND a.logical_agent_id=h.recipient_agent_id)) "
                "AND h.state='held' AND s.state!='deleted' "
                "AND s.logical_agent_id=h.recipient_agent_id "
                "AND c.logical_agent_id=h.recipient_agent_id AND c.number=h.number",
                (self.store.local_node_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def snapshot(self, *, after="", limit=25):
        if (
            not isinstance(after, str)
            or len(after) > 300
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise AccessMeshError("invalid_recipient_hold_cursor")
        with self.store._connect() as db:
            rows = db.execute(
                "SELECT * FROM access_mesh_recipient_holds "
                "WHERE source_node_id=? AND hold_id>? ORDER BY hold_id LIMIT ?",
                (self.store.local_node_id, after, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def merge(self, rows, *, authenticated_peer_id):
        if authenticated_peer_id not in self.store.trusted_issuers:
            raise AccessMeshError("access_mesh_untrusted_peer")
        if not isinstance(rows, list) or len(rows) > 50:
            raise AccessMeshError("invalid_recipient_hold_snapshot")
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or set(row) != HOLD_FIELDS
                    or row["source_node_id"] != authenticated_peer_id
                    or row["issuer_id"] not in self.store.trusted_issuers
                    or any(
                        not isinstance(row[k], str) or not 1 <= len(row[k]) <= 160
                        for k in ("hold_id", "recipient_agent_id", "slot_id")
                    )
                    or row["state"] not in {"held", "released"}
                    or type(row["revision"]) is not int
                    or row["revision"] < 1
                ):
                    raise AccessMeshError("invalid_recipient_hold_snapshot")
                self.store.numbers.validate(row["number"])
                old = db.execute(
                    "SELECT * FROM access_mesh_recipient_holds "
                    "WHERE source_node_id=? AND hold_id=?",
                    (authenticated_peer_id, row["hold_id"]),
                ).fetchone()
                if old is not None:
                    if any(old[k] != row[k] for k in HOLD_FIELDS - {"state", "revision"}):
                        raise AccessMeshError("subscription_recipient_identity_conflict")
                    if old["revision"] > row["revision"]:
                        continue
                    if old["revision"] == row["revision"]:
                        if old["state"] != row["state"]:
                            raise AccessMeshError("recipient_hold_revision_conflict")
                        continue
                db.execute(
                    "INSERT INTO access_mesh_recipient_holds VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source_node_id,hold_id) DO UPDATE SET "
                    "state=excluded.state,revision=excluded.revision",
                    tuple(
                        row[k]
                        for k in (
                            "source_node_id",
                            "hold_id",
                            "recipient_agent_id",
                            "issuer_id",
                            "slot_id",
                            "number",
                            "state",
                            "revision",
                        )
                    ),
                )
