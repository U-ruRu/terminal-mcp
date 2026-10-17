"""A fresh node always exposes the same authenticated role and operator routes."""

import httpx

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
