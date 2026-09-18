from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    detailed_logging: bool = False
    log_level: str = "INFO"
    metrics_enabled: bool = True


class RuntimeConfigProvider:
    def __init__(self, path: Path = Path("/etc/terminal-mcp/runtime.env")):
        self.path = path
        self.current = RuntimeConfig()
        self._mtime_ns = None
        self._last_check = 0.0
        self._invalid_version = None
        self._task = None
        self._stopping = False
        self.warning_callback = None
        self.reload_callback = None

    @property
    def alive(self):
        return self._task is not None and not self._task.done()

    async def start(self):
        await self.refresh(force=True)
        if not self.alive:
            self._stopping = False
            self._task = asyncio.create_task(self._watch(), name="terminal-runtime-config")

    async def stop(self):
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _watch(self):
        while not self._stopping:
            await asyncio.sleep(1)
            await self.refresh(force=True)

    async def before_tool_call(self):
        await self.refresh()
        return self.current

    async def refresh(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_check < 1.0:
            return self.current
        self._last_check = now
        try:
            stat = await asyncio.to_thread(self.path.stat)
        except FileNotFoundError:
            if self._mtime_ns is not None:
                self._mtime_ns = None
                self._invalid_version = None
                self.current = RuntimeConfig()
                if self.reload_callback:
                    self.reload_callback(self.current)
            return self.current
        version = (stat.st_mtime_ns, stat.st_size)
        if self._mtime_ns == version:
            return self.current
        try:
            text = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            parsed = self._parse(text)
        except Exception as exc:
            if self._invalid_version != version:
                self._invalid_version = version
                if self.warning_callback:
                    self.warning_callback(str(exc))
            self._mtime_ns = version
            return self.current
        self.current = parsed
        self._mtime_ns = version
        self._invalid_version = None
        if self.reload_callback:
            self.reload_callback(parsed)
        return parsed

    @staticmethod
    def _parse(text):
        values = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError("runtime config contains an invalid assignment")
            key, value = (part.strip() for part in line.split("=", 1))
            if not key or any(ch.isspace() for ch in key):
                raise ValueError("runtime config contains an invalid key")
            values[key] = value

        def flag(name, default):
            raw = values.get(name, str(default)).lower()
            if raw not in {"true", "false"}:
                raise ValueError(f"{name} must be true or false")
            return raw == "true"

        level = values.get("TERMINAL_MCP_LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("TERMINAL_MCP_LOG_LEVEL is invalid")
        return RuntimeConfig(
            flag("TERMINAL_MCP_DETAILED_LOGGING", False),
            level,
            flag("TERMINAL_MCP_METRICS_ENABLED", True),
        )
