"""Explicit preparation reuse for behavioral tests. No deselection, skips or weaker asserts.

Schema/catalog/generator tests always call the real generators. Migration, startup recovery,
locking, durability and fresh-install tests retain their original fresh preparation paths.
Every seeded behavioral database still executes the complete real initialize() method.
"""

from __future__ import annotations

import asyncio
import json
import os
from functools import wraps
from pathlib import Path

import pytest
from preparation import (
    ImmutablePreparationCache,
    immutable_schema_models,
    seed_absent_database,
    snapshot_empty_database,
)

# These files test behavior over unchanged schema metadata, not schema generation itself.
SCHEMA_PREPARATION_MODULES = frozenset(
    {
        "test_access_slot_inheritance_http.py",
        "test_android_slot_public_paths.py",
        "test_console_events.py",
        "test_http_auth.py",
        "test_mcp_events_fleet_lifecycle.py",
        "test_mcp_events_session_number.py",
        "test_task_qa_public_state.py",
    }
)
# Empty tables only. No existing database, migration fixture or user row is replaced.
EMPTY_DB_PREPARATION_MODULES = frozenset(
    {
        "test_agent_intent_freshness.py",
        "test_compact_close_independent.py",
        "test_compact_response_independent.py",
        "test_task_atomic_ownership_v2.py",
        "test_task_committed_receipts_v2.py",
        "test_task_create_request_receipt.py",
        "test_task_create_signature_immutable.py",
        "test_task_history_cursor_stability.py",
        "test_task_mesh_policy_receipts_v2.py",
        "test_task_ops_simple_contract.py",
        "test_task_ownership_state_v2.py",
        "test_task_projection_core.py",
        "test_task_qa_public_state.py",
        "test_task_revision_side_effects.py",
        "test_task_text_validation.py",
    }
)


def pytest_sessionstart(session):
    """Prevent a live Terminal MCP service environment from changing test fixtures.

    Integration tests set their own configuration or use monkeypatch. Reading the
    installed service's env would silently replace their isolated Settings.
    Only the pytest child process is affected, never the running server.
    Set TMCP_TEST_INHERIT_SERVER_ENV=1 to explicitly test inherited configuration.
    """
    del session
    if os.environ.get("TMCP_TEST_INHERIT_SERVER_ENV") == "1":
        return
    for key in tuple(os.environ):
        if key.startswith("TERMINAL_MCP_"):
            os.environ.pop(key, None)


def pytest_addoption(parser):
    parser.addoption(
        "--fresh-preparation",
        action="store_true",
        default=False,
        help="Disable optional immutable preparation reuse for comparison/diagnosis",
    )


@pytest.fixture(autouse=True)
def activate_preparation(request):
    """Resolve function-scoped preparation before ordinary test fixtures.

    Adding a usefixtures marker after collection does not rebuild pytest's fixture
    closure. Explicit getfixturevalue here ensures the real allowlist is activated.
    """
    if request.config.getoption("--fresh-preparation"):
        return
    name = Path(request.node.path).name
    if name in SCHEMA_PREPARATION_MODULES:
        request.getfixturevalue("reuse_immutable_schema_preparation")
    if name in EMPTY_DB_PREPARATION_MODULES:
        request.getfixturevalue("reuse_empty_repository_preparation")


@pytest.fixture(scope="session")
def immutable_schema_cache(request):
    cache = ImmutablePreparationCache()
    request.config._tmcp_schema_cache = cache
    return cache


@pytest.fixture
def reuse_immutable_schema_preparation(monkeypatch, immutable_schema_cache):
    from terminal_mcp.mcp import output_contracts, task_planning

    success = output_contracts._success_schema
    planning = task_planning.task_action_planning_schema

    @wraps(success)
    def copied_success(annotation):
        return immutable_schema_cache.get((success, annotation), lambda: success(annotation))

    @wraps(planning)
    def copied_planning(*, actions=None, exclude_fields=()):
        # The original generator treats both collections as sets; preserve None vs empty.
        actions = None if actions is None else tuple(actions)
        exclude_fields = tuple(exclude_fields)
        key = (planning, None if actions is None else frozenset(actions), frozenset(exclude_fields))
        return immutable_schema_cache.get(
            key, lambda: planning(actions=actions, exclude_fields=exclude_fields)
        )

    # Capture all inherited implementations before changing any class so subclasses
    # cannot accidentally wrap a parent that was patched earlier in the loop.
    methods = [(model, model.model_json_schema.__func__) for model in immutable_schema_models()]

    def copy_model_metadata(original):
        @wraps(original)
        def copied(cls, *args, **kwargs):
            key = (original, cls, args, tuple(sorted(kwargs.items())))
            return immutable_schema_cache.get(key, lambda: original(cls, *args, **kwargs))

        return classmethod(copied)

    for model, original in methods:
        monkeypatch.setattr(model, "model_json_schema", copy_model_metadata(original))

    monkeypatch.setattr(output_contracts, "_success_schema", copied_success)
    monkeypatch.setattr(task_planning, "task_action_planning_schema", copied_planning)


@pytest.fixture(scope="session")
def empty_repository_template(tmp_path_factory):
    from terminal_mcp.storage.sqlite import SCHEMA_VERSION, SqliteRepository

    root = tmp_path_factory.mktemp("empty-schema-template")
    source = root / "source.sqlite3"
    repository = SqliteRepository(source, root / "output.sqlite3")
    asyncio.run(repository.initialize())
    return snapshot_empty_database(source, root / "sealed.sqlite3", SCHEMA_VERSION)


@pytest.fixture
def reuse_empty_repository_preparation(
    monkeypatch, tmp_path_factory, empty_repository_template, request
):
    from terminal_mcp.storage.sqlite import SqliteRepository

    initialize = SqliteRepository.initialize
    sandbox = tmp_path_factory.getbasetemp()

    @wraps(initialize)
    async def prepared_initialize(self, *args, **kwargs):
        seeded = seed_absent_database(Path(self.path), empty_repository_template, sandbox)
        if seeded:
            request.config._tmcp_seeded_databases = (
                getattr(request.config, "_tmcp_seeded_databases", 0) + 1
            )
        # Preserve validation, recovery, real connections/transactions and every migration check.
        return await initialize(self, *args, **kwargs)

    monkeypatch.setattr(SqliteRepository, "initialize", prepared_initialize)


def pytest_terminal_summary(terminalreporter):
    config = terminalreporter.config
    cache = getattr(config, "_tmcp_schema_cache", None)
    stats = {
        "enabled": not config.getoption("--fresh-preparation"),
        "schema": cache.stats() if cache else {},
        "seeded_databases": getattr(config, "_tmcp_seeded_databases", 0),
    }
    terminalreporter.write_line("Immutable preparation: " + json.dumps(stats, sort_keys=True))
    target = os.environ.get("TMCP_PREPARATION_STATS_PATH")
    if target:
        Path(target).write_text(json.dumps(stats, indent=2) + "\n")
