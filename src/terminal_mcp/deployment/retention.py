from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from pathlib import Path

BACKUP_GLOB = "terminal-mcp-*.sqlite3"


def _usable_release(path: Path) -> bool:
    launcher = path / "bin" / "terminal-mcp"
    return path.is_dir() and launcher.is_file() and os.access(launcher, os.X_OK)


def prune_releases(
    releases_dir: Path | str,
    current: Path | str,
    previous: Path | str | None = None,
) -> list[Path]:
    releases = Path(releases_dir)
    current_path = Path(current).resolve()
    keep = {current_path}
    if previous:
        previous_path = Path(previous)
        if _usable_release(previous_path):
            keep.add(previous_path.resolve())

    removed: list[Path] = []
    if not releases.exists():
        return removed
    for candidate in releases.iterdir():
        if candidate.resolve() in keep:
            continue
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
        elif candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            continue
        removed.append(candidate)
    return removed


def _usable_sqlite_backup(path: Path) -> bool:
    try:
        with sqlite3.connect(path) as db:
            return db.execute("PRAGMA quick_check").fetchone() == ("ok",)
    except (OSError, sqlite3.Error):
        return False


def prune_sqlite_backups(
    backup_dir: Path | str,
    *,
    keep: int = 2,
) -> tuple[list[Path], list[Path]]:
    if keep < 1:
        raise ValueError("keep must be at least 1")
    directory = Path(backup_dir)
    if not directory.exists():
        return [], []

    retained: list[Path] = []
    removed: list[Path] = []
    for path in sorted(directory.glob(BACKUP_GLOB), key=lambda item: item.name, reverse=True):
        if _usable_sqlite_backup(path) and len(retained) < keep:
            retained.append(path)
            continue
        path.unlink(missing_ok=True)
        removed.append(path)
    return retained, removed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Terminal MCP installer retention cleanup")
    subparsers = parser.add_subparsers(dest="action", required=True)

    releases = subparsers.add_parser("releases")
    releases.add_argument("releases_dir")
    releases.add_argument("current")
    releases.add_argument("previous", nargs="?")

    backups = subparsers.add_parser("backups")
    backups.add_argument("backup_dir")
    backups.add_argument("--keep", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "releases":
        prune_releases(args.releases_dir, args.current, args.previous)
    else:
        prune_sqlite_backups(args.backup_dir, keep=args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
