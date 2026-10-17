import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from terminal_mcp import __main__ as cli
from terminal_mcp.auth import pairing
from terminal_mcp.auth.pairing import PairingStore
from terminal_mcp.auth.storage import OAuthStore


async def test_pairing_issue_persists_secret_hash(tmp_path):
    db_path = tmp_path / "auth.sqlite3"
    store = PairingStore(db_path)
    await store.initialize()

    secret = await store.create(300, now=1_000)

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT secret_hash,created_at,expires_at,consumed_at FROM console_pairings"
        ).fetchone()
    assert row == (hashlib.sha256(secret.encode()).hexdigest(), 1_000, 1_300, None)


async def test_pairing_exchange_rejects_expired_secret(tmp_path):
    path = tmp_path / "auth.sqlite3"
    store = PairingStore(path)
    await OAuthStore(path).initialize()
    await store.initialize()
    secret = await store.create(5, now=100)

    exchange, reason = await store.exchange(
        secret, "public-key-" + "x" * 64, "Phone", "terminal:read", 3600, now=105
    )

    assert exchange is None
    assert reason == "expired"


def test_pair_cli_embeds_explicit_display_name(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "terminal.sqlite3"
    auth_path = tmp_path / "auth.sqlite3"
    env_path = tmp_path / "terminal-mcp.env"
    env_path.write_text(
        f'TERMINAL_MCP_DATABASE_PATH="{db_path}"\n'
        f'TERMINAL_MCP_AUTH_DATABASE_PATH="{auth_path}"\n'
        'TERMINAL_MCP_PUBLIC_BASE_URL="https://terminal.example"\n'
        'TERMINAL_MCP_CONSOLE_PUBLIC_BASE_URL="https://terminal-console.solvenger.app"\n'
    )
    monkeypatch.setenv("TERMINAL_MCP_ENV_FILE_PATH", str(env_path))
    monkeypatch.delenv("TERMINAL_MCP_DATABASE_PATH", raising=False)
    monkeypatch.delenv("TERMINAL_MCP_AUTH_DATABASE_PATH", raising=False)
    monkeypatch.delenv("TERMINAL_MCP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("TERMINAL_MCP_CONSOLE_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(pairing.secrets, "token_urlsafe", lambda size: "explicit-name-secret-value")

    assert cli.main(["pair", "--name", "Secondary Test"]) == 0
    url = capsys.readouterr().out.strip()
    fragment = urlsplit(url).fragment
    padded = fragment + "=" * ((4 - len(fragment) % 4) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    assert payload["name"] == "Secondary Test"
    assert payload["server"] == "https://terminal.example"
    assert auth_path.exists()


def _entrypoint_env(tmp_path):
    db_path = tmp_path / "terminal.sqlite3"
    auth_path = tmp_path / "auth.sqlite3"
    env_path = tmp_path / "terminal-mcp.env"
    env_path.write_text(
        chr(10).join(
            [
                f'TERMINAL_MCP_DATABASE_PATH="{db_path}"',
                f'TERMINAL_MCP_AUTH_DATABASE_PATH="{auth_path}"',
                'TERMINAL_MCP_PUBLIC_BASE_URL="https://terminal.example/base/"',
                'TERMINAL_MCP_CONSOLE_PUBLIC_BASE_URL="https://terminal-console.solvenger.app"',
            ]
        )
        + chr(10)
    )
    env = os.environ.copy()
    env["TERMINAL_MCP_ENV_FILE_PATH"] = str(env_path)
    env.pop("TERMINAL_MCP_DATABASE_PATH", None)
    env.pop("TERMINAL_MCP_AUTH_DATABASE_PATH", None)
    env.pop("TERMINAL_MCP_PUBLIC_BASE_URL", None)
    env.pop("TERMINAL_MCP_CONSOLE_PUBLIC_BASE_URL", None)
    source = str(Path(__file__).resolve().parents[1] / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = source + (os.pathsep + existing if existing else "")
    return env


def _run_console_script(args, env):
    command = (
        "import json,sys; "
        "from terminal_mcp.__main__ import main; "
        "sys.exit(main(json.loads(sys.argv[1])))"
    )
    return subprocess.run(
        [sys.executable, "-c", command, json.dumps(args)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.asyncio
async def test_list_devices_returns_only_safe_metadata(tmp_path):
    db_path = tmp_path / "terminal.sqlite3"
    store = PairingStore(db_path)
    await OAuthStore(db_path).initialize()
    await store.initialize()
    secret = await store.create(300, now=100)
    exchange, reason = await store.exchange(
        secret,
        "private-looking-public-key-material-" + ("x" * 64),
        "Laptop",
        "terminal:read",
        3600,
        now=101,
    )
    devices = await store.list_devices()

    assert devices == [
        {
            "device_id": exchange.device_id,
            "label": "Laptop",
            "created_at": 101,
            "last_used_at": 101,
            "revoked_at": None,
        }
    ]


def test_devices_cli_revokes_selected_device(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "terminal.sqlite3"
    auth_path = tmp_path / "auth.sqlite3"
    env_path = tmp_path / "terminal-mcp.env"
    env_path.write_text(
        f'TERMINAL_MCP_DATABASE_PATH="{db_path}"\n'
        f'TERMINAL_MCP_AUTH_DATABASE_PATH="{auth_path}"\n'
        'TERMINAL_MCP_PUBLIC_BASE_URL="https://terminal.example"\n'
    )
    monkeypatch.setenv("TERMINAL_MCP_ENV_FILE_PATH", str(env_path))
    monkeypatch.delenv("TERMINAL_MCP_DATABASE_PATH", raising=False)
    monkeypatch.delenv("TERMINAL_MCP_AUTH_DATABASE_PATH", raising=False)
    monkeypatch.delenv("TERMINAL_MCP_PUBLIC_BASE_URL", raising=False)

    store = PairingStore(auth_path)
    asyncio.run(OAuthStore(auth_path).initialize())
    asyncio.run(store.initialize())
    secret = asyncio.run(store.create())
    exchange, _ = asyncio.run(
        store.exchange(
            secret,
            "public-key-never-listed-" + ("x" * 64),
            "Phone",
            "terminal:read",
            3600,
        )
    )
    assert exchange is not None

    assert cli.main(["devices", "revoke", exchange.device_id]) == 0
    revoked_output = capsys.readouterr().out
    assert revoked_output.strip() == f"revoked {exchange.device_id}"

    devices = asyncio.run(store.list_devices())
    assert devices[0]["device_id"] == exchange.device_id
    assert devices[0]["revoked_at"] is not None
