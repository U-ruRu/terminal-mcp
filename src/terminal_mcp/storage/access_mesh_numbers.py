"""Durable four-digit Mesh session number reservations and reconciliation."""
from datetime import UTC, datetime, timedelta
import re
import secrets
from terminal_mcp.core.access_mesh_grants import AccessMeshError

class MeshSessionNumbers:
    """Stores temporary reservations and immutable session identities."""
    def __init__(self, store):
        self.store = store
        with store._connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS access_mesh_number_reservations(number TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, expires_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS access_mesh_number_claims(issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL, number TEXT NOT NULL, logical_agent_id TEXT NOT NULL, started_at TEXT NOT NULL, hard_expires_at TEXT NOT NULL, PRIMARY KEY(issuer_id,slot_id))')
            db.execute('CREATE INDEX IF NOT EXISTS access_mesh_number_current ON access_mesh_number_claims(number,started_at,issuer_id,slot_id)')
            db.execute('CREATE TABLE IF NOT EXISTS access_mesh_number_incidents(number TEXT NOT NULL, issuer_id TEXT NOT NULL, slot_id TEXT NOT NULL, collided_with TEXT NOT NULL, detected_at TEXT NOT NULL, PRIMARY KEY(number,issuer_id,slot_id,collided_with))')

    @staticmethod
    def validate(number):
        if not isinstance(number, str) or not re.fullmatch(r'[0-9]{4}', number):
            raise AccessMeshError('invalid_session_number')
        return number

    @staticmethod
    def stamp(value):
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise AccessMeshError('invalid_session_timestamp')
        return value.astimezone(UTC).isoformat(timespec='microseconds')

    def _occupied(self, db, number, attempt_id):
        # Legacy grants store per-issuer HMAC tags; no plaintext scan is needed.
        for issuer in self.store.trusted_issuers:
            tag = self.store.code_tag(issuer, number)
            if db.execute("SELECT 1 FROM access_mesh_slot_replicas WHERE issuer_id=? AND code_tag=? AND state!='deleted' LIMIT 1", (issuer, tag)).fetchone():
                return True
        if db.execute('SELECT 1 FROM access_mesh_number_claims WHERE number=? LIMIT 1', (number,)).fetchone():
            return True
        old = db.execute('SELECT attempt_id FROM access_mesh_number_reservations WHERE number=?', (number,)).fetchone()
        return old is not None and old['attempt_id'] != attempt_id

    def reserve(self, *, number, attempt_id, now=None):
        self.validate(number)
        if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 128:
            raise AccessMeshError('invalid_session_attempt')
        now = now or datetime.now(UTC)
        stamp = self.stamp(now)
        expires = self.stamp(now + timedelta(minutes=3))
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM access_mesh_number_reservations WHERE expires_at<=?', (stamp,))
            if not self._occupied(db, number, attempt_id):
                db.execute('INSERT INTO access_mesh_number_reservations VALUES(?,?,?) ON CONFLICT(number) DO UPDATE SET expires_at=excluded.expires_at WHERE attempt_id=excluded.attempt_id', (number, attempt_id, expires))
                return {'ok': True, 'number': number}
            for value in secrets.SystemRandom().sample(range(10000), 10000):
                alternative = f'{value:04d}'
                if not self._occupied(db, alternative, attempt_id):
                    db.execute('INSERT INTO access_mesh_number_reservations VALUES(?,?,?) ON CONFLICT(number) DO UPDATE SET expires_at=excluded.expires_at WHERE attempt_id=excluded.attempt_id', (alternative, attempt_id, expires))
                    return {'ok': False, 'code': 'number_conflict', 'suggested_number': alternative}
        raise AccessMeshError('session_number_capacity')

    def release(self, attempt_id, *, keep_number=None):
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if keep_number is None:
                db.execute('DELETE FROM access_mesh_number_reservations WHERE attempt_id=?', (attempt_id,))
            else:
                self.validate(keep_number)
                db.execute('DELETE FROM access_mesh_number_reservations WHERE attempt_id=? AND number!=?', (attempt_id, keep_number))

    def register(self, *, number, issuer_id, slot_id, logical_agent_id,
                 started_at, hard_expires_at, attempt_id=None, now=None):
        """Merge immutable identities; later-started session wins future operations."""
        self.validate(number)
        if issuer_id not in self.store.trusted_issuers:
            raise AccessMeshError('access_mesh_untrusted_peer')
        if not isinstance(slot_id, str) or not slot_id or not isinstance(logical_agent_id, str) or not logical_agent_id:
            raise AccessMeshError('invalid_session_identity')
        start = self.stamp(datetime.fromisoformat(started_at) if isinstance(started_at, str) else started_at)
        end = self.stamp(datetime.fromisoformat(hard_expires_at) if isinstance(hard_expires_at, str) else hard_expires_at)
        if end < start:
            raise AccessMeshError('invalid_session_timestamp')
        stamp = self.stamp(now or datetime.now(UTC))
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM access_mesh_number_claims WHERE issuer_id=? AND slot_id=?', (issuer_id, slot_id)).fetchone()
            if existing and (existing['number'], existing['logical_agent_id'], existing['started_at']) != (number, logical_agent_id, start):
                raise AccessMeshError('session_identity_conflict')
            db.execute('INSERT INTO access_mesh_number_claims VALUES(?,?,?,?,?,?) ON CONFLICT(issuer_id,slot_id) DO UPDATE SET hard_expires_at=MAX(hard_expires_at,excluded.hard_expires_at)', (issuer_id, slot_id, number, logical_agent_id, start, end))
            if attempt_id:
                db.execute('DELETE FROM access_mesh_number_reservations WHERE attempt_id=?', (attempt_id,))
            others = db.execute('SELECT issuer_id,slot_id FROM access_mesh_number_claims WHERE number=? AND (issuer_id!=? OR slot_id!=?)', (number, issuer_id, slot_id)).fetchall()
            for other in others:
                db.execute('INSERT OR IGNORE INTO access_mesh_number_incidents VALUES(?,?,?,?,?)', (number, issuer_id, slot_id, f"{other['issuer_id']}:{other['slot_id']}", stamp))
            winner = db.execute('SELECT logical_agent_id FROM access_mesh_number_claims WHERE number=? ORDER BY started_at DESC,issuer_id DESC,slot_id DESC LIMIT 1', (number,)).fetchone()
            deadline = db.execute('SELECT MAX(hard_expires_at) FROM access_mesh_number_claims WHERE number=?', (number,)).fetchone()[0]
        return {'ok': True, 'logical_agent_id': winner['logical_agent_id'], 'hard_expires_at': deadline, 'collisions': len(others)}

    def winner(self, number):
        self.validate(number)
        with self.store._connect() as db:
            winner = db.execute('SELECT * FROM access_mesh_number_claims WHERE number=? ORDER BY started_at DESC,issuer_id DESC,slot_id DESC LIMIT 1', (number,)).fetchone()
            if not winner:
                return None
            deadline = db.execute('SELECT MAX(hard_expires_at) FROM access_mesh_number_claims WHERE number=?', (number,)).fetchone()[0]
        return {**dict(winner), 'hard_expires_at': deadline}

    def snapshot(self):
        with self.store._connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM access_mesh_number_claims ORDER BY issuer_id,slot_id').fetchall()]

    def reservations(self, *, now=None):
        stamp = self.stamp(now or datetime.now(UTC))
        with self.store._connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM access_mesh_number_reservations WHERE expires_at>?', (stamp,)).fetchall()]

    def incidents(self):
        with self.store._connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM access_mesh_number_incidents ORDER BY number,issuer_id,slot_id').fetchall()]

    def number_for_slot(self, issuer_id, slot_id):
        with self.store._connect() as db:
            row = db.execute('SELECT number FROM access_mesh_number_claims WHERE issuer_id=? AND slot_id=?', (issuer_id, slot_id)).fetchone()
            return row['number'] if row else None

    def merge_reservations(self, rows, *, now=None):
        """Best-effort anti-entropy for unexpired peer reservations."""
        if not isinstance(rows, list) or len(rows) > 50:
            raise AccessMeshError('invalid_session_snapshot')
        stamp = self.stamp(now or datetime.now(UTC))
        validated = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'number', 'attempt_id', 'expires_at'}:
                raise AccessMeshError('invalid_session_snapshot')
            number = self.validate(row['number'])
            attempt = row['attempt_id']
            if not isinstance(attempt, str) or not 1 <= len(attempt) <= 128:
                raise AccessMeshError('invalid_session_snapshot')
            expires = self.stamp(datetime.fromisoformat(row['expires_at']))
            if expires > stamp:
                validated.append((number, attempt, expires))
        with self.store._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM access_mesh_number_reservations WHERE expires_at<=?', (stamp,))
            for number, attempt, expires in validated:
                if not self._occupied(db, number, attempt):
                    db.execute('INSERT OR IGNORE INTO access_mesh_number_reservations VALUES(?,?,?)', (number, attempt, expires))
