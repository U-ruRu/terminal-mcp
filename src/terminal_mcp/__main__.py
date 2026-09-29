import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn

from terminal_mcp.auth.pairing import PAIRING_DEFAULT_TTL_SEC, PairingStore
from terminal_mcp.config import Settings


def _local_settings() -> Settings:
    """Load the installed instance env when CLI is invoked from a shell."""

    env_path = Path(
        os.environ.get("TERMINAL_MCP_ENV_FILE_PATH", "/etc/terminal-mcp/terminal-mcp.env")
    )
    if env_path.is_file():
        return Settings(_env_file=env_path)
    return Settings()


def _pairing_url(
    console_public_base_url: str,
    public_base_url: str,
    secret: str,
    display_name: str | None = None,
) -> str:
    target = urlsplit(public_base_url)
    target_origin = f"{target.scheme}://{target.netloc}"
    preferred_name = display_name.strip() if display_name is not None else ""
    name = (preferred_name or target.hostname or target.netloc).strip()[:120]
    payload = {"v": 1, "server": target_origin, "name": name, "secret": secret}
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{console_public_base_url.rstrip('/')}/connect#{encoded}"


async def _pairing_store() -> PairingStore:
    settings = _local_settings()
    store = PairingStore(settings.database_path)
    await store.initialize()
    return store


async def _issue_pairing(ttl_seconds: int, display_name: str | None = None) -> str:
    settings = _local_settings()
    store = await _pairing_store()
    secret = await store.create(ttl_seconds)
    return _pairing_url(
        settings.console_public_base_url, settings.public_base_url, secret, display_name
    )


async def _list_devices() -> list[dict]:
    return await (await _pairing_store()).list_devices()


async def _revoke_device(device_id: str) -> bool:
    return await (await _pairing_store()).revoke_device(device_id)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="terminal-mcp")
    subcommands = parser.add_subparsers(dest="command", required=True)
    pair = subcommands.add_parser("pair", help="create a one-time local Console pairing URL")
    pair.add_argument(
        "--ttl",
        type=int,
        default=PAIRING_DEFAULT_TTL_SEC,
        metavar="SECONDS",
        help=f"pairing lifetime in seconds (default: {PAIRING_DEFAULT_TTL_SEC})",
    )
    pair.add_argument(
        "--name",
        default=None,
        metavar="DISPLAY_NAME",
        help="suggested server display name embedded in the pairing link",
    )
    devices = subcommands.add_parser("devices", help="manage paired Console devices")
    device_commands = devices.add_subparsers(dest="device_command", required=True)
    device_commands.add_parser("list", help="list paired devices")
    revoke = device_commands.add_parser("revoke", help="revoke one paired device")
    revoke.add_argument("device_id", help="device id shown by 'terminal-mcp devices list'")
    return parser


def _run_server() -> None:
    settings = Settings()
    uvicorn.run("terminal_mcp.app:app", host=settings.host, port=settings.port, reload=False)


def main(argv: list[str] | None = None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        _run_server()
        return None

    parsed = _parser().parse_args(args)
    if parsed.command == "pair":
        if parsed.ttl <= 0:
            _parser().error("--ttl must be a positive number of seconds")
        url = asyncio.run(_issue_pairing(parsed.ttl, parsed.name))
        print(url)
        return 0
    if parsed.command == "devices":
        if parsed.device_command == "list":
            devices = asyncio.run(_list_devices())
            print(json.dumps(devices, separators=(",", ":"), ensure_ascii=False))
            return 0
        if parsed.device_command == "revoke":
            revoked = asyncio.run(_revoke_device(parsed.device_id))
            if revoked:
                print(f"revoked {parsed.device_id}")
                return 0
            print(f"device not found or already revoked: {parsed.device_id}", file=sys.stderr)
            return 1
        raise AssertionError(f"unhandled device command: {parsed.device_command}")
    raise AssertionError(f"unhandled command: {parsed.command}")


if __name__ == "__main__":
    raise SystemExit(main())
