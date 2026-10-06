import sqlite3
from pathlib import Path

from terminal_mcp.deployment.retention import prune_releases, prune_sqlite_backups


def _release(path: Path) -> Path:
    launcher = path / "bin" / "terminal-mcp"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n")
    launcher.chmod(0o755)
    return path


def _backup(path: Path, value: int) -> Path:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE marker(value INTEGER)")
        db.execute("INSERT INTO marker(value) VALUES(?)", (value,))
    return path


def test_prune_releases_keeps_current_and_previous_usable(tmp_path):
    releases = tmp_path / "releases"
    current = _release(releases / "20261006T120000Z")
    previous = _release(releases / "20261006T110000Z")
    stale = _release(releases / "20261006T100000Z")
    broken = releases / "20261006T090000Z"
    broken.mkdir(parents=True)

    removed = prune_releases(releases, current, previous)

    assert current.exists()
    assert previous.exists()
    assert not stale.exists()
    assert not broken.exists()
    assert {path.name for path in removed} == {stale.name, broken.name}


def test_prune_releases_does_not_keep_unusable_previous(tmp_path):
    releases = tmp_path / "releases"
    current = _release(releases / "20261006T120000Z")
    previous = releases / "20261006T110000Z"
    previous.mkdir(parents=True)

    prune_releases(releases, current, previous)

    assert current.exists()
    assert not previous.exists()


def test_prune_sqlite_backups_keeps_latest_two_usable(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    oldest = _backup(backups / "terminal-mcp-20261006T090000Z.sqlite3", 1)
    second = _backup(backups / "terminal-mcp-20261006T100000Z.sqlite3", 2)
    newest = _backup(backups / "terminal-mcp-20261006T110000Z.sqlite3", 3)
    corrupt = backups / "terminal-mcp-20261006T120000Z.sqlite3"
    corrupt.write_bytes(b"not sqlite")

    retained, removed = prune_sqlite_backups(backups, keep=2)

    assert [path.name for path in retained] == [newest.name, second.name]
    assert not corrupt.exists()
    assert not oldest.exists()
    assert {path.name for path in removed} == {corrupt.name, oldest.name}


def test_prune_sqlite_backups_rejects_invalid_keep(tmp_path):
    try:
        prune_sqlite_backups(tmp_path, keep=0)
    except ValueError as exc:
        assert str(exc) == "keep must be at least 1"
    else:
        raise AssertionError("expected ValueError")
