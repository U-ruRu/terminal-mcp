import asyncio
import hashlib
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from terminal_mcp import __main__ as cli
from terminal_mcp.auth import pairing
from terminal_mcp.auth.pairing import PAIRING_SECRET_BYTES, PairingStore
from terminal_mcp.auth.storage import OAuthStore


@pytest.mark.asyncio
async def test_pairing_persists_only_hash_and_is_single_use(tmp_path, monkeypatch):
    db_path = tmp_path / "terminal.sqlite3"
    store = PairingStore(db_path)
    await store.initialize()

    calls = []

    def token_urlsafe(size):
        calls.append(size)
        return "pairing-secret-never-persisted"

    monkeypatch.setattr(pairing.secrets, "token_urlsafe", token_urlsafe)
    secret = await store.create(300, now=1_000)

    assert calls == [PAIRING_SECRET_BYTES]
    assert await store.consume(secret, now=1_001) is True
    assert await store.consume(secret, now=1_002) is False

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT secret_hash,created_at,expires_at,consumed_at FROM console_pairings"
        ).fetchone()
    assert row == (hashlib.sha256(secret.encode()).hexdigest(), 1_000, 1_300, 1_001)
    assert secret.encode() not in db_path.read_bytes()


@pytest.mark.asyncio
async def test_pairing_expiry_is_enforced(tmp_path):
    store = PairingStore(tmp_path / "terminal.sqlite3")
    await store.initialize()
    secret = await store.create(5, now=100)

    assert await store.consume(secret, now=105) is False
    assert await store.consume(secret, now=104) is True


@pytest.mark.asyncio
async def test_pairing_concurrent_consume_has_one_winner(tmp_path):
    store = PairingStore(tmp_path / "terminal.sqlite3")
    await store.initialize()
    secret = await store.create(300)

    results = await asyncio.gather(store.consume(secret), store.consume(secret))

    assert sorted(results) == [False, True]


def test_pair_cli_loads_installed_env_and_keeps_secret_out_of_logs(
    tmp_path, monkeypatch, capsys, caplog
):
    db_path = tmp_path / "terminal.sqlite3"
    env_path = tmp_path / "terminal-mcp.env"
    env_path.write_text(
        f'TERMINAL_MCP_DATABASE_PATH="{db_path}"\n'
        'TERMINAL_MCP_PUBLIC_BASE_URL="https://terminal.example/base/"\n'
    )
    monkeypatch.setenv("TERMINAL_MCP_ENV_FILE_PATH", str(env_path))
    monkeypatch.delenv("TERMINAL_MCP_DATABASE_PATH", raising=False)
    monkeypatch.delenv("TERMINAL_MCP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(pairing.secrets, "token_urlsafe", lambda size: "opaque-pairing-secret")

    url = cli.main(["pair", "--ttl", "42"])
    captured = capsys.readouterr()

    assert captured.out.strip() == url
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "terminal.example"
    assert parsed.path == "/base/connect"
    assert parsed.query == ""
    assert parsed.fragment == "opaque-pairing-secret"
    assert parsed.fragment not in caplog.text

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT secret_hash, expires_at-created_at FROM console_pairings"
        ).fetchone()
    assert row == (hashlib.sha256(parsed.fragment.encode()).hexdigest(), 42)


def test_pair_cli_rejects_non_positive_ttl():
    with pytest.raises(SystemExit) as exc:
        cli.main(["pair", "--ttl", "0"])
    assert exc.value.code == 2


def test_no_arg_cli_preserves_service_start(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli, "Settings", lambda: type("S", (), {"host": "127.0.0.9", "port": 9876})()
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    cli.main([])

    assert calls == [
        (("terminal_mcp.app:app",), {"host": "127.0.0.9", "port": 9876, "reload": False})
    ]


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
    assert reason == ""
    assert exchange is not None

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
    assert set(devices[0]) == {
        "device_id",
        "label",
        "created_at",
        "last_used_at",
        "revoked_at",
    }


def test_devices_cli_lists_safe_metadata_and_revokes(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "terminal.sqlite3"
    env_path = tmp_path / "terminal-mcp.env"
    env_path.write_text(
        f'TERMINAL_MCP_DATABASE_PATH="{db_path}"\n'
        'TERMINAL_MCP_PUBLIC_BASE_URL="https://terminal.example"\n'
    )
    monkeypatch.setenv("TERMINAL_MCP_ENV_FILE_PATH", str(env_path))
    monkeypatch.delenv("TERMINAL_MCP_DATABASE_PATH", raising=False)
    monkeypatch.delenv("TERMINAL_MCP_PUBLIC_BASE_URL", raising=False)

    store = PairingStore(db_path)
    asyncio.run(OAuthStore(db_path).initialize())
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

    listed = cli.main(["devices", "list"])
    output = capsys.readouterr().out
    assert listed[0]["device_id"] == exchange.device_id
    assert listed[0]["label"] == "Phone"
    assert "public-key-never-listed" not in output
    assert exchange.client_id not in output
    assert exchange.refresh_token not in output

    assert cli.main(["devices", "revoke", exchange.device_id]) == 0
    revoked_output = capsys.readouterr().out
    assert revoked_output.strip() == f"revoked {exchange.device_id}"

    listed_after = cli.main(["devices", "list"])
    capsys.readouterr()
    assert listed_after[0]["revoked_at"] is not None

    assert cli.main(["devices", "revoke", exchange.device_id]) == 1
    error = capsys.readouterr().err
    assert "not found or already revoked" in error


def test_devices_cli_is_not_remote_surface():
    source = Path(cli.__file__).read_text()
    assert 'subcommands.add_parser("devices"' in source
