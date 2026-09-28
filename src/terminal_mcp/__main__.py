import argparse
import asyncio
import os
import sys
from pathlib import Path

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


def _pairing_url(public_base_url: str, secret: str) -> str:
    return f"{public_base_url.rstrip('/')}/connect#{secret}"


async def _issue_pairing(ttl_seconds: int) -> str:
    settings = _local_settings()
    store = PairingStore(settings.database_path)
    await store.initialize()
    secret = await store.create(ttl_seconds)
    return _pairing_url(settings.public_base_url, secret)


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
        url = asyncio.run(_issue_pairing(parsed.ttl))
        print(url)
        return url
    raise AssertionError(f"unhandled command: {parsed.command}")


if __name__ == "__main__":
    main()
