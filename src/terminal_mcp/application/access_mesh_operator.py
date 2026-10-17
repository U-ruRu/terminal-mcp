"""Operator-only provisioning, per-slot controls and durable default policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import datetime

from terminal_mcp.core.access_mesh_grants import AccessMeshError, SlotPolicy
from terminal_mcp.core.read_contract import bounded_page, decode_cursor, encode_cursor


class AccessMeshOperator:
    def __init__(self, mesh):
        self.mesh = mesh
        self.store = mesh.store

    @staticmethod
    def require_operator(actor, *, write=False):
        if actor.endpoint_role != "operator":
            raise AccessMeshError("access_denied")
        admission = actor.admission()
        if admission is None:
            raise AccessMeshError("persistent_auth_required")
        admission.require("terminal:execute" if write else "terminal:read")

    def _view(self, slot, defaults=None):
        defaults = defaults or self.mesh.defaults_state()
        now = self.mesh.clock()
        capture = self.store.capture_policy_window(slot, now)
        return {
            "issuer_node_id": slot.issuer_id,
            "slot_id": slot.slot_id,
            "logical_agent_id": slot.logical_agent_id,
            "public_name": self.store.human_name(slot),
            "mode": slot.kind,
            "state": slot.state,
            "revision": slot.revision,
            "code_generation": slot.code_generation,
            "policy": asdict(slot.policy),
            "inheritance": {
                "mode": "inherit" if slot.inherit_policy else "override",
                "source": (defaults["inheritance"]["source"] if slot.inherit_policy else "slot"),
                "parent": {
                    **defaults["inheritance"]["parent"],
                    "source": "server",
                    "id": self.store.local_node_id,
                    "revision": defaults["revision"],
                    "version": f"server:{self.store.local_node_id}:{defaults['revision']}",
                },
                "reset_supported": True,
                "applies_to": "next_window",
                "pending": capture.policy != slot.policy,
            },
            "inheritance_supported": True,
            "management_contract_version": 2,
            "policy_source": defaults["policy_source"] if slot.inherit_policy else "slot",
            "policy_override": None if slot.inherit_policy else asdict(slot.policy),
            "inherited_policy": defaults["policy"],
            "parent": {"source": defaults["policy_source"], "revision": defaults["revision"]},
            "current_policy": asdict(capture.policy),
            "policy_applies_to": "next_session",
            "session_lifecycle": self.store.merged_cycle(slot, now),
        }

    def _spec(self, actor, key, payload):
        request_key = hashlib.sha256(
            json.dumps(
                ["operator-mesh-v2", self.store.local_node_id, actor.principal_id, key],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {"request_key": request_key, "fingerprint": fingerprint, "connection_key": None}

    def _policy(self, values, base):
        try:
            return SlotPolicy(**{**asdict(base), **(values or {})})
        except (TypeError, ValueError) as exc:
            raise AccessMeshError("access_mesh_invalid_policy") from exc

    async def slots(self, actor, *, limit=20, cursor=None):
        self.require_operator(actor)
        forwarded = await self.mesh.proxy_registry(
            actor, "slots", {"limit": limit, "cursor": cursor}
        )
        if forwarded is not None:
            return forwarded
        scope = {"kind": "access.operator.slots", "node": self.store.local_node_id}
        after = decode_cursor(cursor, scope)
        rows = await asyncio.to_thread(self.store.issuer_slots, after=after, limit=limit + 1)
        defaults = await asyncio.to_thread(self.mesh.defaults_state)
        views = await asyncio.to_thread(lambda: [self._view(slot, defaults) for _, slot in rows])
        page, _ = bounded_page(views, limit=limit, cursor=None, scope=scope)
        next_cursor = (
            encode_cursor(rows[len(page) - 1][0], scope) if page and len(rows) > len(page) else None
        )
        return {"ok": True, "slots": page, "next_cursor": next_cursor}

    async def get(self, actor, *, slot_id):
        self.require_operator(actor)
        forwarded = await self.mesh.proxy_registry(actor, "get", {"slot_id": slot_id})
        if forwarded is not None:
            return forwarded
        slot = await asyncio.to_thread(self.store.registry_slot, slot_id)
        if slot is None:
            raise AccessMeshError("access_mesh_slot_not_found")
        return {"ok": True, "slot": await asyncio.to_thread(self._view, slot)}

    async def defaults(self, actor):
        self.require_operator(actor)
        forwarded = await self.mesh.proxy_registry(actor, "defaults", {})
        if forwarded is not None:
            return forwarded
        return {"ok": True, **await asyncio.to_thread(self.mesh.defaults_state)}

    async def mutate(
        self,
        actor,
        *,
        action,
        idempotency_key,
        slot_id=None,
        expected_revision=None,
        mode=None,
        policy=None,
        deadline_at=None,
        legacy_enabled=None,
        inherit_policy=None,
        expected_parent_revision=None,
    ):
        self.require_operator(actor, write=True)
        if action not in {
            "create",
            "defaults",
            "policy",
            "suspend",
            "resume",
            "delete",
            "rotate",
            "deadline",
            "end",
        }:
            raise AccessMeshError("invalid_request")
        payload = {
            "action": action,
            "slot_id": slot_id,
            "expected_revision": expected_revision,
            "mode": mode,
            "policy": policy,
            "deadline_at": deadline_at,
            "legacy_enabled": legacy_enabled,
            **({"inherit_policy": inherit_policy} if inherit_policy is not None else {}),
            **(
                {"expected_parent_revision": expected_parent_revision}
                if expected_parent_revision is not None
                else {}
            ),
        }
        forwarded = await self.mesh.proxy_registry(
            actor, "mutate", {**payload, "idempotency_key": idempotency_key}
        )
        if forwarded is not None:
            return forwarded
        spec = self._spec(actor, idempotency_key, payload)
        async with self.mesh._issuer_session_lock:
            previous = await asyncio.to_thread(
                self.store.receipt, spec["request_key"], spec["fingerprint"]
            )
            if previous is not None:
                return previous
            if action == "create":
                resolved = self._policy(policy, self.mesh.defaults)
                return await self.mesh.issue(
                    actor,
                    kind=mode,
                    policy=resolved,
                    start=mode == "legacy",
                    receipt_spec=spec,
                    inherit_policy=(policy is None) if inherit_policy is None else inherit_policy,
                )
            if action == "defaults":
                current = await self.defaults(actor)
                inherited = inherit_policy is True
                base = current["inherited_policy"] if inherited else current["policy"]
                resolved = self._policy(None if inherited else policy, SlotPolicy(**base))
                enabled = (
                    current["parent"]["legacy_enabled"]
                    if inherited
                    else current["legacy_enabled"]
                    if legacy_enabled is None
                    else legacy_enabled
                )
                result = {
                    "ok": True,
                    "issuer_node_id": self.store.local_node_id,
                    "slot_id": "defaults",
                    "revision": expected_revision + 1,
                    "policy": asdict(resolved),
                    "legacy_enabled": enabled,
                }
                receipt = self.store.prepare_receipt(event_id="defaults", result=result, **spec)
                await asyncio.to_thread(
                    self.store.change_defaults,
                    expected_revision=expected_revision,
                    policy=asdict(resolved),
                    legacy_enabled=enabled,
                    receipt=receipt,
                    inherit_policy=inherited,
                    parent=current["parent"],
                )
                self.mesh.defaults, self.mesh.legacy_enabled = resolved, enabled
                return result
            slot = await asyncio.to_thread(self.store.registry_slot, slot_id)
            if slot is None:
                raise AccessMeshError("access_mesh_slot_not_found")
            if slot.revision != expected_revision:
                raise AccessMeshError("revision_conflict")
            claimed = await asyncio.to_thread(
                self.store.numbers.group_winner, slot.issuer_id, slot.slot_id
            )
            if claimed and (claimed["issuer_id"], claimed["slot_id"]) != (
                slot.issuer_id,
                slot.slot_id,
            ):
                raise AccessMeshError("access_mesh_identity_conflict")
            changes = {
                "policy": "SlotPolicyChanged",
                "suspend": "SlotSuspended",
                "resume": "SlotResumed",
                "delete": "SlotDeleted",
                "rotate": "AccessCodeRotated",
                "deadline": "SessionUpdated",
                "end": "SessionEnded",
            }
            resolved, start, deadline = None, None, None
            if action == "policy":
                if inherit_policy is True:
                    parent = await asyncio.to_thread(self.mesh.defaults_state)
                    if expected_parent_revision != parent["revision"]:
                        raise AccessMeshError("revision_conflict")
                resolved = (
                    SlotPolicy(**self.mesh.defaults_state()["policy"])
                    if inherit_policy is True
                    else self._policy(policy, slot.policy)
                )
            if action == "deadline":
                cycle = await asyncio.to_thread(self.store.merged_cycle, slot, self.mesh.clock())
                if cycle["state"] not in {"active", "warning", "draining"}:
                    raise AccessMeshError("session_expired")
                try:
                    start = datetime.fromisoformat(cycle["started_at"])
                    deadline = datetime.fromisoformat(deadline_at)
                    if (
                        deadline.tzinfo is None
                        or deadline <= start
                        or deadline < datetime.fromisoformat(cycle["hard_expires_at"])
                    ):
                        raise ValueError("invalid deadline")
                except (TypeError, ValueError) as exc:
                    raise AccessMeshError("access_mesh_invalid_time") from exc
            number_end = (
                await asyncio.to_thread(self.store.number_cycle, slot, self.mesh.clock())
                if action == "end"
                else None
            )
            return await self.mesh.change(
                actor,
                slot_id=slot.slot_id,
                kind=changes[action],
                expected_revision=expected_revision,
                policy=resolved,
                effective_at=start,
                deadline_at=deadline,
                receipt_spec=spec,
                number_end=number_end,
                inherit_policy=inherit_policy if action == "policy" else None,
            )
