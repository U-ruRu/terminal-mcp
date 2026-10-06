import sqlite3
import struct
import sys
from pathlib import Path

import pytest

from terminal_mcp.fleet.writer_trace import (
    FanotifyWatcher,
    WriterTraceUnavailable,
    classify_header,
    trace_command,
    validate_sandbox_target,
)

INCIDENT_WAL_HEADER = struct.pack(
    ">IIIIIIII",
    0x377F0682,
    3_007_000,
    4096,
    12,
    0xC0E2A963,
    0x833FD399,
    0x3890E5D3,
    0x8A4BFCFB,
)


def sqlite_main(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE witness(value TEXT)")
        db.commit()


def test_header_classifier_distinguishes_main_wal_and_unknown():
    assert classify_header(b"SQLite format 3\x00" + b"\x00" * 16) == "sqlite_main"
    assert classify_header(INCIDENT_WAL_HEADER) == "wal_header"
    assert classify_header(b"not sqlite") == "unknown"


def test_writer_trace_refuses_non_temporary_or_misnamed_targets(tmp_path):
    target = tmp_path / "fleet-control.sqlite3"
    sqlite_main(target)
    root, resolved = validate_sandbox_target(tmp_path, target)
    assert root == tmp_path.resolve()
    assert resolved == target.resolve()

    wrong_name = tmp_path / "runtime.sqlite3"
    sqlite_main(wrong_name)
    with pytest.raises(ValueError, match="fleet-control.sqlite3"):
        validate_sandbox_target(tmp_path, wrong_name)
    with pytest.raises(ValueError, match="/tmp or /var/tmp"):
        validate_sandbox_target(Path("/"), Path("/etc/hosts"))


def test_fanotify_attributes_external_32_byte_wal_header_writer(tmp_path):
    target = tmp_path / "fleet-control.sqlite3"
    sqlite_main(target)
    try:
        with FanotifyWatcher(tmp_path, target):
            pass
    except WriterTraceUnavailable as exc:
        pytest.skip(f"fanotify unavailable: {exc}")

    script = (
        "from pathlib import Path; import sys; "
        "p=Path(sys.argv[1]); payload=bytes.fromhex(sys.argv[2]); "
        "f=p.open('r+b'); f.write(payload); f.flush(); f.close()"
    )
    result = trace_command(
        tmp_path,
        target,
        [sys.executable, "-c", script, str(target), INCIDENT_WAL_HEADER.hex()],
    )

    assert result.returncode == 0
    assert result.initial_header_kind == "sqlite_main"
    assert result.final_header_kind == "wal_header"
    assert target.read_bytes()[:32] == INCIDENT_WAL_HEADER
    assert result.events
    assert any(event.relative_path == "fleet-control.sqlite3" for event in result.events)
    assert any(event.header_kind == "wal_header" for event in result.events)
    assert all(event.pid > 0 for event in result.events)
    assert all(event.comm for event in result.events)
    assert all(event.executable for event in result.events)


def test_repository_has_no_raw_fleet_control_wal_to_main_copy_path():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for base in ("src", "deploy", "scripts"):
        for path in (root / base).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".sh", ".service", ".timer"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "fleet-control.sqlite3-wal" in text:
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
