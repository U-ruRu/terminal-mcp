"""A repeated current operator rotation returns its committed generation receipt."""

import pytest
from test_access_mesh_native_lifecycle import actor
from test_access_mesh_native_lifecycle import fixture as fixture

from terminal_mcp.application.access_mesh_operator import AccessMeshOperator


@pytest.mark.asyncio
async def test_rotation_retry_preserves_committed_generation_and_code(fixture):
    operator = AccessMeshOperator(fixture.app)
    issued = await operator.mutate(
        actor("operator"), action="create", mode="persistent", idempotency_key="create"
    )
    mutation = dict(
        action="rotate", slot_id=issued["slot_id"], expected_revision=1, idempotency_key="rotate"
    )
    committed = await operator.mutate(actor("operator"), **mutation)
    repeated = await operator.mutate(actor("operator"), **mutation)
    assert repeated == committed
    assert repeated["code_generation"] == 2
    assert fixture.store.slot("firstbyte", issued["slot_id"]).revision == 2
