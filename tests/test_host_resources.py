from terminal_mcp.host_resources import collect_host_resources


def test_host_resources_contract_is_total_and_non_failing(tmp_path):
    resources = collect_host_resources(tmp_path)
    assert resources["status"] in {"available", "partial", "unavailable"}
    assert set(resources) == {"status", "cpu", "memory", "filesystem", "uptime"}
    for key in ("cpu", "memory", "filesystem", "uptime"):
        assert resources[key]["status"] in {"available", "unavailable"}


def test_cpu_usage_percent_uses_delta_across_all_logical_cores(monkeypatch):
    import terminal_mcp.host_resources as resources_module

    samples = iter([(1000, 400), (1200, 450)])
    monkeypatch.setattr(resources_module, "_CPU_SAMPLE", None)
    monkeypatch.setattr(resources_module, "_read_cpu_times", lambda: next(samples))
    assert resources_module._cpu_usage_percent() is None
    assert resources_module._cpu_usage_percent() == 75.0
