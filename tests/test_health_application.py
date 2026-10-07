import pytest

from terminal_mcp.application.actor import ActorContext
from terminal_mcp.application.health import HealthApplication


class Service:
    async def health(self, auth_mode, agent_id):
        assert auth_mode == "oauth"
        assert agent_id is None
        return {"ok": True, "status": "healthy"}


class Gate:
    async def resolve(self, *args, **kwargs):
        raise AssertionError("infrastructure health must not resolve a managed session")


@pytest.mark.asyncio
async def test_coordinator_infrastructure_health_does_not_require_session():
    app = HealthApplication(Service(), Gate(), auth_mode="oauth")

    result = await app.health(ActorContext(endpoint_role="coordinator"))

    assert result == {"ok": True, "status": "healthy"}
