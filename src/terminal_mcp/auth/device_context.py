from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request


@dataclass(frozen=True, slots=True)
class PairedDeviceContext:
    client_id: str
    device_id: str
    label: str


@dataclass(frozen=True, slots=True)
class AccessActor:
    principal_id: str
    username: str
    display_name: str
    client_id: str
    client_label: str
    scopes: frozenset[str]
    resources: frozenset[str]
    roles: frozenset[str]


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


async def paired_device_context(request: Request, auth, pairing_store) -> PairedDeviceContext:
    token = _bearer(request)
    if not token:
        raise PermissionError("missing_token")
    claims = await auth.verify_access(token, ["terminal:read"])
    client_id = str(claims.get("sub", ""))
    device = await pairing_store.active_device_for_client(client_id)
    if not client_id or device is None:
        raise PermissionError("device_required")
    return PairedDeviceContext(client_id, device["device_id"], device["label"])


async def access_actor(
    request: Request,
    auth,
    pairing_store,
    access_store,
    required_scope: str | None = None,
) -> AccessActor:
    paired = await paired_device_context(request, auth, pairing_store)
    record = await access_store.actor_for_client(paired.client_id)
    if record is None or record["status"] != "active" or record["client_status"] != "active":
        raise PermissionError("access_enrollment_required")
    scopes: set[str] = set()
    resources: set[str] = set()
    roles: set[str] = set()
    for grant in record["grants"]:
        scopes.update(grant["scopes"])
        resources.update(grant["resources"])
        if grant["role"]:
            roles.add(grant["role"])
    if required_scope and required_scope not in scopes:
        raise PermissionError("insufficient_access_scope")
    return AccessActor(
        record["principal_id"],
        record["username"],
        record["display_name"],
        paired.client_id,
        record["client_label"],
        frozenset(scopes),
        frozenset(resources),
        frozenset(roles),
    )
