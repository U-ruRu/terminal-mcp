"""Durable four-digit Mesh session number reservations and reconciliation."""

import re
import secrets
from datetime import UTC, datetime, timedelta

from terminal_mcp.core.access_mesh_grants import AccessMeshError
from terminal_mcp.core.access_policy_windows import latest_number_event

# Durable Mesh schema revision independent of the core SqliteRepository schema.
# v2 preserves multiple Access.end events within a single original cycle.
MESH_NUMBERS_SCHEMA_VERSION = 3


class MeshSessionNumbers:
    """Stores temporary reservations and immutable session identities."""

    def __init__(self, store):
        self.store = store
        from terminal_mcp.storage.access_mesh_recipient_holds import initialize_holds

        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            initialize_holds(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_mesh_number_reservations(number TEXT "
                "PRIMARY KEY, attempt_id TEXT NOT NULL, expires_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_mesh_number_claims(issuer_id TEXT NOT "
                "NULL, slot_id TEXT NOT NULL, number TEXT NOT NULL, "
                "started_at TEXT NOT NULL, hard_expires_at TEXT NOT NULL, PRIMARY "
                "KEY(issuer_id,slot_id))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS access_mesh_number_current ON "
                "access_mesh_number_claims(number,started_at,issuer_id,slot_id)"
            )
            # The v27 session projection already owns the preserved identity.
            if "logical_agent_id" in {
                row[1] for row in db.execute("PRAGMA table_info(access_mesh_number_claims)")
            }:
                db.execute("ALTER TABLE access_mesh_number_claims DROP COLUMN logical_agent_id")
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_mesh_number_incidents(number TEXT NOT "
                "NULL, issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL, collided_with TEXT "
                "NOT NULL, detected_at TEXT NOT NULL, PRIMARY "
                "KEY(number,issuer_id,slot_id,collided_with))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_mesh_number_ends("
                "number TEXT NOT NULL, cycle_key TEXT NOT NULL, "
                "issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL, "
                "event_id TEXT NOT NULL, ended_at TEXT NOT NULL, "
                "revision INTEGER NOT NULL CHECK(revision>0), "
                "PRIMARY KEY(number,cycle_key,event_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS access_mesh_number_starts("
                "event_id TEXT PRIMARY KEY, number TEXT NOT NULL, "
                "issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL, "
                "active_from TEXT NOT NULL, started_at TEXT NOT NULL, "
                "hard_expires_at TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0))"
            )
            for table in ("access_mesh_number_starts", "access_mesh_number_ends"):
                if "revision" in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                    continue
                # Current v26 rows recover their issuer order from the durable
                # event journal. An incomplete peer announcement is re-fetched
                # by ordinary synchronization; no revision is invented for it.
                db.execute(
                    f"DELETE FROM {table} WHERE NOT EXISTS (SELECT 1 FROM access_mesh_event_log e "
                    f"WHERE e.issuer_id={table}.issuer_id AND e.slot_id={table}.slot_id "
                    f"AND e.event_id={table}.event_id)"
                )
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN revision "
                    "INTEGER NOT NULL DEFAULT 1 CHECK(revision>0)"
                )
                db.execute(
                    f"UPDATE {table} SET revision=(SELECT e.revision FROM access_mesh_event_log e "
                    f"WHERE e.issuer_id={table}.issuer_id AND e.slot_id={table}.slot_id "
                    f"AND e.event_id={table}.event_id)"
                )

    @staticmethod
    def validate(number):
        if not isinstance(number, str) or not re.fullmatch(r"[0-9]{4}", number):
            raise AccessMeshError("invalid_session_number")
        return number

    @staticmethod
    def stamp(value):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AccessMeshError("invalid_session_timestamp")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def _occupied(self, db, number, attempt_id, *, as_of=None):
        # A live slot owns its number for its whole lifetime, including idle,
        # expired and suspended windows. Physical deletion releases the claim.
        if db.execute(
            "SELECT 1 FROM access_mesh_recipient_holds WHERE number=? AND state='held' LIMIT 1",
            (number,),
        ).fetchone():
            return True
        if db.execute(
            "SELECT 1 FROM access_mesh_number_claims c JOIN access_mesh_slot_replicas s "
            "USING(issuer_id,slot_id) WHERE c.number=? LIMIT 1",
            (number,),
        ).fetchone():
            return True
        for issuer in self.store.registry_issuers():
            tags = self.store.code_tags(issuer, number)
            placeholders = ",".join("?" for _ in tags)
            if db.execute(
                "SELECT 1 FROM access_mesh_slot_replicas "
                f"WHERE issuer_id=? AND code_tag IN ({placeholders}) LIMIT 1",
                (issuer, *tags),
            ).fetchone():
                return True
        old = db.execute(
            "SELECT attempt_id FROM access_mesh_number_reservations WHERE number=?", (number,)
        ).fetchone()
        return old is not None and old["attempt_id"] != attempt_id

    def reserve(self, *, number, attempt_id, now=None):
        self.validate(number)
        if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 128:
            raise AccessMeshError("invalid_session_attempt")
        now = now or datetime.now(UTC)
        stamp = self.stamp(now)
        expires = self.stamp(now + timedelta(minutes=3))
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.assert_registry_ready(db)
            self.store.assert_registry_writer(db, self.store.local_node_id)
            db.execute("DELETE FROM access_mesh_number_reservations WHERE expires_at<=?", (stamp,))
            if not self._occupied(db, number, attempt_id, as_of=stamp):
                db.execute(
                    "INSERT INTO access_mesh_number_reservations VALUES(?,?,?) ON "
                    "CONFLICT(number) DO UPDATE SET "
                    "expires_at=MIN(expires_at,excluded.expires_at) "
                    "WHERE "
                    "attempt_id=excluded.attempt_id",
                    (number, attempt_id, expires),
                )
                return {"ok": True, "number": number}
            for value in secrets.SystemRandom().sample(range(10000), 10000):
                alternative = f"{value:04d}"
                if not self._occupied(db, alternative, attempt_id, as_of=stamp):
                    db.execute(
                        "INSERT INTO access_mesh_number_reservations VALUES(?,?,?) ON "
                        "CONFLICT(number) DO UPDATE SET "
                        "expires_at=MIN(expires_at,excluded.expires_at) "
                        "WHERE "
                        "attempt_id=excluded.attempt_id",
                        (alternative, attempt_id, expires),
                    )
                    return {"ok": False, "code": "number_conflict", "suggested_number": alternative}
        raise AccessMeshError("session_number_capacity")

    def release(self, attempt_id, *, keep_number=None):
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.assert_registry_ready(db)
            if keep_number is None:
                db.execute(
                    "DELETE FROM access_mesh_number_reservations WHERE attempt_id=?", (attempt_id,)
                )
            else:
                self.validate(keep_number)
                db.execute(
                    "DELETE FROM access_mesh_number_reservations WHERE attempt_id=? AND number!=?",
                    (attempt_id, keep_number),
                )

    def register(
        self,
        *,
        number,
        issuer_id,
        slot_id,
        started_at,
        hard_expires_at,
        attempt_id=None,
        now=None,
        source_peer_id=None,
    ):
        """Merge immutable identities; later-started session wins future operations."""
        self.validate(number)
        if not self.store.registry_issuer_trusted(issuer_id):
            raise AccessMeshError("access_mesh_untrusted_peer")
        if source_peer_id is not None and source_peer_id not in self.store.trusted_issuers:
            raise AccessMeshError("access_mesh_untrusted_peer")
        if not isinstance(slot_id, str) or not slot_id:
            raise AccessMeshError("invalid_session_identity")
        start = self.stamp(
            datetime.fromisoformat(started_at) if isinstance(started_at, str) else started_at
        )
        end = self.stamp(
            datetime.fromisoformat(hard_expires_at)
            if isinstance(hard_expires_at, str)
            else hard_expires_at
        )
        if end < start:
            raise AccessMeshError("invalid_session_timestamp")
        stamp = self.stamp(now or datetime.now(UTC))
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.assert_registry_ready(db)
            self.store.assert_registry_writer(db, source_peer_id or self.store.local_node_id)
            pinned = db.execute(
                "SELECT c.* FROM access_mesh_recipient_holds h "
                "JOIN access_mesh_number_claims c USING(issuer_id,slot_id) "
                "WHERE h.number=? AND h.state='held' AND "
                "(h.issuer_id!=? OR h.slot_id!=?) AND c.started_at<=? LIMIT 1",
                (number, issuer_id, slot_id, start),
            ).fetchone()
            if pinned is not None:
                raise AccessMeshError("access_mesh_subscription_hold")
            existing = db.execute(
                "SELECT * FROM access_mesh_number_claims WHERE issuer_id=? AND slot_id=?",
                (issuer_id, slot_id),
            ).fetchone()
            if existing and (existing["number"] != number or existing["started_at"] != start):
                raise AccessMeshError("session_identity_conflict")
            db.execute(
                "INSERT INTO access_mesh_number_claims VALUES(?,?,?,?,?) ON "
                "CONFLICT(issuer_id,slot_id) DO UPDATE SET "
                "hard_expires_at=MAX(hard_expires_at,excluded.hard_expires_at)",
                (issuer_id, slot_id, number, start, end),
            )
            if attempt_id:
                db.execute(
                    "DELETE FROM access_mesh_number_reservations WHERE attempt_id=?", (attempt_id,)
                )
            others = db.execute(
                "SELECT issuer_id,slot_id FROM access_mesh_number_claims "
                "WHERE number=? AND (issuer_id!=? OR slot_id!=?) "
                "AND started_at<=? AND hard_expires_at>=?",
                (number, issuer_id, slot_id, end, start),
            ).fetchall()
            for other in others:
                db.execute(
                    "INSERT OR IGNORE INTO access_mesh_number_incidents VALUES(?,?,?,?,?)",
                    (number, issuer_id, slot_id, f"{other['issuer_id']}:{other['slot_id']}", stamp),
                )
            claims = [
                dict(row)
                for row in db.execute(
                    "SELECT c.*,s.logical_agent_id FROM access_mesh_number_claims c "
                    "LEFT JOIN access_mesh_sessions s USING(issuer_id,slot_id) WHERE number=?",
                    (number,),
                ).fetchall()
            ]
            for group in self._groups(claims):
                if any(
                    row["issuer_id"] == issuer_id and row["slot_id"] == slot_id for row in group
                ):
                    chosen = self._group_winner(group)
                    break
        return {
            "ok": True,
            "logical_agent_id": chosen["logical_agent_id"],
            "hard_expires_at": chosen["hard_expires_at"],
            "collisions": len(others),
        }

    @staticmethod
    def _groups(rows):
        """Partition claims by overlapping work lifetimes.

        Reuse after expiry creates a fresh identity epoch, while partitioned
        overlapping grants form one LogicalAgent (including transitive chains).
        """
        groups = []
        current = []
        latest_end = ""
        for row in sorted(rows, key=lambda r: (r["started_at"], r["issuer_id"], r["slot_id"])):
            if current and row["started_at"] > latest_end:
                groups.append(current)
                current = []
                latest_end = ""
            current.append(row)
            latest_end = max(latest_end, row["hard_expires_at"])
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _group_winner(group):
        winner = max(group, key=lambda row: (row["started_at"], row["issuer_id"], row["slot_id"]))
        return {**dict(winner), "hard_expires_at": max(item["hard_expires_at"] for item in group)}

    def winner(self, number):
        """Current identity: latest time-overlap group for the four digits."""
        self.validate(number)
        with self.store._connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT c.*,s.logical_agent_id FROM access_mesh_number_claims c "
                    "LEFT JOIN access_mesh_sessions s USING(issuer_id,slot_id) WHERE number=?",
                    (number,),
                ).fetchall()
            ]
        groups = self._groups(rows)
        return self._group_winner(groups[-1]) if groups else None

    def group_winner(self, issuer_id, slot_id):
        """A historic slot maps only to claims overlapping its own lifetime."""
        with self.store._connect() as db:
            original = db.execute(
                "SELECT * FROM access_mesh_number_claims WHERE issuer_id=? AND slot_id=?",
                (issuer_id, slot_id),
            ).fetchone()
            if not original:
                return None
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT c.*,s.logical_agent_id FROM access_mesh_number_claims c "
                    "LEFT JOIN access_mesh_sessions s USING(issuer_id,slot_id) WHERE number=?",
                    (original["number"],),
                ).fetchall()
            ]
        for group in self._groups(rows):
            if any(row["issuer_id"] == issuer_id and row["slot_id"] == slot_id for row in group):
                return self._group_winner(group)
        return None

    def start_for_slot(self, issuer_id, slot_id):
        """Latest manual start of the overlapping historical identity group."""
        with self.store._connect() as db:
            original = db.execute(
                "SELECT * FROM access_mesh_number_claims WHERE issuer_id=? AND slot_id=?",
                (issuer_id, slot_id),
            ).fetchone()
            if original is None:
                return None
            claims = [
                dict(row)
                for row in db.execute(
                    "SELECT c.*,s.logical_agent_id FROM access_mesh_number_claims c "
                    "LEFT JOIN access_mesh_sessions s USING(issuer_id,slot_id) WHERE number=?",
                    (original["number"],),
                ).fetchall()
            ]
            group = next(
                (
                    group
                    for group in self._groups(claims)
                    if any(
                        row["issuer_id"] == issuer_id and row["slot_id"] == slot_id for row in group
                    )
                ),
                None,
            )
            if not group:
                return None
            keys = {(row["issuer_id"], row["slot_id"]) for row in group}
            events = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM access_mesh_number_starts WHERE number=?",
                    (original["number"],),
                )
                if (row["issuer_id"], row["slot_id"]) in keys
            ]
        return latest_number_event(events, "active_from")

    def end_for_slot(self, issuer_id, slot_id):
        """Latest explicit end of this identity group, excluding recycled epochs."""
        with self.store._connect() as db:
            original = db.execute(
                "SELECT number FROM access_mesh_number_claims WHERE issuer_id=? AND slot_id=?",
                (issuer_id, slot_id),
            ).fetchone()
            if original is None:
                return None
            claims = [
                dict(row)
                for row in db.execute(
                    "SELECT c.*,s.logical_agent_id FROM access_mesh_number_claims c "
                    "LEFT JOIN access_mesh_sessions s USING(issuer_id,slot_id) WHERE number=?",
                    (original["number"],),
                ).fetchall()
            ]
            group = next(
                (
                    group
                    for group in self._groups(claims)
                    if any(
                        row["issuer_id"] == issuer_id and row["slot_id"] == slot_id for row in group
                    )
                ),
                None,
            )
            if not group:
                return None
            keys = {(row["issuer_id"], row["slot_id"]) for row in group}
            ends = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM access_mesh_number_ends WHERE number=?",
                    (original["number"],),
                )
                if (row["issuer_id"], row["slot_id"]) in keys
            ]
        return latest_number_event(ends, "ended_at")

    def record_start(
        self,
        *,
        number,
        issuer_id,
        slot_id,
        event_id,
        revision,
        active_from,
        started_at,
        hard_expires_at,
        source_peer_id=None,
    ):
        self.validate(number)
        if not self.store.registry_issuer_trusted(issuer_id):
            raise AccessMeshError("access_mesh_untrusted_peer")
        if not slot_id or not event_id or type(revision) is not int or revision < 1:
            raise AccessMeshError("invalid_session_identity")
        values = [
            self.stamp(datetime.fromisoformat(value) if isinstance(value, str) else value)
            for value in (active_from, started_at, hard_expires_at)
        ]
        if values[2] < values[1]:
            raise AccessMeshError("invalid_session_timestamp")
        candidate = {
            "event_id": event_id,
            "number": number,
            "issuer_id": issuer_id,
            "slot_id": slot_id,
            "active_from": values[0],
            "started_at": values[1],
            "hard_expires_at": values[2],
            "revision": revision,
        }
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.assert_registry_ready(db)
            writer = source_peer_id or self.store.local_node_id
            self.store.assert_registry_writer(db, writer)
            existing = db.execute(
                "SELECT * FROM access_mesh_number_starts WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                changed = {field for field in candidate if existing[field] != candidate[field]}
                if changed:
                    if changed != {"active_from"} or not self._proves_start_activation(
                        db, candidate, writer
                    ):
                        raise AccessMeshError("idempotency_conflict")
                    db.execute(
                        "UPDATE access_mesh_number_starts SET active_from=? WHERE event_id=?",
                        (candidate["active_from"], event_id),
                    )
            else:
                db.execute(
                    "INSERT INTO access_mesh_number_starts VALUES(?,?,?,?,?,?,?,?)",
                    tuple(candidate.values()),
                )
        return {"ok": True}

    @staticmethod
    def _proves_start_activation(db, candidate, writer):
        """Rebuild a derived announcement only from accepted canonical data."""
        authority = db.execute(
            "SELECT authority_id FROM access_mesh_registry_scope WHERE singleton=1"
        ).fetchone()
        if authority is None or authority[0] != writer:
            return False
        key = tuple(candidate[name] for name in ("issuer_id", "slot_id", "event_id", "revision"))
        canonical = db.execute(
            "SELECT json_extract(event_json,'$.active_from'),"
            "json_extract(event_json,'$.effective_at'),"
            "json_extract(event_json,'$.deadline_at') FROM access_mesh_event_log "
            "WHERE issuer_id=? AND slot_id=? AND event_id=? AND revision=? "
            "AND json_extract(event_json,'$.kind')='SessionStarted'",
            key,
        ).fetchone()
        if canonical is None:
            # Snapshot catch-up can precede the event journal. Only the exact
            # accepted revision can prove this start, never a later window.
            canonical = db.execute(
                "SELECT session.active_from,slot.anchor,slot.deadline_at "
                "FROM access_mesh_slot_replicas slot JOIN access_mesh_sessions session "
                "USING(issuer_id,slot_id) WHERE slot.issuer_id=? AND slot.slot_id=? "
                "AND slot.event_id=? AND slot.revision=?",
                key,
            ).fetchone()
        return canonical is not None and tuple(canonical) == tuple(
            candidate[name] for name in ("active_from", "started_at", "hard_expires_at")
        )

    def start_snapshot(self):
        with self.store._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM access_mesh_number_starts ORDER BY issuer_id,slot_id,event_id"
                )
            ]

    def snapshot(self):
        with self.store._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM access_mesh_number_claims ORDER BY issuer_id,slot_id"
                ).fetchall()
            ]

    def reservations(self, *, now=None):
        stamp = self.stamp(now or datetime.now(UTC))
        with self.store._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM access_mesh_number_reservations WHERE expires_at>?", (stamp,)
                ).fetchall()
            ]

    def record_late_conflict(
        self, *, number: str, attempt_id: str, peer_id: str, suggested_number: str | None = None
    ):
        """Audit a peer conflict arriving after this attempt's decision.

        The attempt ID deliberately occupies the incident's slot column:
        no AccessSlot exists at reservation time and historical real session
        collision records must never be rewritten.
        """
        self.validate(number)
        if suggested_number is not None:
            self.validate(suggested_number)
        if peer_id not in self.store.trusted_issuers:
            raise AccessMeshError("access_mesh_untrusted_peer")
        if not isinstance(attempt_id, str) or not attempt_id.startswith("nr_"):
            raise AccessMeshError("invalid_session_attempt")
        marker = f"late_reservation:{peer_id}:{suggested_number or 'none'}"
        with self.store._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO access_mesh_number_incidents "
                "(number,issuer_id,slot_id,collided_with,detected_at) "
                "VALUES(?,?,?,?,?)",
                (
                    number,
                    self.store.local_node_id,
                    attempt_id,
                    marker,
                    self.stamp(datetime.now(UTC)),
                ),
            )
        return {"ok": True}

    def incidents(self):
        with self.store._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM access_mesh_number_incidents ORDER BY number,issuer_id,slot_id"
                ).fetchall()
            ]

    def incident_page(
        self, *, after: tuple[str, str, str, str] | None = None, limit: int = 25
    ) -> list[dict]:
        """Durable, keyset-paginated collision audit for new Mesh members."""
        if type(limit) is not int or not 1 <= limit <= 25:
            raise AccessMeshError("invalid_session_snapshot")
        if after is not None and (
            not isinstance(after, tuple)
            or len(after) != 4
            or not all(isinstance(part, str) for part in after)
        ):
            raise AccessMeshError("invalid_session_snapshot")
        with self.store._connect() as db:
            sql = (
                "SELECT * FROM access_mesh_number_incidents "
                + (
                    "WHERE (number,issuer_id,slot_id,collided_with) > (?,?,?,?) "
                    if after is not None
                    else ""
                )
                + "ORDER BY number,issuer_id,slot_id,collided_with LIMIT ?"
            )
            return [
                dict(row)
                for row in db.execute(
                    sql,
                    (*after, limit) if after is not None else (limit,),
                )
            ]

    def merge_incidents(self, rows: list[dict]) -> int:
        """Merge peer-discovered collisions without rewriting their authors."""
        if not isinstance(rows, list) or len(rows) > 25:
            raise AccessMeshError("invalid_session_snapshot")
        validated = []
        for item in rows:
            if not isinstance(item, dict) or set(item) != {
                "number",
                "issuer_id",
                "slot_id",
                "collided_with",
                "detected_at",
            }:
                raise AccessMeshError("invalid_session_snapshot")
            number = self.validate(item["number"])
            issuer = item["issuer_id"]
            if not self.store.registry_issuer_trusted(issuer):
                raise AccessMeshError("access_mesh_untrusted_peer")
            slot = item["slot_id"]
            collided = item["collided_with"]
            if (
                not isinstance(slot, str)
                or not 1 <= len(slot) <= 256
                or not isinstance(collided, str)
                or not 1 <= len(collided) <= 256
            ):
                raise AccessMeshError("invalid_session_snapshot")
            try:
                detected_at = self.stamp(datetime.fromisoformat(item["detected_at"]))
            except (TypeError, ValueError) as exc:
                raise AccessMeshError("invalid_session_snapshot") from exc
            validated.append((number, issuer, slot, collided, detected_at))
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.assert_registry_ready(db)
            before = db.total_changes
            db.executemany(
                "INSERT OR IGNORE INTO access_mesh_number_incidents "
                "(number,issuer_id,slot_id,collided_with,detected_at) "
                "VALUES(?,?,?,?,?)",
                validated,
            )
            return db.total_changes - before

    def number_for_slot(self, issuer_id, slot_id):
        with self.store._connect() as db:
            row = db.execute(
                "SELECT number FROM access_mesh_number_claims WHERE issuer_id=? AND slot_id=?",
                (issuer_id, slot_id),
            ).fetchone()
            return row["number"] if row else None

    def merge_reservations(self, rows, *, now=None):
        """Best-effort anti-entropy for unexpired peer reservations."""
        if not isinstance(rows, list) or len(rows) > 50:
            raise AccessMeshError("invalid_session_snapshot")
        stamp = self.stamp(now or datetime.now(UTC))
        validated = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"number", "attempt_id", "expires_at"}:
                raise AccessMeshError("invalid_session_snapshot")
            number = self.validate(row["number"])
            attempt = row["attempt_id"]
            if not isinstance(attempt, str) or not 1 <= len(attempt) <= 128:
                raise AccessMeshError("invalid_session_snapshot")
            expires = self.stamp(datetime.fromisoformat(row["expires_at"]))
            if expires > stamp:
                validated.append((number, attempt, expires))
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.assert_registry_ready(db)
            db.execute("DELETE FROM access_mesh_number_reservations WHERE expires_at<=?", (stamp,))
            for number, attempt, expires in validated:
                if not self._occupied(db, number, attempt, as_of=stamp):
                    db.execute(
                        "INSERT OR IGNORE INTO access_mesh_number_reservations VALUES(?,?,?)",
                        (number, attempt, expires),
                    )

    def record_end(
        self,
        *,
        number,
        cycle_key,
        issuer_id,
        slot_id,
        event_id,
        revision,
        ended_at,
        source_peer_id=None,
    ):
        self.validate(number)
        if not self.store.registry_issuer_trusted(issuer_id):
            raise AccessMeshError("access_mesh_untrusted_peer")
        if (
            type(revision) is not int
            or revision < 1
            or not all(
                isinstance(value, str) and 1 <= len(value) <= 256
                for value in (cycle_key, slot_id, event_id)
            )
        ):
            raise AccessMeshError("invalid_session_end")
        stamp = self.stamp(
            datetime.fromisoformat(ended_at) if isinstance(ended_at, str) else ended_at
        )
        with self.store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store.assert_registry_ready(db)
            self.store.assert_registry_writer(db, source_peer_id or self.store.local_node_id)
            candidate = (number, cycle_key, issuer_id, slot_id, event_id, stamp, revision)
            existing = db.execute(
                "SELECT * FROM access_mesh_number_ends WHERE number=? AND cycle_key=? AND "
                "event_id=?",
                (number, cycle_key, event_id),
            ).fetchone()
            if existing is not None and tuple(existing) != candidate:
                raise AccessMeshError("idempotency_conflict")
            db.execute(
                "INSERT OR IGNORE INTO access_mesh_number_ends VALUES(?,?,?,?,?,?,?)", candidate
            )
        return {"ok": True}


    def end_snapshot(self):
        with self.store._connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM access_mesh_number_ends ORDER BY number,cycle_key,event_id"
                ).fetchall()
            ]
