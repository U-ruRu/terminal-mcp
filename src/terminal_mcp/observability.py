from __future__ import annotations

import json
import logging
import logging.handlers
import queue
import sys
from datetime import UTC, datetime
from pathlib import Path

from terminal_mcp.trace import current_trace_id, current_upstream_request_id

_MINIMAL = {
    "application_started",
    "application_stopped",
    "runtime_config_reloaded",
    "runtime_config_invalid",
    "tool_completed",
    "tool_timeout",
    "tool_cancelled",
    "client_disconnected",
    "worker_failed",
    "worker_restarted",
    "sqlite_busy",
    "sqlite_error",
    "plugin_exception",
    "logging_records_dropped",
}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(record.event, separators=(",", ":"), ensure_ascii=False)


class NonBlockingQueueHandler(logging.handlers.QueueHandler):
    def __init__(self, q, owner):
        super().__init__(q)
        self.owner = owner

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.owner.dropped += 1
            if record.levelno >= logging.WARNING:
                sys.stderr.write(json.dumps(record.event) + "\n")


class SecureRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        try:
            self.baseFilename and Path(self.baseFilename).chmod(0o640)
        except OSError:
            pass
        return stream

    def doRollover(self):
        super().doRollover()
        for path in [
            Path(self.baseFilename),
            Path(self.baseFilename + ".1"),
            Path(self.baseFilename + ".2"),
        ]:
            try:
                path.chmod(0o640)
            except FileNotFoundError:
                pass


class EventLogger:
    def __init__(self, path: Path, provider, metrics, max_records=10000):
        self.path = path
        self.provider = provider
        self.metrics = metrics
        self.dropped = 0
        self.listener = None
        self.queue = queue.Queue(max_records)
        self.logger = logging.getLogger(f"terminal_mcp.events.{id(self)}")
        self.logger.propagate = False
        self.logger.handlers = [NonBlockingQueueHandler(self.queue, self)]
        self.logger.setLevel(logging.DEBUG)

    @property
    def alive(self):
        return (
            self.listener is not None
            and self.listener._thread is not None
            and self.listener._thread.is_alive()
        )

    def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o750)
        handler = SecureRotatingFileHandler(
            self.path, maxBytes=10 * 1024 * 1024, backupCount=2, encoding="utf-8", delay=True
        )
        handler.setFormatter(JsonFormatter())
        self.listener = logging.handlers.QueueListener(
            self.queue, handler, respect_handler_level=True
        )
        self.listener.start()

    def stop(self):
        if self.listener:
            self.listener.stop()
            self.listener = None

    def emit(self, event, level="INFO", **fields):
        if event not in _MINIMAL and not self.provider.current.detailed_logging:
            return
        payload = {
            "timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": level,
            "event": event,
            "trace_id": current_trace_id.get(),
            "upstream_request_id": current_upstream_request_id.get(),
        }
        payload.update(
            {
                k: v
                for k, v in fields.items()
                if k
                not in {
                    "cmd",
                    "command",
                    "lines",
                    "stdout",
                    "stderr",
                    "authorization",
                    "body",
                    "url",
                }
            }
        )
        record = logging.LogRecord(
            self.logger.name, getattr(logging, level), __file__, 0, "", (), None
        )
        record.event = payload
        self.logger.handle(record)
        if self.dropped:
            self.metrics.set("terminal_mcp_logging_records_dropped_total", self.dropped)
