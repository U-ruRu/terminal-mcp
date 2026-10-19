"""SCM observation and a SYSTEM Task Scheduler worker for installed Windows nodes."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

from terminal_mcp.config import Settings
from terminal_mcp.deployment.runtime_control import RuntimeControl, RuntimeControlError

TASK_NAME = "TerminalMCP-Maintenance"
TUNNEL_NAME = re.compile(r"^[A-Za-z0-9_=+.-]{1,32}$")


@contextlib.contextmanager
def scheduler():
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        yield service
    finally:
        pythoncom.CoUninitialize()


def configure_task(root: Path, python: Path):
    root, python = root.resolve(), python.resolve()
    if not python.is_relative_to(root / "releases") or python.name.lower() != "python.exe":
        raise ValueError("maintenance runtime requires an installed release")
    with scheduler() as service:
        _register_task(service, root, python)


def _register_task(service, root, python):
    task = service.NewTask(0)
    task.RegistrationInfo.Description = "Terminal MCP local service maintenance"
    task.Principal.UserId = "SYSTEM"
    task.Principal.LogonType = 5
    task.Principal.RunLevel = 1
    task.Settings.Enabled = True
    task.Settings.AllowDemandStart = True
    task.Settings.DisallowStartIfOnBatteries = False
    task.Settings.StopIfGoingOnBatteries = False
    task.Settings.MultipleInstances = 1
    task.Settings.ExecutionTimeLimit = "PT3M"
    action = task.Actions.Create(0)
    action.Path = str(python)
    action.Arguments = subprocess.list2cmdline(
        [
            "-m",
            "terminal_mcp.deployment.windows_runtime",
            "work",
            "--root",
            str(root),
        ]
    )
    action.WorkingDirectory = str(root / "work")
    service.GetFolder("\\").RegisterTaskDefinition(
        TASK_NAME,
        task,
        6,
        "SYSTEM",
        None,
        5,
        "D:P(A;;GA;;;SY)(A;;GA;;;BA)",
    )


class WindowsRuntime:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.windows_service_root
        runtime = self.root / "service/runtime.json"
        configured = json.loads(runtime.read_text(encoding="utf-8")) if runtime.exists() else {}
        name = settings.windows_tunnel_name or configured.get("tunnel_name", "")
        if name and not TUNNEL_NAME.fullmatch(name):
            raise ValueError("invalid Windows tunnel name")
        self.services = {"terminal": "TerminalMCP"}
        if name:
            self.services["tunnel"] = "WireGuardTunnel$" + name

    def _state(self, name):
        import pywintypes
        import win32service

        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        service = None
        try:
            service = win32service.OpenService(manager, name, win32service.SERVICE_QUERY_STATUS)
            state = win32service.QueryServiceStatusEx(service)
            return {
                "state": {
                    win32service.SERVICE_RUNNING: "running",
                    win32service.SERVICE_STOPPED: "stopped",
                }.get(state["CurrentState"], "transitioning"),
                "pid": state["ProcessId"],
            }
        except pywintypes.error as exc:
            if exc.winerror == 1060:
                return {"state": "missing", "pid": None}
            raise
        finally:
            if service is not None:
                win32service.CloseServiceHandle(service)
            win32service.CloseServiceHandle(manager)

    def snapshot(self):
        units = {key: self._state(name) for key, name in self.services.items()}
        try:
            with scheduler() as service:
                configured = bool(service.GetFolder("\\").GetTask(TASK_NAME).Enabled)
        except Exception:
            configured = False
        return {
            "platform": "windows",
            "supported": configured,
            "restart_targets": [
                key for key, value in units.items() if configured and value["state"] != "missing"
            ],
            "services": units,
        }

    def launch(self):
        with scheduler() as service:
            service.GetFolder("\\").GetTask(TASK_NAME).Run(None)

    def restart(self, target):
        import win32service
        import win32serviceutil

        if target not in self.services:
            raise RuntimeControlError("invalid_runtime_target")
        name = self.services[target]
        state = self._state(name)["state"]
        if state == "missing":
            raise RuntimeControlError("runtime_target_unavailable")
        if state != "stopped":
            win32serviceutil.StopService(name)
            win32serviceutil.WaitForServiceStatus(name, win32service.SERVICE_STOPPED, 45)
        win32serviceutil.StartService(name)
        win32serviceutil.WaitForServiceStatus(name, win32service.SERVICE_RUNNING, 45)
        host = self.settings.host
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1" if host == "0.0.0.0" else "::1"
        if ":" in host:
            host = "[" + host + "]"
        deadline = time.monotonic() + 60
        with httpx.Client(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                try:
                    response = client.get(f"http://{host}:{self.settings.port}/health/ready")
                    if response.status_code == 200 and response.json().get("ready"):
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(0.5)
        raise RuntimeControlError("runtime_readiness_timeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("configure", "remove", "work"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--tunnel-name")
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Windows runtime management requires Windows")
    if args.action == "configure":
        if args.tunnel_name is not None:
            from terminal_mcp.deployment.runtime_control import atomic_json

            if args.tunnel_name and not TUNNEL_NAME.fullmatch(args.tunnel_name):
                parser.error("invalid Windows tunnel name")
            atomic_json(args.root / "service/runtime.json", {"tunnel_name": args.tunnel_name})
        configure_task(args.root, args.python or Path(sys.executable))
    elif args.action == "remove":
        with scheduler() as service:
            service.GetFolder("\\").DeleteTask(TASK_NAME, 0)
    else:
        settings = Settings(
            _env_file=args.root / "config/terminal-mcp.env", windows_service_root=args.root
        )
        # Let the initiating HTTP response finish and release the deployment handle.
        time.sleep(2)
        deadline = time.monotonic() + 15
        while True:
            try:
                RuntimeControl(args.root, WindowsRuntime(settings)).run_pending()
                return
            except RuntimeControlError as exc:
                if str(exc) != "runtime_busy" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)


if __name__ == "__main__":
    main()
