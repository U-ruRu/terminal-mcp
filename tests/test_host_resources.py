def test_cpu_usage_percent_uses_delta_across_all_logical_cores(monkeypatch):
    import terminal_mcp.host_resources as resources_module

    samples = iter([(1000, 400), (1200, 450)])
    monkeypatch.setattr(resources_module, "_CPU_SAMPLE", None)
    monkeypatch.setattr(resources_module, "_read_cpu_times", lambda: next(samples))
    assert resources_module._cpu_usage_percent() is None
    assert resources_module._cpu_usage_percent() == 75.0
