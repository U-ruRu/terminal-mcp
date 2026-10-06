from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from terminal_mcp.auth.continuity import ManagerContinuityLock
from terminal_mcp.auth.foundation import AuthConflictError, AuthFoundationStore, AuthNotFoundError


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} is required")
    return value


def _values(values, label: str) -> list[str]:
    result = sorted({_text(str(value), label) for value in values})
    if not result:
        raise ValueError(f"{label} requires at least one value")
    return result


class AccessStore:
    def __init__(self, foundation: AuthFoundationStore, pairing_store, oauth_store=None):
        self.foundation = foundation
        self.pairing_store = pairing_store
        self.oauth_store = oauth_store
        self.manager_continuity = ManagerContinuityLock(foundation.path)
        self.pairing_store.configure_manager_continuity(
            self.manager_continuity, self._manager_removal_allowed
        )
        if self.oauth_store is not None:
            self.oauth_store.configure_manager_continuity(
                self.manager_continuity, self._manager_removal_allowed
            )

    async def realm_status(self) -> dict | None:
        db = await self.foundation._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT realm_id,authority_node_id,authority_epoch,signing_key_id,"
                    "public_signing_key FROM auth_realms ORDER BY created_at LIMIT 1"
                )
            ).fetchone()
        finally:
            await db.close()
        if row is None:
            return None
        return {
            "realm_id": row[0],
            "authority_node_id": row[1],
            "authority_epoch": int(row[2]),
            "signing_key_id": row[3],
            "public_signing_key": row[4],
        }

    async def bootstrap_owner(
        self,
        *,
        realm_id: str,
        authority_node_id: str,
        signing_key_id: str,
        public_signing_key: str,
        paired_client_id: str,
        client_label: str,
        username: str,
        password: str,
        display_name: str = "",
    ) -> dict:
        realm_id = _text(realm_id, "realm_id")
        authority_node_id = _text(authority_node_id, "authority_node_id")
        signing_key_id = _text(signing_key_id, "signing_key_id")
        public_signing_key = _text(public_signing_key, "public_signing_key")
        paired_client_id = _text(paired_client_id, "paired_client_id")
        client_label = _text(client_label, "client_label")
        username = _text(username, "username")
        if not password:
            raise ValueError("password is required")
        display_name = display_name.strip() or username
        principal_id = "usr_" + secrets.token_urlsafe(18)
        grant_id = "grt_" + secrets.token_urlsafe(18)
        verifier = self.foundation.passwords.hash(password)
        resources = [f"fleet:{realm_id}"]
        scopes = ["auth:manage", "servers:manage", "terminal:execute", "terminal:read"]
        now = _now()
        db = await self.foundation._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            if await (await db.execute("SELECT 1 FROM auth_realms LIMIT 1")).fetchone():
                raise AuthConflictError("already_initialized")
            await db.execute(
                "INSERT INTO auth_realms"
                "(realm_id,authority_node_id,authority_epoch,signing_key_id,public_signing_key,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (realm_id, authority_node_id, 1, signing_key_id, public_signing_key, now, now),
            )
            await db.execute(
                "INSERT INTO auth_principals"
                "(principal_id,username,username_key,display_name,password_verifier,status,"
                "created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?)",
                (principal_id, username, username.casefold(), display_name, verifier, now, now),
            )
            await db.execute(
                "INSERT INTO auth_clients"
                "(client_id,principal_id,client_type,label,status,created_at,revoked_at) "
                "VALUES(?,?,?,?,'active',?,NULL)",
                (paired_client_id, principal_id, "management_device", client_label, now),
            )
            await db.execute(
                "INSERT INTO auth_grants"
                "(grant_id,principal_id,client_id,resources_json,scopes_json,role,"
                "created_at,revoked_at) "
                "VALUES(?,?,?,?,?,'owner',?,NULL)",
                (
                    grant_id,
                    principal_id,
                    paired_client_id,
                    json.dumps(resources, separators=(",", ":")),
                    json.dumps(scopes, separators=(",", ":")),
                    now,
                ),
            )
            generation = await self.foundation._bump_generation(db, now)
            await self.foundation._audit(
                db,
                "owner_bootstrap",
                "success",
                principal_id=principal_id,
                client_id=paired_client_id,
                node_id=authority_node_id,
                resource=resources[0],
                grant_id=grant_id,
                details={"role": "owner", "scopes": scopes},
                now=now,
            )
            await db.commit()
            return {
                "realm": {
                    "realm_id": realm_id,
                    "authority_node_id": authority_node_id,
                    "authority_epoch": 1,
                },
                "principal": {
                    "principal_id": principal_id,
                    "username": username,
                    "display_name": display_name,
                    "status": "active",
                },
                "client": {
                    "client_id": paired_client_id,
                    "principal_id": principal_id,
                    "client_type": "management_device",
                    "label": client_label,
                    "status": "active",
                },
                "grant": {
                    "grant_id": grant_id,
                    "principal_id": principal_id,
                    "client_id": paired_client_id,
                    "resources": resources,
                    "scopes": scopes,
                    "role": "owner",
                },
                "security_generation": generation,
            }
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def actor_for_client(self, client_id: str) -> dict | None:
        db = await self.foundation._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT p.principal_id,p.username,p.display_name,p.status,"
                    "c.client_id,c.client_type,c.label,c.status "
                    "FROM auth_clients c JOIN auth_principals p ON p.principal_id=c.principal_id "
                    "WHERE c.client_id=?",
                    (client_id,),
                )
            ).fetchone()
        finally:
            await db.close()
        if row is None:
            return None
        grants = await self.foundation.active_grants(row[0])
        return {
            "principal_id": row[0],
            "username": row[1],
            "display_name": row[2],
            "status": row[3],
            "client_id": row[4],
            "client_type": row[5],
            "client_label": row[6],
            "client_status": row[7],
            "grants": [grant for grant in grants if grant["client_id"] == row[4]],
        }

    async def list_principals(self) -> list[dict]:
        db = await self.foundation._connect()
        try:
            rows = await (
                await db.execute(
                    "SELECT principal_id,username,display_name,status,created_at,updated_at "
                    "FROM auth_principals ORDER BY username_key"
                )
            ).fetchall()
        finally:
            await db.close()
        return [
            {
                "principal_id": r[0],
                "username": r[1],
                "display_name": r[2],
                "status": r[3],
                "created_at": r[4],
                "updated_at": r[5],
            }
            for r in rows
        ]

    async def list_clients(self) -> list[dict]:
        db = await self.foundation._connect()
        try:
            rows = await (
                await db.execute(
                    "SELECT client_id,principal_id,client_type,label,status,created_at,revoked_at "
                    "FROM auth_clients ORDER BY created_at,client_id"
                )
            ).fetchall()
        finally:
            await db.close()
        return [
            {
                "client_id": r[0],
                "principal_id": r[1],
                "client_type": r[2],
                "label": r[3],
                "status": r[4],
                "created_at": r[5],
                "revoked_at": r[6],
            }
            for r in rows
        ]

    async def list_grants(self) -> list[dict]:
        db = await self.foundation._connect()
        try:
            rows = await (
                await db.execute(
                    "SELECT grant_id,principal_id,client_id,resources_json,scopes_json,role,"
                    "created_at,revoked_at FROM auth_grants ORDER BY created_at,grant_id"
                )
            ).fetchall()
        finally:
            await db.close()
        return [
            {
                "grant_id": r[0],
                "principal_id": r[1],
                "client_id": r[2],
                "resources": json.loads(r[3]),
                "scopes": json.loads(r[4]),
                "role": r[5],
                "created_at": r[6],
                "revoked_at": r[7],
            }
            for r in rows
        ]

    @staticmethod
    def digest(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()

    async def assign_client(
        self,
        client_id: str,
        principal_id: str,
        *,
        client_type: str,
        label: str,
        resources,
        scopes,
        role: str,
        actor_principal_id: str,
        actor_client_id: str,
    ) -> dict:
        client_id = _text(client_id, "client_id")
        principal_id = _text(principal_id, "principal_id")
        client_type = _text(client_type, "client_type")
        label = _text(label, "label")
        resource_values = _values(resources, "resource")
        scope_values = _values(scopes, "scope")
        role = _text(role, "role")
        grant_id = "grt_" + secrets.token_urlsafe(18)
        now = _now()
        db = await self.foundation._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            principal = await (
                await db.execute(
                    "SELECT status FROM auth_principals WHERE principal_id=?", (principal_id,)
                )
            ).fetchone()
            if principal is None or principal[0] != "active":
                raise AuthNotFoundError("active principal does not exist")
            current = await (
                await db.execute(
                    "SELECT principal_id,status FROM auth_clients WHERE client_id=?", (client_id,)
                )
            ).fetchone()
            if current is not None and (current[0] != principal_id or current[1] != "active"):
                raise AuthConflictError("client is already assigned")
            if current is None:
                await db.execute(
                    "INSERT INTO auth_clients"
                    "(client_id,principal_id,client_type,label,status,created_at,revoked_at) "
                    "VALUES(?,?,?,?,'active',?,NULL)",
                    (client_id, principal_id, client_type, label, now),
                )
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
            generation = await self.foundation._bump_generation(db, now)
            await self.foundation._audit(
                db,
                "client_assign",
                "success",
                principal_id=actor_principal_id,
                client_id=actor_client_id,
                grant_id=grant_id,
                details={
                    "subject_principal_id": principal_id,
                    "subject_client_id": client_id,
                    "resources": resource_values,
                    "scopes": scope_values,
                    "role": role,
                },
                now=now,
            )
            await db.commit()
            return {
                "client_id": client_id,
                "principal_id": principal_id,
                "grant_id": grant_id,
                "resources": resource_values,
                "scopes": scope_values,
                "role": role,
                "security_generation": generation,
            }
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def create_enrollment(
        self,
        purpose: str,
        *,
        issuer_principal_id: str,
        issuer_client_id: str,
        target_principal_id: str | None = None,
        payload: dict | None = None,
        ttl_seconds: int = 900,
    ) -> dict:
        purpose = _text(purpose, "purpose")
        if purpose not in {"human_invite", "password_reset"}:
            raise ValueError("unsupported enrollment purpose")
        if purpose == "password_reset" and not target_principal_id:
            raise ValueError("target_principal_id is required for password reset")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        secret = secrets.token_urlsafe(32)
        enrollment_id = "enr_" + secrets.token_urlsafe(18)
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=int(ttl_seconds))).isoformat()
        db = await self.foundation._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            if target_principal_id:
                target = await (
                    await db.execute(
                        "SELECT status FROM auth_principals WHERE principal_id=?",
                        (target_principal_id,),
                    )
                ).fetchone()
                if target is None or target[0] != "active":
                    raise AuthNotFoundError("active target principal does not exist")
            await db.execute(
                "INSERT INTO auth_enrollments"
                "(enrollment_id,purpose,secret_hash,issuer_principal_id,issuer_client_id,"
                "target_principal_id,payload_json,created_at,expires_at,consumed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                (
                    enrollment_id,
                    purpose,
                    self.digest(secret),
                    issuer_principal_id,
                    issuer_client_id,
                    target_principal_id,
                    json.dumps(payload or {}, separators=(",", ":"), sort_keys=True),
                    now,
                    expires_at,
                ),
            )
            generation = await self.foundation._bump_generation(db, now)
            await self.foundation._audit(
                db,
                "enrollment_create",
                "success",
                principal_id=issuer_principal_id,
                client_id=issuer_client_id,
                details={
                    "enrollment_id": enrollment_id,
                    "purpose": purpose,
                    "target_principal_id": target_principal_id,
                    "expires_at": expires_at,
                },
                now=now,
            )
            await db.commit()
            return {
                "enrollment_id": enrollment_id,
                "purpose": purpose,
                "secret": secret,
                "expires_at": expires_at,
                "security_generation": generation,
            }
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def consume_enrollment(
        self,
        secret: str,
        *,
        username: str = "",
        password: str,
        display_name: str = "",
    ) -> dict:
        if not secret or not password:
            raise ValueError("secret and password are required")
        now = _now()
        db = await self.foundation._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT enrollment_id,purpose,issuer_principal_id,issuer_client_id,"
                    "target_principal_id,payload_json,expires_at,consumed_at "
                    "FROM auth_enrollments WHERE secret_hash=?",
                    (self.digest(secret),),
                )
            ).fetchone()
            if row is None or row[7] is not None or row[6] <= now:
                raise AuthConflictError("invalid_enrollment")
            purpose = row[1]
            payload = json.loads(row[5])
            if purpose == "human_invite":
                uname = _text(username, "username")
                principal_id = "usr_" + secrets.token_urlsafe(18)
                name = display_name.strip() or str(payload.get("display_name") or uname)
                await db.execute(
                    "INSERT INTO auth_principals"
                    "(principal_id,username,username_key,display_name,password_verifier,status,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,'active',?,?)",
                    (
                        principal_id,
                        uname,
                        uname.casefold(),
                        name,
                        self.foundation.passwords.hash(password),
                        now,
                        now,
                    ),
                )
                result = {
                    "purpose": purpose,
                    "principal": {
                        "principal_id": principal_id,
                        "username": uname,
                        "display_name": name,
                        "status": "active",
                    },
                }
                subject_principal_id = principal_id
            else:
                subject_principal_id = row[4]
                if not subject_principal_id:
                    raise AuthConflictError("reset enrollment has no target")
                cursor = await db.execute(
                    "UPDATE auth_principals SET password_verifier=?,updated_at=? "
                    "WHERE principal_id=? AND status='active'",
                    (
                        self.foundation.passwords.hash(password),
                        now,
                        subject_principal_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AuthNotFoundError("active principal does not exist")
                result = {"purpose": purpose, "principal_id": subject_principal_id}
            cursor = await db.execute(
                "UPDATE auth_enrollments SET consumed_at=? "
                "WHERE enrollment_id=? AND consumed_at IS NULL",
                (now, row[0]),
            )
            if cursor.rowcount != 1:
                raise AuthConflictError("invalid_enrollment")
            generation = await self.foundation._bump_generation(db, now)
            await self.foundation._audit(
                db,
                "enrollment_consume",
                "success",
                principal_id=row[2],
                client_id=row[3],
                details={
                    "enrollment_id": row[0],
                    "purpose": purpose,
                    "subject_principal_id": subject_principal_id,
                },
                now=now,
            )
            await db.commit()
            result["security_generation"] = generation
            return result
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def revoke_preview(
        self,
        *,
        principal_id: str | None = None,
        client_id: str | None = None,
        grant_id: str | None = None,
    ) -> dict:
        generation = await self.foundation.security_generation()
        grants = await self.list_grants()
        clients = await self.list_clients()
        principals = await self.list_principals()
        affected_grants = [
            item
            for item in grants
            if item["revoked_at"] is None
            and (
                (grant_id and item["grant_id"] == grant_id)
                or (client_id and item["client_id"] == client_id)
                or (principal_id and item["principal_id"] == principal_id)
            )
        ]
        affected_clients = [
            item
            for item in clients
            if item["status"] == "active"
            and (
                (client_id and item["client_id"] == client_id)
                or (principal_id and item["principal_id"] == principal_id)
            )
        ]
        affected_principals = [
            item
            for item in principals
            if principal_id and item["principal_id"] == principal_id and item["status"] == "active"
        ]
        return {
            "security_generation": generation,
            "principals": affected_principals,
            "clients": affected_clients,
            "grants": affected_grants,
        }

    async def _active_manager_client_ids(self, db) -> list[str]:
        rows = await (
            await db.execute(
                "SELECT g.client_id,g.scopes_json FROM auth_grants g "
                "JOIN auth_clients c ON c.client_id=g.client_id "
                "JOIN auth_principals p ON p.principal_id=g.principal_id "
                "WHERE g.revoked_at IS NULL AND c.status='active' AND p.status='active'"
            )
        ).fetchall()
        clients: list[str] = []
        for client_id, scopes_json in rows:
            if "auth:manage" in json.loads(scopes_json) and client_id not in clients:
                clients.append(client_id)
        return clients

    async def _has_active_manager(self, db, *, exclude_client_id: str | None = None) -> bool:
        for client_id in await self._active_manager_client_ids(db):
            if client_id == exclude_client_id:
                continue
            try:
                usable = await self.pairing_store.manager_transport_usable(client_id)
            except Exception:
                return False
            if usable:
                return True
        return False

    async def _manager_removal_allowed(self, client_id: str) -> bool:
        db = await self.foundation._connect()
        try:
            managers = await self._active_manager_client_ids(db)
            if client_id not in managers:
                return True
            return await self._has_active_manager(db, exclude_client_id=client_id)
        finally:
            await db.close()

    async def revoke_commit(
        self,
        *,
        expected_generation: int,
        actor_principal_id: str,
        actor_client_id: str,
        principal_id: str | None = None,
        client_id: str | None = None,
        grant_id: str | None = None,
    ) -> dict:
        async with self.manager_continuity.hold():
            return await self._revoke_commit_locked(
                expected_generation=expected_generation,
                actor_principal_id=actor_principal_id,
                actor_client_id=actor_client_id,
                principal_id=principal_id,
                client_id=client_id,
                grant_id=grant_id,
            )

    async def _revoke_commit_locked(
        self,
        *,
        expected_generation: int,
        actor_principal_id: str,
        actor_client_id: str,
        principal_id: str | None = None,
        client_id: str | None = None,
        grant_id: str | None = None,
    ) -> dict:
        now = _now()
        db = await self.foundation._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            current = await (
                await db.execute("SELECT generation FROM auth_security_state WHERE singleton=1")
            ).fetchone()
            if int(current[0]) != int(expected_generation):
                raise AuthConflictError("stale_security_generation")
            changed = 0
            if grant_id:
                cursor = await db.execute(
                    "UPDATE auth_grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL",
                    (now, grant_id),
                )
                changed += cursor.rowcount
            if client_id:
                cursor = await db.execute(
                    "UPDATE auth_grants SET revoked_at=? WHERE client_id=? AND revoked_at IS NULL",
                    (now, client_id),
                )
                changed += cursor.rowcount
                cursor = await db.execute(
                    "UPDATE auth_clients SET status='revoked',revoked_at=? "
                    "WHERE client_id=? AND status='active'",
                    (now, client_id),
                )
                changed += cursor.rowcount
            if principal_id:
                cursor = await db.execute(
                    "UPDATE auth_grants SET revoked_at=? "
                    "WHERE principal_id=? AND revoked_at IS NULL",
                    (now, principal_id),
                )
                changed += cursor.rowcount
                cursor = await db.execute(
                    "UPDATE auth_clients SET status='revoked',revoked_at=? "
                    "WHERE principal_id=? AND status='active'",
                    (now, principal_id),
                )
                changed += cursor.rowcount
                cursor = await db.execute(
                    "UPDATE auth_principals SET status='disabled',updated_at=? "
                    "WHERE principal_id=? AND status='active'",
                    (now, principal_id),
                )
                changed += cursor.rowcount
            if changed == 0:
                raise AuthNotFoundError("nothing active to revoke")
            if not await self._has_active_manager(db):
                raise AuthConflictError("last_auth_manager_required")
            generation = await self.foundation._bump_generation(db, now)
            await self.foundation._audit(
                db,
                "access_revoke",
                "success",
                principal_id=actor_principal_id,
                client_id=actor_client_id,
                grant_id=grant_id,
                details={
                    "subject_principal_id": principal_id,
                    "subject_client_id": client_id,
                    "changed": changed,
                },
                now=now,
            )
            await db.commit()
            return {
                "revoked": True,
                "changed": changed,
                "security_generation": generation,
            }
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
