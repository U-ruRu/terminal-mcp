import json
import secrets
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from terminal_mcp.storage.permissions import secure_database_path
from terminal_mcp.storage.sqlite_observability import (
    SqliteDiagnostics,
    open_observed_connection,
)

AUTH_SCHEMA_VERSION = 3


class AuthFoundationError(RuntimeError):
    pass


class AuthConflictError(AuthFoundationError):
    pass


class AuthNotFoundError(AuthFoundationError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalized(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


def _string_set(values, label: str) -> list[str]:
    result = sorted({_normalized(str(value), label) for value in values})
    if not result:
        raise ValueError(f"{label} requires at least one value")
    return result


class AuthFoundationStore:
    """Canonical identity/grant foundation kept outside the runtime rollback DB."""

    def __init__(self, path: Path, *, access_key_path: Path | None = None):
        self.path = Path(path)
        self.passwords = PasswordHasher()
        self.sqlite_diagnostics = SqliteDiagnostics("auth")
        from terminal_mcp.auth.access_authority import AccessCodeAuthority

        key_path = access_key_path or self.path.with_name(self.path.name + ".access-key")
        self.access = AccessCodeAuthority(self, key_path)

    def configure_observability(self, events, metrics):
        self.sqlite_diagnostics.configure(events, metrics)

    async def _connect(self):
        return await open_observed_connection(
            aiosqlite.connect,
            self.path,
            busy_timeout=5.0,
            diagnostics=self.sqlite_diagnostics,
            operation="auth_foundation",
            pragmas=(
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=FULL",
                "PRAGMA busy_timeout=5000",
                "PRAGMA foreign_keys=ON",
            ),
        )

    async def initialize(self) -> None:
        secure_database_path(self.path)
        db = await self._connect()
        try:
            schema_row = await (await db.execute("PRAGMA user_version")).fetchone()
            schema_version = int(schema_row[0]) if schema_row else 0
            if schema_version > AUTH_SCHEMA_VERSION:
                raise AuthFoundationError(
                    f"auth schema version {schema_version} is newer than supported version "
                    f"{AUTH_SCHEMA_VERSION}"
                )
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS auth_security_state(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    generation INTEGER NOT NULL CHECK(generation >= 0),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_realms(
                    realm_id TEXT PRIMARY KEY,
                    authority_node_id TEXT NOT NULL,
                    authority_epoch INTEGER NOT NULL CHECK(authority_epoch > 0),
                    signing_key_id TEXT NOT NULL,
                    public_signing_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_principals(
                    principal_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    password_verifier TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','disabled')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_clients(
                    client_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    client_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(principal_id) REFERENCES auth_principals(principal_id)
                );
                CREATE TABLE IF NOT EXISTS auth_grants(
                    grant_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    resources_json TEXT NOT NULL,
                    scopes_json TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    FOREIGN KEY(principal_id) REFERENCES auth_principals(principal_id),
                    FOREIGN KEY(client_id) REFERENCES auth_clients(client_id)
                );
                CREATE TABLE IF NOT EXISTS auth_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    principal_id TEXT,
                    client_id TEXT,
                    node_id TEXT,
                    resource TEXT,
                    grant_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_auth_clients_principal
                    ON auth_clients(principal_id,status);
                CREATE INDEX IF NOT EXISTS ix_auth_grants_principal
                    ON auth_grants(principal_id,revoked_at);
                CREATE INDEX IF NOT EXISTS ix_auth_grants_client
                    ON auth_grants(client_id,revoked_at);
                CREATE INDEX IF NOT EXISTS ix_auth_audit_created
                    ON auth_audit(created_at,id);
                """
            )
            await self.access.initialize(db)
            await db.execute(f"PRAGMA user_version={AUTH_SCHEMA_VERSION}")
            now = _utc_now()
            await db.execute(
                "INSERT OR IGNORE INTO auth_security_state(singleton,generation,updated_at) "
                "VALUES(1,0,?)",
                (now,),
            )
            await db.commit()
        finally:
            await db.close()

    async def _bump_generation(self, db, now: str) -> int:
        await db.execute(
            "UPDATE auth_security_state SET generation=generation+1,updated_at=? WHERE singleton=1",
            (now,),
        )
        row = await (
            await db.execute("SELECT generation FROM auth_security_state WHERE singleton=1")
        ).fetchone()
        return int(row[0])

    async def _audit(
        self,
        db,
        event_type: str,
        outcome: str,
        *,
        principal_id: str | None = None,
        client_id: str | None = None,
        node_id: str | None = None,
        resource: str | None = None,
        grant_id: str | None = None,
        details: dict | None = None,
        now: str,
    ) -> None:
        await db.execute(
            "INSERT INTO auth_audit"
            "(event_type,outcome,principal_id,client_id,node_id,resource,grant_id,"
            "details_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                event_type,
                outcome,
                principal_id,
                client_id,
                node_id,
                resource,
                grant_id,
                json.dumps(details or {}, separators=(",", ":"), sort_keys=True),
                now,
            ),
        )

    async def security_generation(self) -> int:
        db = await self._connect()
        try:
            row = await (
                await db.execute("SELECT generation FROM auth_security_state WHERE singleton=1")
            ).fetchone()
            return int(row[0])
        finally:
            await db.close()

    async def bootstrap_realm(
        self,
        realm_id: str,
        authority_node_id: str,
        signing_key_id: str,
        public_signing_key: str,
    ) -> dict:
        realm_id = _normalized(realm_id, "realm_id")
        authority_node_id = _normalized(authority_node_id, "authority_node_id")
        signing_key_id = _normalized(signing_key_id, "signing_key_id")
        public_signing_key = _normalized(public_signing_key, "public_signing_key")
        now = _utc_now()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            rows = await (
                await db.execute(
                    "SELECT realm_id,authority_node_id,authority_epoch,signing_key_id,"
                    "public_signing_key FROM auth_realms"
                )
            ).fetchall()
            if rows:
                row = rows[0]
                if (
                    row[0] == realm_id
                    and row[1] == authority_node_id
                    and row[3] == signing_key_id
                    and row[4] == public_signing_key
                ):
                    await db.commit()
                    return {
                        "realm_id": row[0],
                        "authority_node_id": row[1],
                        "authority_epoch": int(row[2]),
                        "signing_key_id": row[3],
                        "public_signing_key": row[4],
                    }
                raise AuthConflictError("auth realm is already initialized")
            await db.execute(
                "INSERT INTO auth_realms"
                "(realm_id,authority_node_id,authority_epoch,signing_key_id,public_signing_key,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    realm_id,
                    authority_node_id,
                    1,
                    signing_key_id,
                    public_signing_key,
                    now,
                    now,
                ),
            )
            await self._bump_generation(db, now)
            await self._audit(
                db,
                "realm_bootstrap",
                "success",
                node_id=authority_node_id,
                resource=realm_id,
                details={"authority_epoch": 1, "signing_key_id": signing_key_id},
                now=now,
            )
            await db.commit()
            return {
                "realm_id": realm_id,
                "authority_node_id": authority_node_id,
                "authority_epoch": 1,
                "signing_key_id": signing_key_id,
                "public_signing_key": public_signing_key,
            }
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def transfer_authority(
        self,
        realm_id: str,
        expected_epoch: int,
        new_authority_node_id: str,
        signing_key_id: str,
        public_signing_key: str,
    ) -> dict:
        new_authority_node_id = _normalized(new_authority_node_id, "new_authority_node_id")
        signing_key_id = _normalized(signing_key_id, "signing_key_id")
        public_signing_key = _normalized(public_signing_key, "public_signing_key")
        now = _utc_now()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT authority_node_id,authority_epoch FROM auth_realms WHERE realm_id=?",
                    (realm_id,),
                )
            ).fetchone()
            if row is None:
                raise AuthNotFoundError("auth realm does not exist")
            if int(row[1]) != int(expected_epoch):
                raise AuthConflictError("stale auth authority epoch")
            next_epoch = int(row[1]) + 1
            await db.execute(
                "UPDATE auth_realms SET authority_node_id=?,authority_epoch=?,signing_key_id=?,"
                "public_signing_key=?,updated_at=? WHERE realm_id=? AND authority_epoch=?",
                (
                    new_authority_node_id,
                    next_epoch,
                    signing_key_id,
                    public_signing_key,
                    now,
                    realm_id,
                    expected_epoch,
                ),
            )
            await self._bump_generation(db, now)
            await self._audit(
                db,
                "authority_transfer",
                "success",
                node_id=new_authority_node_id,
                resource=realm_id,
                details={
                    "previous_node_id": row[0],
                    "authority_epoch": next_epoch,
                    "signing_key_id": signing_key_id,
                },
                now=now,
            )
            await db.commit()
            return {
                "realm_id": realm_id,
                "authority_node_id": new_authority_node_id,
                "authority_epoch": next_epoch,
                "signing_key_id": signing_key_id,
                "public_signing_key": public_signing_key,
            }
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def create_principal(
        self,
        username: str,
        password: str,
        *,
        display_name: str = "",
        principal_id: str | None = None,
    ) -> dict:
        username = _normalized(username, "username")
        if not password:
            raise ValueError("password is required")
        principal_id = principal_id or "usr_" + secrets.token_urlsafe(18)
        display_name = display_name.strip() or username
        verifier = self.passwords.hash(password)
        now = _utc_now()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT INTO auth_principals"
                "(principal_id,username,username_key,display_name,password_verifier,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?)",
                (
                    principal_id,
                    username,
                    username.casefold(),
                    display_name,
                    verifier,
                    now,
                    now,
                ),
            )
            await self._bump_generation(db, now)
            await self._audit(
                db,
                "principal_create",
                "success",
                principal_id=principal_id,
                details={"username": username},
                now=now,
            )
            await db.commit()
            return {
                "principal_id": principal_id,
                "username": username,
                "display_name": display_name,
                "status": "active",
            }
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def verify_password(self, username: str, password: str) -> dict | None:
        username_key = _normalized(username, "username").casefold()
        db = await self._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT principal_id,username,display_name,password_verifier,status "
                    "FROM auth_principals WHERE username_key=?",
                    (username_key,),
                )
            ).fetchone()
        finally:
            await db.close()
        if row is None or row[4] != "active":
            return None
        try:
            valid = self.passwords.verify(row[3], password)
        except (VerifyMismatchError, InvalidHashError):
            return None
        if not valid:
            return None
        return {
            "principal_id": row[0],
            "username": row[1],
            "display_name": row[2],
            "status": row[4],
        }

    async def create_client(
        self,
        principal_id: str,
        client_type: str,
        label: str,
        *,
        client_id: str | None = None,
    ) -> dict:
        client_type = _normalized(client_type, "client_type")
        label = _normalized(label, "label")
        client_id = client_id or "cli_" + secrets.token_urlsafe(18)
        now = _utc_now()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            principal = await (
                await db.execute(
                    "SELECT status FROM auth_principals WHERE principal_id=?",
                    (principal_id,),
                )
            ).fetchone()
            if principal is None or principal[0] != "active":
                raise AuthNotFoundError("active principal does not exist")
            await db.execute(
                "INSERT INTO auth_clients"
                "(client_id,principal_id,client_type,label,status,created_at,revoked_at) "
                "VALUES(?,?,?,?,'active',?,NULL)",
                (client_id, principal_id, client_type, label, now),
            )
            await self._bump_generation(db, now)
            await self._audit(
                db,
                "client_create",
                "success",
                principal_id=principal_id,
                client_id=client_id,
                details={"client_type": client_type, "label": label},
                now=now,
            )
            await db.commit()
            return {
                "client_id": client_id,
                "principal_id": principal_id,
                "client_type": client_type,
                "label": label,
                "status": "active",
            }
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def create_grant(
        self,
        principal_id: str,
        client_id: str,
        resources,
        scopes,
        *,
        role: str = "",
        grant_id: str | None = None,
    ) -> dict:
        resource_values = _string_set(resources, "resource")
        scope_values = _string_set(scopes, "scope")
        role = role.strip()
        grant_id = grant_id or "grt_" + secrets.token_urlsafe(18)
        now = _utc_now()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            principal = await (
                await db.execute(
                    "SELECT status FROM auth_principals WHERE principal_id=?",
                    (principal_id,),
                )
            ).fetchone()
            client = await (
                await db.execute(
                    "SELECT principal_id,status FROM auth_clients WHERE client_id=?",
                    (client_id,),
                )
            ).fetchone()
            if principal is None or principal[0] != "active":
                raise AuthNotFoundError("active principal does not exist")
            if client is None or client[1] != "active":
                raise AuthNotFoundError("active client does not exist")
            if client[0] != principal_id:
                raise AuthConflictError("client belongs to a different principal")
            await db.execute(
                "INSERT INTO auth_grants"
                "(grant_id,principal_id,client_id,resources_json,scopes_json,role,"
                "created_at,revoked_at) "
                "VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    grant_id,
                    principal_id,
                    client_id,
                    json.dumps(resource_values, separators=(",", ":")),
                    json.dumps(scope_values, separators=(",", ":")),
                    role,
                    now,
                ),
            )
            await self._bump_generation(db, now)
            await self._audit(
                db,
                "grant_create",
                "success",
                principal_id=principal_id,
                client_id=client_id,
                resource=resource_values[0] if len(resource_values) == 1 else None,
                grant_id=grant_id,
                details={"resources": resource_values, "scopes": scope_values, "role": role},
                now=now,
            )
            await db.commit()
            return {
                "grant_id": grant_id,
                "principal_id": principal_id,
                "client_id": client_id,
                "resources": resource_values,
                "scopes": scope_values,
                "role": role,
                "revoked_at": None,
            }
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def revoke_grant(
        self,
        grant_id: str,
        *,
        actor_principal_id: str | None = None,
    ) -> bool:
        now = _utc_now()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT principal_id,client_id,resources_json FROM auth_grants "
                    "WHERE grant_id=? AND revoked_at IS NULL",
                    (grant_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                return False
            await db.execute(
                "UPDATE auth_grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL",
                (now, grant_id),
            )
            await self._bump_generation(db, now)
            resources = json.loads(row[2])
            await self._audit(
                db,
                "grant_revoke",
                "success",
                principal_id=actor_principal_id or row[0],
                client_id=row[1],
                resource=resources[0] if len(resources) == 1 else None,
                grant_id=grant_id,
                details={"subject_principal_id": row[0]},
                now=now,
            )
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def active_grants(self, principal_id: str) -> list[dict]:
        db = await self._connect()
        try:
            rows = await (
                await db.execute(
                    "SELECT grant_id,principal_id,client_id,resources_json,scopes_json,role "
                    "FROM auth_grants WHERE principal_id=? AND revoked_at IS NULL "
                    "ORDER BY grant_id",
                    (principal_id,),
                )
            ).fetchall()
        finally:
            await db.close()
        return [
            {
                "grant_id": row[0],
                "principal_id": row[1],
                "client_id": row[2],
                "resources": json.loads(row[3]),
                "scopes": json.loads(row[4]),
                "role": row[5],
            }
            for row in rows
        ]

    async def access_slot(self, logical_agent_id: str) -> dict | None:
        return await self.access.get_slot(logical_agent_id)

    async def access_slot_by_public_name(self, public_name: str) -> dict | None:
        return await self.access.get_slot_by_public_name(public_name)

    async def access_slots(self, *, include_deleted: bool = False) -> list[dict]:
        return await self.access.list_slots(include_deleted=include_deleted)

    async def register_access_slot(
        self,
        logical_agent_id: str,
        authority_node_id: str,
        *,
        slot_kind: str = "persistent",
        display_suffix: str | None = None,
    ) -> dict:
        return await self.access.register_slot(
            logical_agent_id, authority_node_id, slot_kind=slot_kind, display_suffix=display_suffix
        )

    async def reserve_access_codes(self, codes, *, reason: str = "legacy_selector") -> int:
        return await self.access.reserve_codes(codes, reason=reason)

    async def issue_access_code(
        self, logical_agent_id: str, *, forbidden_codes=(), requested_code: str | None = None
    ) -> dict:
        return await self.access.issue_code(
            logical_agent_id, forbidden_codes=forbidden_codes, requested_code=requested_code
        )

    async def resolve_access_code(self, access_code: str) -> dict | None:
        return await self.access.resolve_code(access_code)

    async def retire_access_slot(self, logical_agent_id: str) -> dict:
        return await self.access.retire_slot(logical_agent_id)

    async def update_access_display_suffix(
        self, logical_agent_id: str, display_suffix: str | None
    ) -> dict:
        return await self.access.update_display_suffix(logical_agent_id, display_suffix)

    async def audit_events(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        db = await self._connect()
        try:
            rows = await (
                await db.execute(
                    "SELECT event_type,outcome,principal_id,client_id,node_id,resource,grant_id,"
                    "details_json,created_at FROM auth_audit ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            ).fetchall()
        finally:
            await db.close()
        return [
            {
                "event_type": row[0],
                "outcome": row[1],
                "principal_id": row[2],
                "client_id": row[3],
                "node_id": row[4],
                "resource": row[5],
                "grant_id": row[6],
                "details": json.loads(row[7]),
                "created_at": row[8],
            }
            for row in rows
        ]
