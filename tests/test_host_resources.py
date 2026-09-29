from terminal_mcp.host_resources import collect_host_resources


def test_host_resources_contract_is_total_and_non_failing(tmp_path):
    resources = collect_host_resources(tmp_path)
    assert resources["status"] in {"available", "partial", "unavailable"}
    assert set(resources) == {"status", "cpu", "memory", "filesystem", "uptime"}
    for key in ("cpu", "memory", "filesystem", "uptime"):
        assert resources[key]["status"] in {"available", "unavailable"}
