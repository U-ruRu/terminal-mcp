"""Staged import/launcher validation cannot touch inherited live stores."""

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

STORE_KEYS = (
    "DATABASE_PATH",
    "AUTH_DATABASE_PATH",
    "OUTPUT_CACHE_PATH",
    "FLEET_NODE_META_PATH",
    "FLEET_CONTROL_PATH",
    "FLEET_PROJECTION_PATH",
)


def _fixture(tmp_path, failure):
    release = tmp_path / "release"
    (release / "bin").mkdir(parents=True)
    native = release / "lib" / "terminal-mcp-native"
    native.mkdir(parents=True)
    loader = release / "loader"
    loader.write_text('#!/bin/bash\nshift 2\nexec "$@"\n')
    loader.chmod(0o755)
    (native / "loader.path").write_text(str(loader))
    runner = """#!/usr/bin/python3
import json, os, pathlib, sqlite3, sys
release=pathlib.Path(__file__).resolve().parents[1]
phase="import" if "-c" in sys.argv else "launcher"
assert "TERMINAL_MCP_ACCESS_MESH_PROOF_KEY" not in os.environ
assert "TERMINAL_MCP_ACCESS_MESH_ENABLED" not in os.environ
assert "UNRELATED_SENTINEL" not in os.environ
assert "PYTHONPATH" not in os.environ
assert pathlib.Path.cwd()!=release.parent
keys=("DATABASE_PATH","AUTH_DATABASE_PATH","OUTPUT_CACHE_PATH",
"FLEET_NODE_META_PATH","FLEET_CONTROL_PATH","FLEET_PROJECTION_PATH")
paths=[]
for key in keys:
 p=pathlib.Path(os.environ["TERMINAL_MCP_"+key])
 assert p.parent==pathlib.Path.cwd()
 with sqlite3.connect(p) as db: db.execute("CREATE TABLE validation_only(value TEXT)")
 paths.append(str(p))
for key in ("ENV_FILE_PATH","RUNTIME_CONFIG_PATH","LOG_PATH"):
 p=pathlib.Path(os.environ["TERMINAL_MCP_"+key])
 assert p.parent==pathlib.Path.cwd()
 p.write_text("isolated")
with (release.parent/"trace.jsonl").open("a") as out:
 out.write(json.dumps({"phase":phase,"cwd":str(pathlib.Path.cwd()),"paths":paths})+"\\n")
sys.exit(17 if (release/("fail-"+phase)).exists() else 0)
"""
    # Both checks get the same sandbox; tolerate the second creation there.
    runner = runner.replace(
        "CREATE TABLE validation_only", "CREATE TABLE IF NOT EXISTS validation_only"
    )
    for name in ("python", "terminal-mcp"):
        p = release / "bin" / name
        p.write_text(runner)
        p.chmod(0o755)
    if failure:
        (release / ("fail-" + failure)).touch()
    script = (Path(__file__).resolve().parents[1] / "deploy/install.sh").read_text()
    helpers = script.split("staged_validation_command(){", 1)[1].split("release_version(){", 1)[0]
    shell = "staged_validation_command(){" + helpers
    shell += '\nvalidate_staged_runtime "$1"\n'
    live = tmp_path / "live"
    live.mkdir()
    env = dict(os.environ)
    for key in STORE_KEYS:
        p = live / (key.lower() + ".sqlite3")
        with sqlite3.connect(p) as db:
            db.execute("CREATE TABLE original(value TEXT)")
        env["TERMINAL_MCP_" + key] = str(p)
    for key in ("ENV_FILE_PATH", "RUNTIME_CONFIG_PATH", "LOG_PATH"):
        p = live / (key.lower() + ".env")
        p.write_text("live-original")
        env["TERMINAL_MCP_" + key] = str(p)
    env.update(
        TERMINAL_MCP_ACCESS_MESH_ENABLED="true",
        TERMINAL_MCP_ACCESS_MESH_PROOF_KEY="fixture-secret-must-never-be-logged",
        UNRELATED_SENTINEL="fixture-unrelated-environment",
        PYTHONPATH=str(live),
    )
    (tmp_path / ".env").write_text("TERMINAL_MCP_DATABASE_PATH=" + str(live / "dotenv.db"))
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in live.iterdir()}
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", shell, "validation", str(release)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in live.iterdir()}
    assert before == after
    assert not list(release.glob(".runtime-validation.*"))
    assert "fixture-secret-must-never-be-logged" not in result.stdout + result.stderr
    trace = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert all(not Path(row["cwd"]).exists() for row in trace)
    return result, trace


@pytest.mark.parametrize("failure", [None, "import", "launcher"])
def test_staged_validation_isolated_stores_and_cleanup_on_every_exit(tmp_path, failure):
    result, trace = _fixture(tmp_path, failure)
    assert result.returncode == (0 if failure is None else 1)
    assert [row["phase"] for row in trace] == (
        ["import"] if failure == "import" else ["import", "launcher"]
    )
    if failure == "import":
        assert "Staged release runtime import check failed" in result.stderr
    elif failure == "launcher":
        assert "Staged Terminal MCP launcher check failed" in result.stderr
    else:
        assert not result.stderr


def test_isolated_validation_still_precedes_backup_and_activation():
    script = (Path(__file__).resolve().parents[1] / "deploy/install.sh").read_text()
    stage = script.split("stage(){", 1)[1].split("staged_validation_command(){", 1)[0]
    assert stage.index('build_sqlite_runtime "$STAGED_RELEASE"') < stage.index(
        'validate_staged_runtime "$STAGED_RELEASE"'
    )
    update = next(line for line in script.splitlines() if line.startswith(" update)"))
    assert (
        update.index("stage") < update.index('backup "$STAGED_RELEASE"') < update.index("activate")
    )
