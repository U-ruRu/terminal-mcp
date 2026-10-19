import ctypes
from types import SimpleNamespace

import pytest


def test_cpu_usage_percent_uses_delta_across_all_logical_cores(monkeypatch):
    import terminal_mcp.host_resources as resources_module

    samples = iter([(1000, 400), (1200, 450)])
    monkeypatch.setattr(resources_module, "_CPU_SAMPLE", None)
    monkeypatch.setattr(resources_module, "_read_cpu_times", lambda: next(samples))
    assert resources_module._cpu_usage_percent() is None
    assert resources_module._cpu_usage_percent() == 75.0


def test_windows_cpu_counter_includes_idle_once_and_preserves_high_bits(monkeypatch):
    from terminal_mcp import host_resources_windows as native

    def counters(idle, kernel, user):
        for pointer, low, high in ((idle, 200, 1), (kernel, 500, 2), (user, 100, 3)):
            value = ctypes.cast(pointer, ctypes.POINTER(native._FileTime)).contents
            value.low, value.high = low, high
        return 1

    monkeypatch.setattr(native, "_kernel32", lambda: SimpleNamespace(GetSystemTimes=counters))
    assert native.cpu_times() == ((5 << 32) + 600, (1 << 32) + 200)


@pytest.mark.parametrize("succeeds", [False, True])
def test_windows_memory_uses_initialized_physical_memory_structure(monkeypatch, succeeds):
    from terminal_mcp import host_resources_windows as native

    def memory(pointer):
        status = ctypes.cast(pointer, ctypes.POINTER(native._MemoryStatus)).contents
        assert status.length == ctypes.sizeof(native._MemoryStatus) == 64
        status.total_physical, status.available_physical = 64 << 30, 17 << 30
        return int(succeeds)

    monkeypatch.setattr(native, "_kernel32", lambda: SimpleNamespace(GlobalMemoryStatusEx=memory))
    if succeeds:
        assert native.memory() == (64 << 30, 17 << 30)
    else:
        with pytest.raises(OSError, match="physical memory counters unavailable"):
            native.memory()
