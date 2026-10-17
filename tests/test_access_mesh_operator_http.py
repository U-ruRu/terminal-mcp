"""Authenticated mobile controls, strict runtime validation, durable replay and policy."""

from test_access_mesh_native_lifecycle import actor

HEADERS = {"authorization": "Bearer mesh-test-token"}


def mutate(client, action, key, **payload):
    response = client.post(
        "/actions/access/mutate",
        headers=HEADERS,
        json={"action": action, "idempotency_key": key, **payload},
    )
    assert response.status_code == 200, response.text
    return response.json()


def internal_persistent_start(client, app, number):
    """Prepare a controlled persistent window for operator policy assertions."""

    async def start():
        mesh = app.state.access_mesh
        slot = mesh.store.registry_code_slot(number)
        return await mesh.change(
            actor("operator"),
            slot_id=slot.slot_id,
            expected_revision=slot.revision,
            kind="SessionStarted",
        )

    return client.portal.call(start)


def internal_persistent_end(client, app, number):
    async def end():
        mesh = app.state.access_mesh
        slot = mesh.store.registry_code_slot(number)
        return await mesh.change(
            actor("operator"),
            slot_id=slot.slot_id,
            expected_revision=slot.revision,
            kind="SessionEnded",
            number_end=mesh.store.number_cycle(slot, mesh.clock()),
        )

    return client.portal.call(end)
