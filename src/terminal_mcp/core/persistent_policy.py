from __future__ import annotations

import asyncio
import os
import stat
import tempfile
from pathlib import Path


class PersistentPolicyError(RuntimeError):
    def __init__(self, code: str, *, blockers: list[dict] | None = None):
        super().__init__(code)
        self.code = code
        self.blockers = blockers or []


class PersistentPolicyController:
    _DURATION_KEY = "TERMINAL_MCP_PERSISTENT_SESSION_DURATION_SEC"
    _WARNING_KEY = "TERMINAL_MCP_PERSISTENT_SESSION_WARNING_AFTER_SEC"
    _ALERT_KEY = "TERMINAL_MCP_PERSISTENT_SESSION_ALERT_AFTER_SEC"
    _REARM_KEY = "TERMINAL_MCP_PERSISTENT_SESSION_REARM_AFTER_SEC"
    _LEGACY_KEY = "TERMINAL_MCP_LEGACY_AGENT_ADMISSION_ENABLED"

    def __init__(self, settings, service, lifecycle):
        self.settings = settings
        self.service = service
        self.lifecycle = lifecycle
        self._lock = asyncio.Lock()
        self._local_policy = self.snapshot()

    def snapshot(self) -> dict:
        return {
            "duration_seconds": int(self.settings.persistent_session_duration_sec),
            "warning_after_seconds": int(self.settings.persistent_session_warning_after_sec),
            "alert_after_seconds": int(self.settings.persistent_session_alert_after_sec),
            "rearm_after_seconds": int(self.settings.persistent_session_rearm_after_sec),
            "legacy_admission_enabled": bool(self.service.legacy_agent_admission_enabled),
        }

    async def update(
        self,
        *,
        duration_seconds: int | None = None,
        warning_after_seconds: int | None = None,
        alert_after_seconds: int | None = None,
        rearm_after_seconds: int | None = None,
        legacy_admission_enabled: bool | None = None,
    ) -> dict:
        if (
            duration_seconds is None
            and warning_after_seconds is None
            and alert_after_seconds is None
            and rearm_after_seconds is None
            and legacy_admission_enabled is None
        ):
            raise PersistentPolicyError("policy_update_empty")

        async with self._lock, self.lifecycle.policy_guard():
            current = self.snapshot()
            duration = (
                current["duration_seconds"] if duration_seconds is None else int(duration_seconds)
            )
            warning = (
                current["warning_after_seconds"]
                if warning_after_seconds is None
                else int(warning_after_seconds)
            )
            alert = (
                current["alert_after_seconds"]
                if alert_after_seconds is None
                else int(alert_after_seconds)
            )
            rearm = (
                current["rearm_after_seconds"]
                if rearm_after_seconds is None
                else int(rearm_after_seconds)
            )
            legacy = (
                current["legacy_admission_enabled"]
                if legacy_admission_enabled is None
                else bool(legacy_admission_enabled)
            )
            self._validate_thresholds(duration, warning, alert)
            if rearm <= 0:
                raise PersistentPolicyError("policy_invalid_rearm")

            timing_changed = (
                duration != current["duration_seconds"]
                or warning != current["warning_after_seconds"]
                or alert != current["alert_after_seconds"]
                or rearm != current["rearm_after_seconds"]
            )
            if timing_changed:
                slots = await self.service.persistent.slot_list()
                blockers = []
                for item in slots.get("slots") or []:
                    slot = item.get("slot") or {}
                    if slot.get("state") in {"armed", "active", "stopping"}:
                        blockers.append(
                            {
                                "logical_agent_id": slot.get("logical_agent_id"),
                                "display_name": slot.get("display_name"),
                                "state": slot.get("state"),
                            }
                        )
                if blockers:
                    raise PersistentPolicyError("policy_in_use", blockers=blockers)

            updates: dict[str, str] = {}
            if timing_changed:
                updates.update(
                    {
                        self._DURATION_KEY: str(duration),
                        self._WARNING_KEY: str(warning),
                        self._ALERT_KEY: str(alert),
                        self._REARM_KEY: str(rearm),
                    }
                )
            if legacy != current["legacy_admission_enabled"]:
                updates[self._LEGACY_KEY] = "true" if legacy else "false"

            if updates:
                try:
                    await asyncio.to_thread(
                        self._persist_env_updates, Path(self.settings.env_file_path), updates
                    )
                except OSError as exc:
                    raise PersistentPolicyError("policy_persist_failed") from exc

            if timing_changed:
                self.settings.persistent_session_duration_sec = duration
                self.settings.persistent_session_warning_after_sec = warning
                self.settings.persistent_session_alert_after_sec = alert
                self.settings.persistent_session_rearm_after_sec = rearm
                self.lifecycle.session_duration_seconds = duration
                self.lifecycle.rearm_delay_seconds = rearm
            if legacy != current["legacy_admission_enabled"]:
                self.settings.legacy_agent_admission_enabled = legacy
                self.service.legacy_agent_admission_enabled = legacy

            result = self.snapshot()
            self._local_policy = dict(result)
            return result

    async def restore_local(self) -> dict:
        """Restore the latest standalone policy after leaving managed Mesh membership."""
        policy = dict(self._local_policy)
        async with self._lock, self.lifecycle.policy_guard():
            self.settings.persistent_session_duration_sec = int(policy["duration_seconds"])
            self.settings.persistent_session_warning_after_sec = int(
                policy["warning_after_seconds"]
            )
            self.settings.persistent_session_alert_after_sec = int(policy["alert_after_seconds"])
            self.settings.persistent_session_rearm_after_sec = int(policy["rearm_after_seconds"])
            self.settings.legacy_agent_admission_enabled = bool(policy["legacy_admission_enabled"])
            self.lifecycle.session_duration_seconds = int(policy["duration_seconds"])
            self.lifecycle.rearm_delay_seconds = int(policy["rearm_after_seconds"])
            self.service.legacy_agent_admission_enabled = bool(policy["legacy_admission_enabled"])
            return self.snapshot()

    async def apply_managed(
        self,
        *,
        duration_seconds: int,
        warning_after_seconds: int,
        alert_after_seconds: int,
        rearm_after_seconds: int,
        legacy_admission_enabled: bool,
    ) -> dict:
        """Apply authoritative managed policy in memory without rewriting bootstrap env."""
        duration = int(duration_seconds)
        warning = int(warning_after_seconds)
        alert = int(alert_after_seconds)
        rearm = int(rearm_after_seconds)
        legacy = bool(legacy_admission_enabled)
        self._validate_thresholds(duration, warning, alert)
        if rearm <= 0:
            raise PersistentPolicyError("policy_invalid_rearm")

        async with self._lock, self.lifecycle.policy_guard():
            self.settings.persistent_session_duration_sec = duration
            self.settings.persistent_session_warning_after_sec = warning
            self.settings.persistent_session_alert_after_sec = alert
            self.settings.persistent_session_rearm_after_sec = rearm
            self.settings.legacy_agent_admission_enabled = legacy
            self.lifecycle.session_duration_seconds = duration
            self.lifecycle.rearm_delay_seconds = rearm
            self.service.legacy_agent_admission_enabled = legacy
            return self.snapshot()

    @staticmethod
    def _validate_thresholds(duration: int, warning: int, alert: int) -> None:
        if duration <= 0:
            raise PersistentPolicyError("policy_invalid_duration")
        if warning <= 0 or warning >= duration:
            raise PersistentPolicyError("policy_invalid_warning")
        if alert <= warning or alert >= duration:
            raise PersistentPolicyError("policy_invalid_alert")

    @staticmethod
    def _persist_env_updates(path: Path, updates: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        remaining = dict(updates)
        output: list[str] = []

        for raw in original.splitlines():
            stripped = raw.strip()
            matched = None
            for key in tuple(remaining):
                candidate = stripped
                if candidate.startswith("export "):
                    candidate = candidate[7:].lstrip()
                if candidate.startswith(f"{key}="):
                    matched = key
                    break
            if matched is None:
                output.append(raw)
                continue
            output.append(f'{matched}="{remaining.pop(matched)}"')

        for key, value in remaining.items():
            output.append(f'{key}="{value}"')

        payload = "\n".join(output)
        if payload:
            payload += "\n"

        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise
