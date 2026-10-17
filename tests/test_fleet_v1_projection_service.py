import pytest

from terminal_mcp.fleet.config import FleetConfig
from terminal_mcp.fleet.projection import FleetProjectionService


class RelayLocalSource:
    async def manifest(self):
        return {
            "fleet_id": "fleet-a",
            "node_id": "node-local",
            "source_stream_generation": "gen-a",
        }

    async def query(self, resource, **kwargs):
        assert resource == "commands"
        assert kwargs["limit"] == 10
        return {
            "resource": resource,
            "items": [
                {
                    "hash": "cmd-local",
                    "cmd": "printf secret",
                    "status": "completed",
                    "auth_principal_id": "principal-secret",
                }
            ],
            "complete": True,
        }


class RelayResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("GET", "https://relay.example/query")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                "relay failed",
                request=request,
                response=response,
            )

    def json(self):
        return self.body


class RelayClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, *, headers, params=None):
        del headers, params
        if "node-good.example" in url:
            return RelayResponse(
                {
                    "resource": "commands",
                    "items": [
                        {
                            "hash": "cmd-good",
                            "cmd": "cat private",
                            "status": "running",
                            "auth_token": "must-not-escape",
                        }
                    ],
                    "complete": True,
                }
            )
        if "node-auth.example" in url:
            return RelayResponse({}, 401)
        raise AssertionError(url)


@pytest.mark.asyncio
async def test_query_relay_reports_partial_sources_when_a_peer_denies_access():
    from terminal_mcp.fleet.config import FleetPeer

    peers = (
        FleetPeer("node-good", "https://node-good.example", "public", "token"),
        FleetPeer("node-auth", "https://node-auth.example", "public", "token"),
    )
    service = FleetProjectionService(
        FleetConfig("projection-a", "key", peers, 1.0, 1.0),
        object(),
        local_source=RelayLocalSource(),
        owner_node_id="projection-a",
        client_factory=RelayClient,
    )

    result = await service.query("commands", limit=10)
    assert result["partial"] is True
    assert result["complete"] is False
    by_source = {item["source_node_id"]: item for item in result["sources"]}
    assert set(by_source) == {"node-local", "node-good", "node-auth"}

    assert by_source["node-auth"]["status"] == "OFFLINE_AUTH"


def test_command_query_redacts_credentials_and_shell_text():
    from terminal_mcp.fleet.projection import _redact_query_data

    result = _redact_query_data(
        {
            "items": [
                {
                    "hash": "cmd-a",
                    "status": "running",
                    "cmd": "private shell",
                    "auth_token": "secret",
                }
            ]
        },
        operation="query",
        resource="commands",
    )

    assert result == {"items": [{"hash": "cmd-a", "status": "running"}]}
