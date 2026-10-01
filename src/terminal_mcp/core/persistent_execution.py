from __future__ import annotations


class PersistentExecutionFence:
    """Fence commands attributed to one exact Persistent work session."""

    def __init__(self, repo, terminal, task_store):
        self.repo = repo
        self.terminal = terminal
        self.task_store = task_store

    @staticmethod
    def _blocker(command, kind: str) -> dict:
        return {
            "kind": kind,
            "command_hash": command.cmd_hash,
            "pid": command.pid,
            "queue_id": command.queue_id,
        }

    async def _cancel_or_block(self, command) -> dict | None:
        if command.status == "queued":
            if await self.repo.cancel_queued(command.cmd_hash):
                return None
            command = await self.repo.get(command.cmd_hash)
            if command is None or command.status not in {"queued", "running"}:
                return None
        if command.status != "running":
            return None
        process = self.terminal.processes.get(command.cmd_hash)
        if process is None:
            if command.pid is None and self.terminal._owns_pidless_running(command):
                self.terminal.cancel_requested.add(command.cmd_hash)
                return self._blocker(command, "starting_command")
            if command.pid and self.terminal._pid_exists(command.pid):
                return self._blocker(command, "detached_command")
            changed = await self.repo.finish_running(
                command.cmd_hash,
                "cancelled",
                command.exit_code,
                "persistent.session_fenced",
            )
            return None if changed else self._blocker(command, "running_command")
        ok, _error = await self.terminal.cancel(command)
        return None if ok else self._blocker(command, "running_command")

    async def revoke_session(
        self,
        logical_agent_id: str,
        work_session_id: str,
        session_epoch: int,
        *,
        reason: str,
    ) -> list[dict]:
        commands = await self.repo.persistent_commands(
            logical_agent_id,
            work_session_id=work_session_id,
            session_epoch=session_epoch,
            statuses=("queued", "running"),
        )
        blockers = []
        for command in commands:
            blocker = await self._cancel_or_block(command)
            if blocker:
                blocker["reason"] = reason
                blockers.append(blocker)
        return blockers

    async def blockers_for_session(
        self, logical_agent_id: str, work_session_id: str, session_epoch: int
    ) -> list[dict]:
        commands = await self.repo.persistent_commands(
            logical_agent_id,
            work_session_id=work_session_id,
            session_epoch=session_epoch,
            statuses=("queued", "running"),
        )
        blockers = []
        for command in commands:
            if command.status == "queued":
                kind = "queued_command"
            elif command.cmd_hash in self.terminal.processes:
                kind = "running_command"
            elif command.pid and self.terminal._pid_exists(command.pid):
                kind = "detached_command"
            elif command.pid is None and self.terminal._owns_pidless_running(command):
                kind = "starting_command"
            else:
                kind = "running_command"
            blockers.append(self._blocker(command, kind))
        return blockers

    async def blockers_for_slot(self, logical_agent_id: str) -> list[dict]:
        commands = await self.repo.persistent_commands(
            logical_agent_id, statuses=("queued", "running")
        )
        blockers = []
        for command in commands:
            if command.status == "queued":
                kind = "queued_command"
            elif command.cmd_hash in self.terminal.processes:
                kind = "running_command"
            elif command.pid and self.terminal._pid_exists(command.pid):
                kind = "detached_command"
            else:
                kind = "running_command"
            blockers.append(self._blocker(command, kind))
        return blockers

    async def blockers_for_claim(
        self, namespace: str, task_id: str, logical_agent_id: str
    ) -> list[dict]:
        refs = await self.task_store.persistent_task_commands(namespace, task_id, logical_agent_id)
        blockers = []
        for ref in refs:
            command = await self.repo.get(ref["command_hash"])
            if command is None or command.status not in {"queued", "running"}:
                continue
            if command.status == "queued":
                kind = "queued_command"
            elif command.cmd_hash in self.terminal.processes:
                kind = "running_command"
            elif command.pid and self.terminal._pid_exists(command.pid):
                kind = "detached_command"
            else:
                kind = "running_command"
            blockers.append(self._blocker(command, kind))
        return blockers


class CompositePersistentExecutionFence:
    def __init__(self, local_fence, remote_fence):
        self.local_fence = local_fence
        self.remote_fence = remote_fence

    async def revoke_session(
        self, logical_agent_id, work_session_id, session_epoch, *, reason
    ) -> list[dict]:
        local, remote = await __import__("asyncio").gather(
            self.local_fence.revoke_session(
                logical_agent_id, work_session_id, session_epoch, reason=reason
            ),
            self.remote_fence.revoke_session(
                logical_agent_id, work_session_id, session_epoch, reason=reason
            ),
        )
        return [*local, *remote]

    async def blockers_for_session(
        self, logical_agent_id: str, work_session_id: str, session_epoch: int
    ) -> list[dict]:
        local, remote = await __import__("asyncio").gather(
            self.local_fence.blockers_for_session(
                logical_agent_id, work_session_id, session_epoch
            ),
            self.remote_fence.blockers_for_session(
                logical_agent_id, work_session_id, session_epoch
            ),
        )
        return [*local, *remote]

    async def blockers_for_slot(self, logical_agent_id: str) -> list[dict]:
        return await self.local_fence.blockers_for_slot(logical_agent_id)

    async def blockers_for_claim(self, namespace, task_id, logical_agent_id) -> list[dict]:
        return await self.local_fence.blockers_for_claim(namespace, task_id, logical_agent_id)
