"""Carry the admitted namespace through awaits and SQLite worker threads."""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps

from terminal_mcp.core.access_mesh_grants import AccessMeshError

REGISTRY_NAMESPACE = ContextVar("access_registry_namespace", default=None)


def assert_registry_scope(db):
    """Check the durable fence in the same transaction as the proposed write."""
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='access_mesh_registry_scope'"
    ).fetchone()
    if not exists:
        return  # Standalone storage projections used before application bootstrap.
    row = db.execute(
        "SELECT namespace_id,transition_id FROM access_mesh_registry_scope WHERE singleton=1"
    ).fetchone()
    expected = REGISTRY_NAMESPACE.get()
    if row and expected is not None and expected != row[0]:
        raise AccessMeshError("mesh_namespace_mismatch")
    if row and row[1]:
        raise AccessMeshError("mesh_transition_pending")


def registry_operation(method):
    """An operation keeps its original scope even if topology commits meanwhile."""

    @wraps(method)
    async def scoped(self, *args, **kwargs):
        store = self.store if hasattr(self, "store") else self.mesh.store
        if not hasattr(store, "namespace_id"):
            store = self.mesh.store
        expected = REGISTRY_NAMESPACE.get()
        if expected is not None and expected != store.namespace_id:
            raise AccessMeshError("mesh_namespace_mismatch")
        token = REGISTRY_NAMESPACE.set(expected or store.namespace_id)
        try:
            return await method(self, *args, **kwargs)
        finally:
            REGISTRY_NAMESPACE.reset(token)

    return scoped
