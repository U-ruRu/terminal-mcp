"""Independent real-process migration checks: no production runtime or systemd access."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def isolated_process(tmp_path: Path):
    data = tmp_path / "node"
    data.mkdir(mode=0o700)
    cwd = data / "cwd"
    cwd.mkdir()
    paths = {
        "DATABASE_PATH": "runtime.sqlite3",
        "AUTH_DATABASE_PATH": "auth.sqlite3",
        "OUTPUT_CACHE_PATH": "output.sqlite3",
        "ENV_FILE_PATH": "node.env",
        "EXECUTOR_SOCKET_PATH": "executor.sock",
        "FLEET_PEER_TRANSPORTS_PATH": "peers.json",
        "MESH_VPN_STATE_DIR": "mesh-vpn",
        "CWD": "cwd",
        "LOG_PATH": "test.log",
        "RUNTIME_CONFIG_PATH": "runtime.env",
        "FLEET_CONTROL_PATH": "control.sqlite3",
        "FLEET_PROJECTION_PATH": "projection.sqlite3",
        "FLEET_NODE_META_PATH": "node-meta.sqlite3",
    }
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TERMINAL_MCP_") and not k.startswith("TMCP_")
    }
    env.update({"TERMINAL_MCP_" + name: str(data / value) for name, value in paths.items()})
    env.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TERMINAL_MCP_PUBLIC_BASE_URL": "https://migration.example",
            "TERMINAL_MCP_FLEET_ENABLED": "false",
            "TERMINAL_MCP_ACCESS_MESH_ENABLED": "false",
        }
    )
    stores = {
        key: str(data / name)
        for key, name in {
            "runtime": "runtime.sqlite3",
            "auth": "auth.sqlite3",
            "output": "output.sqlite3",
            "control": "control.sqlite3",
            "projection": "projection.sqlite3",
            "node_meta": "node-meta.sqlite3",
        }.items()
    }
    return data, env, {"stores": stores}


def migrate_in_subprocess(data, env, profile, backup):
    script = """
import asyncio,json,sys
from pathlib import Path
from terminal_mcp.deployment.release import migrate_stores
profile=json.loads(sys.argv[1])
result=asyncio.run(migrate_stores(profile,Path(sys.argv[2])))
print(json.dumps({'completed':True,'schemas':result}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, json.dumps(profile), str(backup)],
        cwd=data,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


def test_actual_fresh_migration_and_repeat_preserve_real_stores(tmp_path):
    from terminal_mcp.deployment.update import snapshot
    from terminal_mcp.storage.sqlite import SCHEMA_VERSION

    data, env, profile = isolated_process(tmp_path)
    backup = data / "before-fresh"
    snapshot(profile, backup, [])
    first = migrate_in_subprocess(data, env, profile, backup)
    assert first["completed"] is True
    assert first["schemas"]["runtime"] == SCHEMA_VERSION
    assert first["schemas"]["auth"] > 0
    with sqlite3.connect(profile["stores"]["runtime"]) as db:
        db.execute(
            "CREATE TABLE deploy_preservation_probe "
            "(id TEXT PRIMARY KEY, expires_at TEXT, payload BLOB)"
        )
        db.execute(
            "INSERT INTO deploy_preservation_probe VALUES (?, ?, ?)",
            ("live-existing-session", "2026-10-13T08:00:00Z", b"synthetic-private-state"),
        )
    protected = [p for p in data.iterdir() if p.name.endswith((".access-key", ".identity-anchor"))]
    assert protected, "Actual fresh identity initialization must produce protected identity assets"
    identity_before = {p.name: p.read_bytes() for p in protected}
    backup2 = data / "before-repeat"
    snapshot(profile, backup2, [])
    second = migrate_in_subprocess(data, env, profile, backup2)
    assert second["schemas"] == first["schemas"]
    with sqlite3.connect(profile["stores"]["runtime"]) as db:
        assert db.execute("SELECT * FROM deploy_preservation_probe").fetchall() == [
            ("live-existing-session", "2026-10-13T08:00:00Z", b"synthetic-private-state")
        ]
    assert {p.name: p.read_bytes() for p in protected} == identity_before
