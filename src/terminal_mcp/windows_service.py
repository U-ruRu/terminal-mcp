"""Service entrypoint with an explicit installed configuration and Proactor loop."""

import argparse
import os
from pathlib import Path

import uvicorn

from terminal_mcp.config import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Windows service requires Windows")
    settings = Settings(_env_file=args.env_file)
    if any(settings.mode_for(role) != "oauth" for role in ("mcp", "actions")):
        parser.error("Windows service requires OAuth on both interfaces")
    from terminal_mcp.app import create_app

    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, loop="asyncio")


if __name__ == "__main__":
    main()
