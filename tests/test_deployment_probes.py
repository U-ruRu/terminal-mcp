"""A fresh node always exposes the same authenticated role and operator routes."""

import httpx
import pytest

from terminal_mcp.deployment._release_common import ReleaseError
from terminal_mcp.deployment.probes import ProbeClient


def test_ingress_uses_all_three_roles_without_mode_flags():
    base = "https://terminal.example"
    seen = []
    probe = ProbeClient()

    def reply(request):
        path = request.url.path
        seen.append(path)
        if path.startswith("/.well-known/oauth-authorization-server"):
            return httpx.Response(200, json={"issuer": base})
        if path.startswith("/.well-known/oauth-protected-resource"):
            return httpx.Response(200, json={"resource": base + "/terminal-mcp/access/v1/mcp"})
        return httpx.Response(200 if path == "/connect" else 401)

    probe.client.close()
    probe.client = httpx.Client(transport=httpx.MockTransport(reply))
    try:
        result = probe.ingress(base)
    finally:
        probe.close()
    assert {item["path"] for item in result} == set(seen)
    assert set(seen) == {
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/connect",
        "/actions/access/slots",
        "/actions/access/defaults",
        *{f"/terminal-mcp/{role}/v1/mcp" for role in ("access", "executor", "coordinator")},
        *{
            "/.well-known/oauth-protected-resource/terminal-mcp/" + role + "/v1/mcp"
            for role in ("access", "executor", "coordinator")
        },
    }


@pytest.mark.parametrize("ready,status", [(True, 200), (False, 503), (False, 200)])
def test_candidate_activation_requires_registry_readiness(ready, status):
    probe = ProbeClient()
    probe.client.close()
    seen = []

    def reply(request):
        seen.append(request.url.path)
        return httpx.Response(status, json={"ok": True, "ready": ready, "release": "candidate"})

    probe.client = httpx.Client(transport=httpx.MockTransport(reply))
    try:
        if ready:
            assert probe.health(
                "http://127.0.0.1/health/live", "candidate", readiness=True
            )["ok"] is True
        else:
            with pytest.raises(ReleaseError):
                probe.health("http://127.0.0.1/health/live", "candidate", readiness=True)
    finally:
        probe.close()
    assert seen == ["/health/ready"]
