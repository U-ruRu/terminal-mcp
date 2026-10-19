"""Read-only Win32 host counters used by the Console resource sampler."""

import ctypes
from functools import lru_cache


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def ticks(self):
        return (self.high << 32) | self.low


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("load", ctypes.c_uint32),
        ("total_physical", ctypes.c_uint64),
        ("available_physical", ctypes.c_uint64),
        ("total_page_file", ctypes.c_uint64),
        ("available_page_file", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64),
        ("available_virtual", ctypes.c_uint64),
        ("available_extended_virtual", ctypes.c_uint64),
    ]


@lru_cache(maxsize=1)
def _kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.GetSystemTimes.argtypes = [ctypes.POINTER(_FileTime)] * 3
    library.GetSystemTimes.restype = ctypes.c_int32
    library.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MemoryStatus)]
    library.GlobalMemoryStatusEx.restype = ctypes.c_int32
    library.GetTickCount64.argtypes = []
    library.GetTickCount64.restype = ctypes.c_uint64
    return library


def cpu_times() -> tuple[int, int]:
    idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
    if not _kernel32().GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        raise OSError("Windows CPU counters unavailable")
    # Kernel time includes idle; adding it again would understate utilization.
    return kernel.ticks() + user.ticks(), idle.ticks()


def memory() -> tuple[int, int]:
    status = _MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not _kernel32().GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("Windows physical memory counters unavailable")
    return status.total_physical, status.available_physical


def uptime_seconds() -> int:
    return _kernel32().GetTickCount64() // 1000
