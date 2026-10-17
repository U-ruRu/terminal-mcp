"""Operator rotation commits its selector and receipt in one current transaction."""

import pytest
from test_access_mesh_native_lifecycle import actor
from test_access_mesh_native_lifecycle import fixture as fixture

from terminal_mcp.application.access_mesh_operator import AccessMeshOperator


@pytest.mark.asyncio
async def test_rotation_failure_rolls_back_selector_and_receipt(fixture, monkeypatch):
    operator = AccessMeshOperator(fixture.app)
    issued = await operator.mutate(
        actor("operator"), action="create", mode="persistent", idempotency_key="create"
    )
    previous = fixture.store.slot("firstbyte", issued["slot_id"])
    mutation = dict(
        action="rotate", slot_id=issued["slot_id"], expected_revision=1, idempotency_key="rotate"
    )
    original = fixture.store._after_apply

    def broken(db, event, **kwargs):
        original(db, event, **kwargs)
        if event.kind == "AccessCodeRotated":
            raise RuntimeError("synthetic post-receipt rollback")

    monkeypatch.setattr(fixture.store, "_after_apply", broken)
    with pytest.raises(RuntimeError, match="post-receipt rollback"):
        await operator.mutate(actor("operator"), **mutation)
    assert fixture.store.slot("firstbyte", issued["slot_id"]) == previous
    with fixture.store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM access_mesh_issuer_receipts").fetchone()[0] == 1
