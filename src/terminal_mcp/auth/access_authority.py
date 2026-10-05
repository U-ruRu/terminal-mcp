from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable

from terminal_mcp.auth.access_key import AccessVerifierKeyStore
from terminal_mcp.auth.foundation import (
    AuthConflictError,
    AuthFoundationError,
    AuthNotFoundError,
    _utc_now,
)

_ACCESS_DIGITS = "0123456789"
_NATO = (
    "Alpha",
    "Bravo",
    "Charlie",
    "Delta",
    "Echo",
    "Foxtrot",
    "Golf",
    "Hotel",
    "India",
    "Juliett",
    "Kilo",
    "Lima",
    "Mike",
    "November",
    "Oscar",
    "Papa",
    "Quebec",
    "Romeo",
    "Sierra",
    "Tango",
    "Uniform",
    "Victor",
    "Whiskey",
    "X-ray",
    "Yankee",
    "Zulu",
)


def normalize_access_code(value: str) -> str:
    code = (value or "").strip()
    if len(code) != 4 or any(ch not in _ACCESS_DIGITS for ch in code):
        raise ValueError("access code must be exactly 4 decimal digits")
    return code


class AccessCodeAuthority:
    """Rollback-excluded authority for slot identities and short access credentials.

    Raw codes are accepted only at the call boundary. Durable state contains two
    domain-separated keyed digests and tombstones, never the plaintext code.
    """

    def __init__(self, foundation, key_path):
        self.foundation = foundation
        self.key_store = AccessVerifierKeyStore(key_path)
        self._key: bytes | None = None

    def _require_key(self) -> bytes:
        if self._key is None:
            raise AuthFoundationError("access code secret is unavailable")
        return self._key

    def _digest(self, domain: bytes, code: str) -> str:
        key = self._require_key()
        return hmac.new(key, domain + b"\x00" + code.encode("ascii"), hashlib.sha256).hexdigest()

    def _index(self, code: str) -> str:
        return self._digest(b"index", code)

    def _verifier(self, code: str) -> str:
        return self._digest(b"verify", code)

    @staticmethod
    def _public_name(ordinal: int) -> str:
        if ordinal < 1:
            raise ValueError("ordinal must be positive")
        word = _NATO[(ordinal - 1) % len(_NATO)]
        cycle = (ordinal - 1) // len(_NATO) + 1
        return word if cycle == 1 else f"{word}-{cycle}"

    async def initialize(self, db) -> None:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS auth_access_slots(
                logical_agent_id TEXT PRIMARY KEY,
                public_name TEXT NOT NULL,
                slot_kind TEXT NOT NULL CHECK(slot_kind IN ('persistent','legacy')),
                display_suffix TEXT,
                authority_node_id TEXT NOT NULL,
                access_generation INTEGER NOT NULL DEFAULT 0 CHECK(access_generation >= 0),
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                deleted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS auth_access_codes(
                code_index TEXT PRIMARY KEY,
                logical_agent_id TEXT NOT NULL,
                generation INTEGER NOT NULL CHECK(generation > 0),
                verifier TEXT NOT NULL,
                created_at TEXT NOT NULL,
                retired_at TEXT,
                tombstoned_at TEXT,
                UNIQUE(logical_agent_id,generation),
                FOREIGN KEY(logical_agent_id)
                    REFERENCES auth_access_slots(logical_agent_id) ON DELETE RESTRICT
            );
            CREATE TABLE IF NOT EXISTS auth_access_code_tombstones(
                code_index TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_auth_access_codes_slot
                ON auth_access_codes(logical_agent_id,generation);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_auth_access_slots_live_name
                ON auth_access_slots(public_name) WHERE status='active';
            CREATE INDEX IF NOT EXISTS ix_auth_access_slots_authority
                ON auth_access_slots(authority_node_id,status,public_name);
            """
        )
        await self._migrate_live_name_uniqueness(db)
        state_count = int(
            (
                await (
                    await db.execute(
                        "SELECT (SELECT COUNT(*) FROM auth_access_slots) + "
                        "(SELECT COUNT(*) FROM auth_access_codes) + "
                        "(SELECT COUNT(*) FROM auth_access_code_tombstones)"
                    )
                ).fetchone()
            )[0]
        )
        self._key = self.key_store.load_or_create(allow_create=state_count == 0)

    async def _migrate_live_name_uniqueness(self, db) -> None:
        row = await (
            await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='auth_access_slots'"
            )
        ).fetchone()
        sql = (row[0] or "") if row else ""
        if "public_name TEXT NOT NULL UNIQUE" not in sql:
            return
        await db.execute("PRAGMA foreign_keys=OFF")
        try:
            await db.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TABLE IF EXISTS auth_access_slots_v3;
                CREATE TABLE auth_access_slots_v3(
                    logical_agent_id TEXT PRIMARY KEY,
                    public_name TEXT NOT NULL,
                    slot_kind TEXT NOT NULL CHECK(slot_kind IN ('persistent','legacy')),
                    display_suffix TEXT,
                    authority_node_id TEXT NOT NULL,
                    access_generation INTEGER NOT NULL DEFAULT 0 CHECK(access_generation >= 0),
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','deleted')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                INSERT INTO auth_access_slots_v3(
                    logical_agent_id,public_name,slot_kind,display_suffix,authority_node_id,
                    access_generation,status,created_at,updated_at,deleted_at
                )
                SELECT logical_agent_id,public_name,slot_kind,display_suffix,authority_node_id,
                       access_generation,status,created_at,updated_at,deleted_at
                FROM auth_access_slots;
                DROP TABLE auth_access_slots;
                ALTER TABLE auth_access_slots_v3 RENAME TO auth_access_slots;
                CREATE UNIQUE INDEX ux_auth_access_slots_live_name
                    ON auth_access_slots(public_name) WHERE status='active';
                CREATE INDEX IF NOT EXISTS ix_auth_access_slots_authority
                    ON auth_access_slots(authority_node_id,status,public_name);
                COMMIT;
                """
            )
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            await db.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _slot(row) -> dict | None:
        if row is None:
            return None
        return {
            "logical_agent_id": row[0],
            "public_name": row[1],
            "slot_kind": row[2],
            "display_suffix": row[3],
            "authority_node_id": row[4],
            "access_generation": int(row[5]),
            "status": row[6],
            "created_at": row[7],
            "updated_at": row[8],
            "deleted_at": row[9],
        }

    async def get_slot(self, logical_agent_id: str) -> dict | None:
        db = await self.foundation._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT logical_agent_id,public_name,slot_kind,display_suffix,"
                    "authority_node_id,access_generation,status,created_at,updated_at,deleted_at "
                    "FROM auth_access_slots WHERE logical_agent_id=?",
                    (logical_agent_id,),
                )
            ).fetchone()
            return self._slot(row)
        finally:
            await db.close()

    async def get_slot_by_public_name(self, public_name: str) -> dict | None:
        name = (public_name or "").strip()
        if not name:
            return None
        db = await self.foundation._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT logical_agent_id,public_name,slot_kind,display_suffix,"
                    "authority_node_id,access_generation,status,created_at,updated_at,deleted_at "
                    "FROM auth_access_slots WHERE public_name=? AND status='active'",
                    (name,),
                )
            ).fetchone()
            return self._slot(row)
        finally:
            await db.close()

    async def list_slots(
        self,
        *,
        include_deleted: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        db = await self.foundation._connect()
        try:
            where = "" if include_deleted else " WHERE status='active'"
            query = (
                "SELECT logical_agent_id,public_name,slot_kind,display_suffix,"
                "authority_node_id,access_generation,status,created_at,updated_at,deleted_at "
                "FROM auth_access_slots" + where + " ORDER BY created_at,logical_agent_id"
            )
            params: list[int] = []
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                params.extend((max(1, int(limit)), max(0, int(offset))))
            rows = await (await db.execute(query, params)).fetchall()
            return [self._slot(row) for row in rows]
        finally:
            await db.close()

    async def register_slot(
        self,
        logical_agent_id: str,
        authority_node_id: str,
        *,
        slot_kind: str = "persistent",
        display_suffix: str | None = None,
    ) -> dict:
        logical_agent_id = logical_agent_id.strip()
        authority_node_id = authority_node_id.strip()
        if not logical_agent_id or not authority_node_id:
            raise ValueError("logical_agent_id and authority_node_id are required")
        if slot_kind not in {"persistent", "legacy"}:
            raise ValueError("slot_kind must be persistent or legacy")
        display_suffix = (display_suffix or "").strip() or None
        now = _utc_now()
        db = await self.foundation._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            existing = await (
                await db.execute(
                    "SELECT logical_agent_id,public_name,slot_kind,display_suffix,"
                    "authority_node_id,access_generation,status,created_at,updated_at,deleted_at "
                    "FROM auth_access_slots WHERE logical_agent_id=?",
                    (logical_agent_id,),
                )
            ).fetchone()
            if existing is not None:
                await db.commit()
                return self._slot(existing)
            used = {
                row[0]
                for row in await (
                    await db.execute(
                        "SELECT public_name FROM auth_access_slots WHERE status='active'"
                    )
                ).fetchall()
            }
            ordinal = 1
            public_name = self._public_name(ordinal)
            while public_name in used:
                ordinal += 1
                public_name = self._public_name(ordinal)
            await db.execute(
                "INSERT INTO auth_access_slots("
                "logical_agent_id,public_name,slot_kind,display_suffix,"
                "authority_node_id,access_generation,status,created_at,updated_at,deleted_at) "
                "VALUES(?,?,?,?,?,0,'active',?,?,NULL)",
                (
                    logical_agent_id,
                    public_name,
                    slot_kind,
                    display_suffix,
                    authority_node_id,
                    now,
                    now,
                ),
            )
            await self.foundation._bump_generation(db, now)
            await self.foundation._audit(
                db,
                "access_slot_register",
                "success",
                node_id=authority_node_id,
                resource=logical_agent_id,
                details={"public_name": public_name, "slot_kind": slot_kind},
                now=now,
            )
            await db.commit()
            return await self.get_slot(logical_agent_id)
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def update_display_suffix(
        self, logical_agent_id: str, display_suffix: str | None
    ) -> dict:
        display_suffix = (display_suffix or "").strip() or None
        now = _utc_now()
        db = await self.foundation._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "UPDATE auth_access_slots SET display_suffix=?,updated_at=? "
                "WHERE logical_agent_id=? AND status='active'",
                (display_suffix, now, logical_agent_id),
            )
            if cur.rowcount != 1:
                raise AuthNotFoundError("access slot does not exist")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
        return await self.get_slot(logical_agent_id)

    async def reserve_codes(self, codes: Iterable[str], *, reason: str = "legacy_selector") -> int:
        self._require_key()
        normalized = []
        for value in codes:
            try:
                normalized.append(normalize_access_code(value))
            except ValueError:
                continue
        if not normalized:
            return 0
        now = _utc_now()
        db = await self.foundation._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            changed = 0
            for value in sorted(set(normalized)):
                cur = await db.execute(
                    "INSERT OR IGNORE INTO auth_access_code_tombstones"
                    "(code_index,reason,created_at) VALUES(?,?,?)",
                    (self._index(value), reason, now),
                )
                changed += max(0, int(cur.rowcount or 0))
            if changed:
                await self.foundation._bump_generation(db, now)
            await db.commit()
            return changed
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def issue_code(
        self,
        logical_agent_id: str,
        *,
        forbidden_codes: Iterable[str] = (),
        requested_code: str | None = None,
    ) -> dict:
        self._require_key()
        forbidden = set()
        for item in forbidden_codes:
            try:
                forbidden.add(normalize_access_code(item))
            except ValueError:
                continue
        now = _utc_now()
        db = await self.foundation._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            slot = await (
                await db.execute(
                    "SELECT public_name,access_generation,status,authority_node_id "
                    "FROM auth_access_slots "
                    "WHERE logical_agent_id=?",
                    (logical_agent_id,),
                )
            ).fetchone()
            if slot is None or slot[2] != "active":
                raise AuthNotFoundError("active access slot does not exist")
            code = None
            code_index = None
            verifier = None
            attempts = [requested_code] if requested_code is not None else [None] * 256
            for candidate in attempts:
                value = (
                    normalize_access_code(candidate)
                    if candidate is not None
                    else f"{secrets.randbelow(10_000):04d}"
                )
                if value in forbidden:
                    if requested_code is not None:
                        raise AuthConflictError(
                            "requested access code conflicts with reserved selector"
                        )
                    continue
                idx = self._index(value)
                prior = await (
                    await db.execute(
                        "SELECT 1 FROM auth_access_codes WHERE code_index=? "
                        "UNION ALL SELECT 1 FROM auth_access_code_tombstones "
                        "WHERE code_index=? LIMIT 1",
                        (idx, idx),
                    )
                ).fetchone()
                if prior is not None:
                    if requested_code is not None:
                        raise AuthConflictError("requested access code was already used")
                    continue
                code = value
                code_index = idx
                verifier = self._verifier(value)
                break
            if code is None:
                raise AuthConflictError("unable to allocate unique access code")
            next_generation = int(slot[1]) + 1
            await db.execute(
                "UPDATE auth_access_codes SET retired_at=COALESCE(retired_at,?),"
                "tombstoned_at=COALESCE(tombstoned_at,?) "
                "WHERE logical_agent_id=? AND retired_at IS NULL",
                (now, now, logical_agent_id),
            )
            await db.execute(
                "INSERT INTO auth_access_codes("
                "code_index,logical_agent_id,generation,verifier,created_at,"
                "retired_at,tombstoned_at) VALUES(?,?,?,?,?,NULL,NULL)",
                (code_index, logical_agent_id, next_generation, verifier, now),
            )
            await db.execute(
                "UPDATE auth_access_slots SET access_generation=?,updated_at=? "
                "WHERE logical_agent_id=?",
                (next_generation, now, logical_agent_id),
            )
            await self.foundation._bump_generation(db, now)
            await self.foundation._audit(
                db,
                "access_code_issue",
                "success",
                node_id=slot[3],
                resource=logical_agent_id,
                details={"public_name": slot[0], "access_generation": next_generation},
                now=now,
            )
            await db.commit()
            return {
                "logical_agent_id": logical_agent_id,
                "public_name": slot[0],
                "authority_node_id": slot[3],
                "access_generation": next_generation,
                "access_code": code,
            }
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def resolve_code(self, code: str) -> dict | None:
        value = normalize_access_code(code)
        code_index = self._index(value)
        expected = self._verifier(value)
        db = await self.foundation._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT c.verifier,c.generation,s.logical_agent_id,s.public_name,s.slot_kind,"
                    "s.display_suffix,s.authority_node_id,s.access_generation,s.status "
                    "FROM auth_access_codes c JOIN auth_access_slots s "
                    "ON s.logical_agent_id=c.logical_agent_id WHERE c.code_index=? "
                    "AND c.retired_at IS NULL AND c.tombstoned_at IS NULL",
                    (code_index,),
                )
            ).fetchone()
        finally:
            await db.close()
        if row is None or row[8] != "active" or int(row[1]) != int(row[7]):
            return None
        if not hmac.compare_digest(str(row[0]), expected):
            return None
        return {
            "logical_agent_id": row[2],
            "public_name": row[3],
            "slot_kind": row[4],
            "display_suffix": row[5],
            "authority_node_id": row[6],
            "access_generation": int(row[7]),
        }

    async def retire_slot(self, logical_agent_id: str) -> dict:
        now = _utc_now()
        db = await self.foundation._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT public_name,access_generation,status,authority_node_id "
                    "FROM auth_access_slots WHERE logical_agent_id=?",
                    (logical_agent_id,),
                )
            ).fetchone()
            if row is None:
                raise AuthNotFoundError("access slot does not exist")
            if row[2] == "deleted":
                await db.commit()
                return await self.get_slot(logical_agent_id)
            next_generation = int(row[1]) + 1
            await db.execute(
                "UPDATE auth_access_codes SET retired_at=COALESCE(retired_at,?),"
                "tombstoned_at=COALESCE(tombstoned_at,?) "
                "WHERE logical_agent_id=? AND retired_at IS NULL",
                (now, now, logical_agent_id),
            )
            await db.execute(
                "UPDATE auth_access_slots SET status='deleted',access_generation=?,"
                "updated_at=?,deleted_at=? "
                "WHERE logical_agent_id=?",
                (next_generation, now, now, logical_agent_id),
            )
            await self.foundation._bump_generation(db, now)
            await self.foundation._audit(
                db,
                "access_slot_retire",
                "success",
                node_id=row[3],
                resource=logical_agent_id,
                details={"public_name": row[0], "access_generation": next_generation},
                now=now,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
        return await self.get_slot(logical_agent_id)
