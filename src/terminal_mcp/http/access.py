from __future__ import annotations

import base64
import hashlib
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from terminal_mcp.auth.device_context import access_actor, paired_device_context
from terminal_mcp.auth.foundation import AuthConflictError, AuthNotFoundError


class BootstrapOwnerRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    display_name: str = Field(default="", max_length=160)
    password: str = Field(min_length=8, max_length=1024)


class EnrollmentCreateRequest(BaseModel):
    purpose: str
    target_principal_id: str | None = None
    display_name: str = Field(default="", max_length=160)


class EnrollmentExchangeRequest(BaseModel):
    secret: str = Field(min_length=16, max_length=512)
    username: str = Field(default="", max_length=120)
    display_name: str = Field(default="", max_length=160)
    password: str = Field(min_length=8, max_length=1024)


class ClientAssignRequest(BaseModel):
    principal_id: str
    client_type: str = "device"
    label: str = Field(default="", max_length=160)
    resources: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=lambda: ["terminal:read"])
    role: str = "reader"


class RevokeRequest(BaseModel):
    principal_id: str | None = None
    client_id: str | None = None
    grant_id: str | None = None
    expected_generation: int | None = None


def _error(code: str, status: int, **extra):
    return JSONResponse({"error": code, **extra}, status_code=status)


def _local_node_id(settings) -> str:
    return settings.fleet_instance_id.strip() or (
        urlsplit(settings.public_base_url).hostname or "local"
    )


def _realm_material(settings) -> tuple[str, str, str, str]:
    node_id = _local_node_id(settings)
    realm_id = "realm_" + hashlib.sha256(settings.public_base_url.encode()).hexdigest()[:24]
    raw_private = settings.fleet_signing_private_key.strip()
    if raw_private:
        raw = base64.urlsafe_b64decode(raw_private + "=" * (-len(raw_private) % 4))
        public = (
            Ed25519PrivateKey.from_private_bytes(raw)
            .public_key()
            .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        )
    else:
        public = hashlib.sha256(
            ("terminal-mcp-local-authority:" + settings.public_base_url).encode()
        ).digest()
    public_text = base64.urlsafe_b64encode(public).rstrip(b"=").decode()
    return realm_id, node_id, "ed25519:" + hashlib.sha256(public).hexdigest()[:24], public_text


async def _actor(request, auth, pairing_store, access_store, scope=None):
    try:
        return await access_actor(request, auth, pairing_store, access_store, scope), None
    except PermissionError as exc:
        code = str(exc)
        return None, _error(code, 403 if code == "insufficient_access_scope" else 401)
    except Exception:
        return None, _error("unauthorized", 401)


async def _manager(request, settings, auth, pairing_store, access_store):
    actor, error = await _actor(request, auth, pairing_store, access_store, "auth:manage")
    if error:
        return None, error
    realm = await access_store.realm_status()
    if realm is None:
        return None, _error("auth_uninitialized", 409)
    local = _local_node_id(settings)
    if realm["authority_node_id"] != local:
        return None, _error(
            "auth_authority_required", 409, authority_node_id=realm["authority_node_id"]
        )
    return actor, None


def build_access_router(settings, auth, pairing_store, access_store):
    router = APIRouter()

    @router.get("/access/bootstrap/status", include_in_schema=False)
    async def bootstrap_status():
        realm = await access_store.realm_status()
        return (
            {"status": "uninitialized"}
            if realm is None
            else {
                "status": "initialized",
                "realm_id": realm["realm_id"],
                "authority_node_id": realm["authority_node_id"],
            }
        )

    @router.post("/access/bootstrap/owner", include_in_schema=False)
    async def bootstrap_owner(request: Request, body: BootstrapOwnerRequest):
        try:
            paired = await paired_device_context(request, auth, pairing_store)
        except Exception:
            return _error("paired_device_required", 401)
        realm_id, node_id, key_id, public_key = _realm_material(settings)
        try:
            return await access_store.bootstrap_owner(
                realm_id=realm_id,
                authority_node_id=node_id,
                signing_key_id=key_id,
                public_signing_key=public_key,
                paired_client_id=paired.client_id,
                client_label=paired.label,
                username=body.username,
                display_name=body.display_name,
                password=body.password,
            )
        except AuthConflictError:
            return _error("already_initialized", 409)
        except ValueError as exc:
            return _error("invalid_request", 400, detail=str(exc))

    @router.get("/access/me", include_in_schema=False)
    async def me(request: Request):
        actor, error = await _actor(request, auth, pairing_store, access_store)
        if error:
            return error
        return {
            "principal_id": actor.principal_id,
            "username": actor.username,
            "display_name": actor.display_name,
            "client_id": actor.client_id,
            "client_label": actor.client_label,
            "scopes": sorted(actor.scopes),
            "resources": sorted(actor.resources),
            "roles": sorted(actor.roles),
        }

    @router.get("/access/principals", include_in_schema=False)
    async def principals(request: Request):
        _who, error = await _manager(request, settings, auth, pairing_store, access_store)
        if error:
            return error
        return {
            "principals": await access_store.list_principals(),
            "security_generation": await access_store.foundation.security_generation(),
        }

    @router.get("/access/clients", include_in_schema=False)
    async def clients(request: Request):
        _who, error = await _manager(request, settings, auth, pairing_store, access_store)
        if error:
            return error
        return {
            "clients": await access_store.list_clients(),
            "security_generation": await access_store.foundation.security_generation(),
        }

    @router.get("/access/grants", include_in_schema=False)
    async def grants(request: Request):
        _who, error = await _manager(request, settings, auth, pairing_store, access_store)
        if error:
            return error
        return {
            "grants": await access_store.list_grants(),
            "security_generation": await access_store.foundation.security_generation(),
        }

    @router.post("/access/enrollments", include_in_schema=False)
    async def create_enrollment(request: Request, body: EnrollmentCreateRequest):
        actor, error = await _manager(request, settings, auth, pairing_store, access_store)
        if error:
            return error
        try:
            return await access_store.create_enrollment(
                body.purpose,
                issuer_principal_id=actor.principal_id,
                issuer_client_id=actor.client_id,
                target_principal_id=body.target_principal_id,
                payload={"display_name": body.display_name}
                if body.purpose == "human_invite"
                else {},
                ttl_seconds=settings.auth_enrollment_ttl_sec
                if body.purpose == "human_invite"
                else settings.auth_recovery_ttl_sec,
            )
        except AuthNotFoundError:
            return _error("principal_not_found", 404)
        except ValueError as exc:
            return _error("invalid_request", 400, detail=str(exc))

    @router.post("/access/enrollments/exchange", include_in_schema=False)
    async def exchange_enrollment(body: EnrollmentExchangeRequest):
        try:
            return await access_store.consume_enrollment(
                body.secret,
                username=body.username,
                password=body.password,
                display_name=body.display_name,
            )
        except AuthConflictError:
            return _error("invalid_enrollment", 400)
        except (AuthNotFoundError, ValueError) as exc:
            return _error("invalid_request", 400, detail=str(exc))

    @router.post("/access/recovery", include_in_schema=False)
    async def recovery(request: Request, body: EnrollmentCreateRequest):
        actor, error = await _manager(request, settings, auth, pairing_store, access_store)
        if error:
            return error
        if not body.target_principal_id:
            return _error("target_principal_required", 400)
        try:
            return await access_store.create_enrollment(
                "password_reset",
                issuer_principal_id=actor.principal_id,
                issuer_client_id=actor.client_id,
                target_principal_id=body.target_principal_id,
                ttl_seconds=settings.auth_recovery_ttl_sec,
            )
        except AuthNotFoundError:
            return _error("principal_not_found", 404)

    @router.post("/access/recovery/exchange", include_in_schema=False)
    async def recovery_exchange(body: EnrollmentExchangeRequest):
        try:
            return await access_store.consume_enrollment(body.secret, password=body.password)
        except AuthConflictError:
            return _error("invalid_enrollment", 400)
        except (AuthNotFoundError, ValueError) as exc:
            return _error("invalid_request", 400, detail=str(exc))

    @router.post("/access/clients/{client_id}/assign", include_in_schema=False)
    async def assign_client(client_id: str, request: Request, body: ClientAssignRequest):
        actor, error = await _manager(request, settings, auth, pairing_store, access_store)
        if error:
            return error
        paired = await pairing_store.active_device_for_client(client_id)
        if paired is None:
            return _error("paired_client_not_found", 404)
        realm = await access_store.realm_status()
        resources = body.resources or [f"fleet:{realm['realm_id']}"]
        try:
            return await access_store.assign_client(
                client_id,
                body.principal_id,
                client_type=body.client_type,
                label=body.label.strip() or paired["label"],
                resources=resources,
                scopes=body.scopes,
                role=body.role,
                actor_principal_id=actor.principal_id,
                actor_client_id=actor.client_id,
            )
        except AuthNotFoundError:
            return _error("principal_not_found", 404)
        except AuthConflictError as exc:
            return _error("client_assignment_conflict", 409, detail=str(exc))

    @router.post("/access/revocations/preview", include_in_schema=False)
    async def revoke_preview(request: Request, body: RevokeRequest):
        _actor_record, error = await _manager(request, settings, auth, pairing_store, access_store)
        if error:
            return error
        if not any((body.principal_id, body.client_id, body.grant_id)):
            return _error("revocation_target_required", 400)
        return await access_store.revoke_preview(
            principal_id=body.principal_id, client_id=body.client_id, grant_id=body.grant_id
        )

    @router.post("/access/revocations/commit", include_in_schema=False)
    async def revoke_commit(request: Request, body: RevokeRequest):
        actor, error = await _manager(request, settings, auth, pairing_store, access_store)
        if error:
            return error
        if body.expected_generation is None:
            return _error("expected_generation_required", 400)
        try:
            return await access_store.revoke_commit(
                expected_generation=body.expected_generation,
                actor_principal_id=actor.principal_id,
                actor_client_id=actor.client_id,
                principal_id=body.principal_id,
                client_id=body.client_id,
                grant_id=body.grant_id,
            )
        except AuthConflictError as exc:
            code = str(exc)
            return _error(
                "last_auth_manager_required"
                if code == "last_auth_manager_required"
                else "stale_security_generation",
                409,
            )
        except AuthNotFoundError:
            return _error("revocation_target_not_found", 404)

    @router.get("/access/audit", include_in_schema=False)
    async def audit(request: Request):
        _who, error = await _manager(request, settings, auth, pairing_store, access_store)
        if error:
            return error
        return {"audit": await access_store.foundation.audit_events(200)}

    return router
