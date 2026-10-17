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
    """Mobile provisioning starts a persistent slot via internal issuer API.

    Public Access.start intentionally accepts action only.
    """

    async def start():
        return await app.state.access_mesh.issuer_session(
            actor("access"), action="start", mode="persistent", code=number
        )

    return client.portal.call(start)


def internal_persistent_end(client, app, number):
    async def end():
        return await app.state.access_mesh.issuer_session(
            actor("access"), action="end", mode="persistent", code=number
        )

    return client.portal.call(end)
