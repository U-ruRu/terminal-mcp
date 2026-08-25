import asyncio
import secrets
from sqlite3 import IntegrityError

DEFAULT_READ_LINES = 500
MAX_READ_LINES = 1000
OPERATION_TIMEOUT_SECONDS = 45
RECOVERY_OUTPUT_LINES = 500


def _error(method, stage, exc):
    reason = str(exc).strip() or exc.__class__.__name__
    return f"{method}.{stage}: {reason}"


class TerminalService:
    def __init__(self, repo, terminal, max_lines, auth_mode="none", health_command=""):
        self.repo = repo
        self.terminal = terminal
        self.max_lines = max_lines
        self.auth_mode = auth_mode
        self.health_command = health_command

    async def _create_with_hash(self, cmd, status):
        for _ in range(32):
            cmd_hash = secrets.token_hex(4)
            try:
                return await self.repo.create(cmd, status=status, cmd_hash=cmd_hash)
            except IntegrityError:
                continue
        raise RuntimeError("unable to allocate unique command hash")

    async def run(self, cmd):
        command = None
        stage = "persist"
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                command = await self._create_with_hash(cmd, "queued")
                stage = "enqueue"
                await self.terminal.submit(command)
            return {"ok": True, "cmd_hash": command.cmd_hash, "error": None}
        except TimeoutError:
            if command is not None:
                await self.terminal.discard_queued(command.cmd_hash)
                await self.repo.delete_command(command.cmd_hash)
            return {
                "ok": False,
                "cmd_hash": None,
                "error": f"run.{stage}: timed out after 45000 ms",
            }
        except Exception as exc:
            if command is not None:
                await self.terminal.discard_queued(command.cmd_hash)
                await self.repo.delete_command(command.cmd_hash)
            return {"ok": False, "cmd_hash": None, "error": _error("run", stage, exc)}

    @staticmethod
    def _render(lines, scoped):
        return [
            f"[{line.appeared_at}] {line.text}"
            if scoped
            else f"[{line.appeared_at}] [{line.cmd_hash}] {line.text}"
            for line in lines
        ]

    async def read(self, cmd_hash=None, lines_count=DEFAULT_READ_LINES, offset=None):
        limit = max(1, min(int(lines_count), MAX_READ_LINES))
        result = {
            "ok": True,
            "lines": [],
            "next_offset": 0,
            "overall_lines_count": None,
            "displayed_lines_count": 0,
            "cmd_hash": cmd_hash,
            "status": None,
            "exit_code": None,
            "error": None,
        }
        stage = "load_command" if cmd_hash else "load_lines"
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                if cmd_hash:
                    command = await self.repo.get(cmd_hash)
                    if command is None:
                        result["status"] = "not_found"
                        result["overall_lines_count"] = 0
                        return result
                    result["status"] = command.status
                    result["exit_code"] = command.exit_code
                    result["error"] = command.error
                    result["ok"] = command.error is None
                    stage = "count_lines"
                    total = await self.repo.count_lines(cmd_hash)
                    result["overall_lines_count"] = total
                    if offset is None:
                        start = max(total - limit, 0)
                    elif offset < 0:
                        start = max(total + offset, 0)
                    else:
                        start = min(offset, total)
                    stage = "load_lines"
                    lines = await self.repo.read_command_lines(cmd_hash, limit, start)
                    result["next_offset"] = start + len(lines)
                    result["lines"] = self._render(lines, scoped=True)
                else:
                    if offset is None:
                        lines = await self.repo.read_global_tail(limit)
                        result["next_offset"] = lines[-1].seq if lines else 0
                    elif offset < 0:
                        lines = await self.repo.read_global_tail(limit, abs(offset))
                        result["next_offset"] = lines[-1].seq if lines else 0
                    else:
                        lines = await self.repo.read_global_after_cursor(limit, offset)
                        result["next_offset"] = lines[-1].seq if lines else offset
                    result["lines"] = self._render(lines, scoped=False)
                result["displayed_lines_count"] = len(result["lines"])
                return result
        except TimeoutError:
            result["ok"] = False
            result["error"] = f"read.{stage}: timed out after 45000 ms"
            return result
        except Exception as exc:
            result["ok"] = False
            result["error"] = _error("read", stage, exc)
            return result

    async def recovery(self, cmd):
        command = None
        stage = "persist"
        started_at = asyncio.get_running_loop().time()
        try:
            command = await self._create_with_hash(cmd, "running")
            stage = "execute"
            duration_ms = await self.terminal.recovery(
                command, timeout_seconds=OPERATION_TIMEOUT_SECONDS
            )
            current = await self.repo.get(command.cmd_hash) or command
            stage = "count_lines"
            total = await self.repo.count_lines(command.cmd_hash)
            stage = "load_lines"
            start = max(total - RECOVERY_OUTPUT_LINES, 0)
            lines = await self.repo.read_command_lines(
                command.cmd_hash, RECOVERY_OUTPUT_LINES, start
            )
            plugin_error = current.error
            return {
                "ok": plugin_error is None and current.status in {"completed", "failed"},
                "cmd_hash": command.cmd_hash,
                "lines": self._render(lines, scoped=True),
                "overall_lines_count": total,
                "displayed_lines_count": len(lines),
                "exit_code": current.exit_code,
                "error": plugin_error,
                "duration_ms": duration_ms,
            }
        except Exception as exc:
            elapsed = round((asyncio.get_running_loop().time() - started_at) * 1000)
            if command is not None:
                current = await self.repo.get(command.cmd_hash) or command
                current.status = "failed"
                current.error = _error("recovery", stage, exc)
                await self.repo.update(current)
            return {
                "ok": False,
                "cmd_hash": command.cmd_hash if command else None,
                "lines": [],
                "overall_lines_count": 0,
                "displayed_lines_count": 0,
                "exit_code": None,
                "error": _error("recovery", stage, exc),
                "duration_ms": elapsed,
            }

    async def cancel(self, cmd_hash):
        stage = "lookup"
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT_SECONDS):
                command = await self.repo.get(cmd_hash)
                if command is None:
                    return {
                        "ok": False,
                        "cmd_hash": cmd_hash,
                        "error": "cancel.lookup: command not found",
                    }
                stage = "stop"
                ok, error = await self.terminal.cancel(
                    command, timeout_seconds=OPERATION_TIMEOUT_SECONDS
                )
                return {"ok": ok, "cmd_hash": cmd_hash, "error": error}
        except TimeoutError:
            return {
                "ok": False,
                "cmd_hash": cmd_hash,
                "error": f"cancel.{stage}: timed out after 45000 ms",
            }
        except Exception as exc:
            return {"ok": False, "cmd_hash": cmd_hash, "error": _error("cancel", stage, exc)}

    async def health(self, auth_mode):
        terminal = await self.terminal.health()
        result = {
            "ok": terminal.get("ok", False),
            "application": "terminal-mcp",
            "storage": "ok",
            "auth_mode": auth_mode,
            "terminal": terminal,
        }
        if self.health_command:
            custom = await self.terminal.capture(
                self.health_command, timeout_ms=5000, max_output_lines=min(1000, self.max_lines)
            )
            result["custom_command"] = {"command": self.health_command, **custom}
        return result
