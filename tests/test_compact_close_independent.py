"""Independent compact/chunk acceptance; isolated stores and public wire only."""


import pytest
from test_access_mesh_messages_v2 import _public_name, actor
from test_access_mesh_messages_v2 import mesh_pair as mesh_pair


@pytest.mark.asyncio
async def test_oversized_send_preflight_rejects_before_commit(mesh_pair):
    node = mesh_pair.nodes["bacloud"]
    with node.store.connect() as db:
        before = list(db.execute("SELECT * FROM coordination_messages"))
    result = await node.message(
        actor("alice", "independent-preflight"),
        text="🧪" * 16384,
        target=_public_name(node.mesh.store.slots["bob"]),
        mode="notify",
    )
    assert result["ok"] is False and result["code"] == "output_item_too_large"
    assert result["outcome"] == "not_committed"
    with node.store.connect() as db:
        after = list(db.execute("SELECT * FROM coordination_messages"))
    assert before == after
