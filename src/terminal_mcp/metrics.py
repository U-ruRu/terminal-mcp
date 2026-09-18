from __future__ import annotations

import asyncio
from collections import Counter, defaultdict


class Metrics:
    def __init__(self, provider, host="127.0.0.1", port=8081):
        self.provider = provider
        self.host = host
        self.port = port
        self.server = None
        self.counters = Counter()
        self.gauges = defaultdict(float)
        self.durations = defaultdict(list)

    @property
    def alive(self):
        return self.server is not None and self.server.is_serving()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, self.host, self.port)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    def inc(self, name, labels=(), value=1):
        self.counters[(name, tuple(labels))] += value

    def set(self, name, value, labels=()):
        self.gauges[(name, tuple(labels))] = value

    def observe(self, name, value, labels=()):
        self.durations[(name, tuple(labels))].append(float(value))

    def render(self):
        out = []
        for (name, labels), value in sorted(self.counters.items()):
            out.append(f"{name}{_labels(labels)} {value}")
        for (name, labels), value in sorted(self.gauges.items()):
            out.append(f"{name}{_labels(labels)} {value}")
        for (name, labels), values in sorted(self.durations.items()):
            out.append(f"{name}_count{_labels(labels)} {len(values)}")
            out.append(f"{name}_sum{_labels(labels)} {sum(values)}")
        return "\n".join(out) + "\n"

    async def _handle(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 2)
            enabled = self.provider.current.metrics_enabled
            status = (
                b"200 OK" if enabled and line.startswith(b"GET /metrics ") else b"404 Not Found"
            )
            body = self.render().encode() if status.startswith(b"200") else b""
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\nContent-Type: text/plain; version=0.0.4\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def _labels(labels):
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}"
