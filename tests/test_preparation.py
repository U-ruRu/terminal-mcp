"""Preparation reuse preserves real generators, storage, isolation and restart checks."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest
from preparation import ImmutablePreparationCache, seed_absent_database, snapshot_empty_database
from pydantic import TypeAdapter, ValidationError

from terminal_mcp.mcp import output_contracts, task_planning
from terminal_mcp.storage.sqlite import SCHEMA_VERSION, SqliteRepository

# Preserve direct access to the real generators before any function-scoped fixture patches.
REAL_SUCCESS = output_contracts._success_schema
REAL_PLANNING = task_planning.task_action_planning_schema


def test_cache_returns_independent_deep_copies_and_is_bounded():
    cache = ImmutablePreparationCache(max_entries=1)
    builds = []

    def build():
        builds.append(1)
        return {"nested": [{"value": 1}]}

    a = cache.get("a", build)
    a["nested"][0]["value"] = 99
    assert cache.get("a", build) == {"nested": [{"value": 1}]}
    assert len(builds) == 1
    cache.get("b", build)
    cache.get("a", build)
    assert cache.stats() == {"hits": 1, "misses": 3, "entries": 1}


def test_concurrent_preparation_runs_real_generator_once():
    cache = ImmutablePreparationCache()
    barrier = Barrier(4)
    builds = []

    def call(_):
        barrier.wait(timeout=3)
        return cache.get("shared", lambda: (builds.append(1), {"values": [1]})[1])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(call, range(4)))
    assert len(builds) == 1
    assert all(value == {"values": [1]} for value in results)
    assert len({id(value["values"]) for value in results}) == 4


def test_generator_errors_are_not_hidden_or_cached():
    cache = ImmutablePreparationCache()
    with pytest.raises(ValueError, match="generator failure"):
        cache.get("x", lambda: (_ for _ in ()).throw(ValueError("generator failure")))
    assert cache.get("x", lambda: {"ok": True}) == {"ok": True}
    assert cache.stats()["misses"] == 1


def test_unhashable_input_uses_real_preparation_each_time():
    cache = ImmutablePreparationCache()
    values = []
    for _ in range(2):
        cache.get([], lambda: values.append(1))
    assert len(values) == 2
    assert cache.stats()["entries"] == 0


def test_schema_cache_matches_uncached_generator_and_keeps_validators_real(
    reuse_immutable_schema_preparation,
):
    annotation = output_contracts.SessionInfo
    expected = REAL_SUCCESS(annotation)
    first = output_contracts._success_schema(annotation)
    assert first == expected
    first["properties"]["public_name"]["type"] = "incorrect"
    assert output_contracts._success_schema(annotation) == expected
    with pytest.raises(ValidationError):
        TypeAdapter(annotation).validate_python({"public_name": ["wrong type"]})


def test_planning_cache_matches_all_actions_and_keeps_argument_specific_copies(
    reuse_immutable_schema_preparation,
):
    for actions in (None, (), ("claim", "update")):
        for excluded in ((), ("code",), ("force", "code")):
            expected = REAL_PLANNING(actions=actions, exclude_fields=excluded)
            result = task_planning.task_action_planning_schema(
                actions=actions, exclude_fields=excluded
            )
            assert result == expected
            result.clear()
            assert (
                task_planning.task_action_planning_schema(actions=actions, exclude_fields=excluded)
                == expected
            )
    with pytest.raises(ValueError, match="Unknown task planning actions"):
        task_planning.task_action_planning_schema(actions=("not-an-action",))


def test_schema_fixture_is_function_scoped():
    assert output_contracts._success_schema is REAL_SUCCESS
    assert task_planning.task_action_planning_schema is REAL_PLANNING


def test_empty_template_has_current_schema_and_no_shared_rows(empty_repository_template, tmp_path):
    first = tmp_path / "a.sqlite3"
    second = tmp_path / "b.sqlite3"
    assert seed_absent_database(first, empty_repository_template, tmp_path)
    assert seed_absent_database(second, empty_repository_template, tmp_path)
    with closing(sqlite3.connect(first)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        db.execute("INSERT INTO commands(hash,cmd,status) VALUES('a','echo isolated','completed')")
        db.commit()
    with closing(sqlite3.connect(second)) as db:
        assert db.execute("SELECT count(*) FROM commands").fetchone()[0] == 0
    assert first.stat().st_mode & 0o777 == 0o600


def test_seed_never_replaces_existing_data_or_escapes_sandbox(tmp_path):
    existing = tmp_path / "existing.sqlite3"
    existing.write_bytes(b"existing-user-data")
    assert seed_absent_database(existing, b"replacement", tmp_path) is False
    assert existing.read_bytes() == b"existing-user-data"
    sandbox = tmp_path / "private"
    sandbox.mkdir()
    outside = tmp_path / "outside.sqlite3"
    assert seed_absent_database(outside, b"replacement", sandbox) is False
    assert not outside.exists()
    link = sandbox / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    assert seed_absent_database(link / "outside.sqlite3", b"replacement", sandbox) is False
    assert not outside.exists()


def test_atomic_seed_handles_competing_creators(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    barrier = Barrier(4)

    def create(index):
        barrier.wait(timeout=3)
        return index, seed_absent_database(path, str(index).encode(), tmp_path)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(create, range(4)))
    winners = [i for i, created in results if created]
    assert len(winners) == 1
    assert path.read_bytes() == str(winners[0]).encode()
    assert not list(tmp_path.glob(".empty-schema-*"))


def test_templates_with_rows_or_wrong_version_are_rejected(tmp_path):
    source = tmp_path / "nonempty.sqlite3"
    with closing(sqlite3.connect(source)) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        db.execute("CREATE TABLE users(value TEXT)")
        db.execute("INSERT INTO users VALUES('must-not-be-shared')")
        db.commit()
    with pytest.raises(AssertionError, match="contains rows"):
        snapshot_empty_database(source, tmp_path / "copy.sqlite3", SCHEMA_VERSION)
    with pytest.raises(AssertionError):
        snapshot_empty_database(source, tmp_path / "copy.sqlite3", SCHEMA_VERSION + 1)


@pytest.mark.asyncio
async def test_seed_still_calls_real_initialize_migrations_and_recovery(
    tmp_path,
    monkeypatch,
    reuse_empty_repository_preparation,
):
    repository = SqliteRepository(tmp_path / "real.sqlite3", tmp_path / "output.sqlite3")
    migrate = repository._migrate
    calls = []

    async def observed_migration(db):
        calls.append(1)
        return await migrate(db)

    monkeypatch.setattr(repository, "_migrate", observed_migration)
    await repository.initialize()
    with closing(sqlite3.connect(repository.path)) as db:
        db.execute("INSERT INTO commands(hash,cmd,status) VALUES('live','echo live','running')")
        db.commit()
    await repository.initialize()
    with closing(sqlite3.connect(repository.path)) as db:
        row = db.execute("SELECT status,error FROM commands WHERE hash='live'").fetchone()
    assert len(calls) == 2
    assert row[0] == "failed"
    assert "startup.recover" in row[1]
    assert isinstance(Path(repository.path), Path)


@pytest.mark.parametrize("fresh", [False, True])
def test_allowlist_activates_real_pytest_fixtures_and_fresh_mode_restores_original(
    tmp_path,
    fresh,
):
    """Exercise real fixture discovery; late usefixtures markers would fail this test."""
    import os
    import shutil
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    suite = tmp_path / "nested-suite"
    suite.mkdir()
    for name in ("conftest.py", "preparation.py"):
        shutil.copyfile(root / "tests" / name, suite / name)
    (suite / "test_agents.py").write_text(
        "from terminal_mcp.storage.sqlite import SqliteRepository\n"
        "ORIGINAL = SqliteRepository.initialize\n"
        "def test_automatic_fixture():\n"
        f"    assert (SqliteRepository.initialize is ORIGINAL) is {fresh!r}\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("TERMINAL_MCP_")
        and not key.startswith("TMCP_")
        and key not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONOPTIMIZE"}
    }
    env["PYTHONPATH"] = str(root / "src")
    command = [sys.executable, "-m", "pytest", "-q", "test_agents.py"]
    if fresh:
        command.append("--fresh-preparation")
    result = subprocess.run(command, cwd=suite, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
