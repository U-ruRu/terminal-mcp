"""Explicit component, integration and platform selection; full acceptance never selects."""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from fnmatch import fnmatch
from pathlib import Path

DEV_TESTS = (
    "tests/test_public_errors.py",
    "tests/test_compact_contracts.py",
    "tests/test_read_contract.py",
    "tests/test_work_window_domain.py",
    "tests/test_release_version.py",
    "tests/test_test_workflow.py",
    "tests/test_preparation.py",
)
COMPONENTS = {
    "auth": (
        "*auth*",
        "*oauth*",
        "*pairing*",
        "*identity*",
        "*provider*",
        "*bootstrap*",
        "*actor*",
    ),
    "mesh": ("*mesh*", "*fleet*", "*remote*", "*access*", "*session_number*"),
    "storage": (
        "*storage*",
        "*sqlite*",
        "*migration*",
        "*restart*",
        "*recover*",
        "*retention*",
        "*durable*",
        "*lease*",
    ),
    "contracts": (
        "*contract*",
        "*planning*",
        "*bounds*",
        "*metadata*",
        "*public*",
        "*role*",
        "*mcp*",
        "*compact*",
    ),
    "commands": (
        "*command*",
        "*queue*",
        "*execution*",
        "*runtime*",
        "*terminal*",
        "*output*",
        "*read*",
        "*service*",
        "*fencing*",
        "*lease*",
    ),
    "slots": (
        "*slot*",
        "*persistent*",
        "*access*",
        "*window*",
        "*policy*",
        "*lifecycle*",
        "*android*",
        "*session*",
    ),
    "events": ("*event*", "*message*", "*subscribe_capture*"),
    "capture": ("*subscribe_capture*",),
    "tasks": ("*task*", "*claim*"),
    "agents": ("*agent*", "*context*", "*live_claim*"),
    "testing": ("*test_workflow*", "*preparation*"),
    "deployment": (
        "*installer*",
        "*deployment*",
        "*topology*",
        "*release*",
        "*version*",
        "*privacy*",
    ),
}
# Used only when integration coverage is explicitly requested.
DEPENDENCIES = {
    "auth": ("storage", "contracts"),
    "mesh": ("auth", "storage", "contracts", "events"),
    "commands": ("storage", "contracts"),
    "slots": ("auth", "mesh", "storage", "events"),
    "events": ("storage", "contracts"),
    "deployment": ("storage",),
    "tasks": ("storage", "contracts"),
    "agents": ("storage", "contracts"),
}
# A reviewed component allowlist. Unreviewed/mixed modules remain in integration
# until their real application, storage, process or multi-node seams are separated.
# SQLite storage transactions belong here when storage itself is the subject.
COMPONENT_MODULES = frozenset(
    {
        "test_access_mesh_message_application.py",
        "test_access_mesh_message_storage.py",
        "test_agent_context_freshness.py",
        "test_adapter_actor_context.py",
        "test_application_boundary.py",
        "test_application_session_gate_routing.py",
        "test_application_fleet_boundaries.py",
        "test_application_operator_mesh.py",
        "test_auth_foundation.py",
        "test_backup_manifest.py",
        "test_command_run_fast_path.py",
        "test_compact_contracts.py",
        "test_execution_port.py",
        "test_execution_topology.py",
        "test_fleet_control_http.py",
        "test_fleet_identity.py",
        "test_fleet_storage.py",
        "test_fleet_v1_source.py",
        "test_fleet_v1_source_query.py",
        "test_host_resources.py",
        "test_mcp_callback_html_metadata.py",
        "test_mcp_application_adapter.py",
        "test_mcp_callback_response_metadata.py",
        "test_mcp_events_callback_diagnostics_mike3.py",
        "test_mcp_events_diagnostics_opt_in.py",
        "test_mcp_input_bounds.py",
        "test_mcp_output_contracts.py",
        "test_mcp_provider_evidence.py",
        "test_mesh_bootstrap_auth_isolation_401.py",
        "test_output_retention.py",
        "test_persistent_agent_storage.py",
        "test_preparation.py",
        "test_provider_identity_domain.py",
        "test_public_error_boundaries.py",
        "test_public_errors.py",
        "test_qa_idle_window_recovery_lock.py",
        "test_qa_mesh_degraded_cursor_backoff.py",
        "test_qa_wireguard_probe_failure.py",
        "test_read_contract.py",
        "test_read_wire_regressions.py",
        "test_release_version.py",
        "test_sqlite_main_health.py",
        "test_sqlite_observability.py",
        "test_sqlite_posix_lock_preservation.py",
        "test_subscribe_capture.py",
        "test_task_projection_core.py",
        "test_task_storage.py",
        "test_test_workflow.py",
        "test_version.py",
        "test_work_window_domain.py",
        "test_documentation_contract.py",
    }
)
PLATFORM_MODULES = frozenset(
    {
        "test_access_mesh_alert_ack_contract.py",
        "test_access_mesh_mcp_runtime.py",
        "test_access_mesh_operator_http.py",
        "test_access_mesh_repeat_end_qa.py",
        "test_access_mesh_session_restart_contract.py",
        "test_agent_intent_freshness.py",
        "test_agents.py",
        "test_cancel_reconciler_real_process.py",
        "test_execution_ipc.py",
        "test_execution_port_native.py",
        "test_fleet_control_writer_trace.py",
        "test_http_auth.py",
        "test_installer.py",
        "test_installer_staging_isolation.py",
        "test_persistent_http.py",
        "test_queue.py",
        "test_runtime_finalization.py",
        "test_service.py",
        "test_v08.py",
        "test_v09.py",
        "test_v010.py",
    }
)
# Function selectors preserve fast checks inside these mixed modules without
# executing their known app/native boundaries. Full pytest still sees every test.
MIXED_TEST_LAYERS = {
    "test_execution_port.py": {
        "integration": (
            "test_four_durable_fifo_queues_and_default_selection_over_memory_port",
            "test_queued_cancellation_never_reaches_execution_port",
            "test_cancel_between_durable_claim_and_spawn_fences_stdin",
            "test_recovery_timeout_terminates_through_port_and_finalizes",
            "test_restart_marks_lost_running_failed_without_replaying_side_effect",
            "test_durable_claim_prevents_overlapping_running_command",
            "test_mid_execution_process_loss_fails_once_then_unblocks_queue",
            "test_uncertain_executor_keeps_durable_queue_blocked_and_cleans_local_waiters",
        ),
    },
    "test_command_run_fast_path.py": {
        "integration": ("test_default_queue_prefers_idle_then_deterministic_tie",),
    },
    "test_compact_contracts.py": {
        "integration": ("test_builder_matches_application_actual_effective_catalogue",),
    },
    "test_release_version.py": {
        "integration": ("test_current_http_health_includes_short_release_identifier",),
    },
    "test_mcp_events_diagnostics_opt_in.py": {
        "integration": ("test_actual_asgi_default_no_event_diagnostics",),
    },
    "test_execution_topology.py": {
        "platform": ("test_legacy_installer_refuses_split_update_before_mutation",),
    },
}
INTEGRATION_EXTRAS = {
    "capture": (
        "tests/test_mcp_events_message_binding.py",
        "tests/test_mcp_events_subscription_commit_charlie3.py",
        "tests/test_access_mesh_shared_restart.py",
    ),
}
# Order matters: specific owners precede shared/feature-wide boundaries.
SOURCE_COMPONENTS = (
    ("src/terminal_mcp/mcp/subscribe_capture.py", ("capture",)),
    ("src/terminal_mcp/mcp/event_server.py", ("events", "capture")),
    ("src/terminal_mcp/mcp/event*.py", ("events",)),
    ("src/terminal_mcp/mcp/message_binding.py", ("events",)),
    ("src/terminal_mcp/core/public_errors.py", ("contracts",)),
    ("src/terminal_mcp/core/read_contract.py", ("contracts", "commands")),
    ("src/terminal_mcp/core/agent*.py", ("agents",)),
    ("src/terminal_mcp/core/task*.py", ("tasks",)),
    ("src/terminal_mcp/core/managed*.py", ("slots",)),
    ("src/terminal_mcp/core/persistent*.py", ("slots",)),
    ("src/terminal_mcp/application/*message*.py", ("events",)),
    ("src/terminal_mcp/application/*task*.py", ("tasks",)),
    ("src/terminal_mcp/application/*session*.py", ("slots",)),
    ("src/terminal_mcp/application/command*.py", ("commands",)),
    ("src/terminal_mcp/core/execution.py", ("commands",)),
    ("src/terminal_mcp/storage/*message*.py", ("events", "storage")),
    ("src/terminal_mcp/storage/*task*.py", ("tasks", "storage")),
    ("src/terminal_mcp/auth/**", ("auth",)),
    ("src/terminal_mcp/adapters/actor.py", ("auth",)),
    ("src/terminal_mcp/storage/**", ("storage",)),
    ("src/terminal_mcp/fleet/**", ("mesh",)),
    ("src/terminal_mcp/terminal/**", ("commands",)),
    ("src/terminal_mcp/deployment/**", ("deployment",)),
    ("src/terminal_mcp/mcp/**", ("contracts",)),
    ("scripts/test*.py", ("testing",)),
    ("scripts/preparation.py", ("testing",)),
    ("scripts/pytest_profile.py", ("testing",)),
    ("scripts/build*catalog*.py", ("contracts",)),
    ("scripts/release_version.py", ("deployment",)),
    ("deploy/**", ("deployment",)),
)


def test_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py"))


def test_functions(root: Path, relative: str) -> list[str]:
    tree = ast.parse((root / relative).read_text(encoding="utf-8"), filename=relative)
    found = []
    nodes = [(node, "") for node in tree.body]
    for node, prefix in nodes:
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            nodes.extend((child, node.name + "::") for child in node.body)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            found.append(prefix + node.name)
    return found


def layer_tests(root: Path, files: list[str], layer: str) -> list[str]:
    selected = []
    for relative in files:
        name = Path(relative).name
        mixed = MIXED_TEST_LAYERS.get(name)
        if mixed:
            special = {test: tier for tier, tests in mixed.items() for test in tests}
            functions = test_functions(root, relative)
            if set(special) - set(functions):
                raise ValueError(f"Stale test-layer declaration: {relative}")
            selected.extend(
                relative + "::" + test
                for test in functions
                if special.get(test, "component") == layer
                or (layer == "integration" and special.get(test, "component") == "component")
            )
        elif layer == "platform" and name in PLATFORM_MODULES:
            selected.append(relative)
        elif layer == "component" and name in COMPONENT_MODULES:
            selected.append(relative)
        elif layer == "integration" and name not in PLATFORM_MODULES:
            selected.append(relative)
    return sorted(set(selected))


def component_plan(root: Path, names: list[str], *, integration: bool = False) -> dict:
    all_components = names == ["all"]
    if all_components:
        names = sorted(COMPONENTS)
    if not names or any(n not in COMPONENTS for n in names):
        raise ValueError("Choose one or more components: " + ", ".join(COMPONENTS))
    expanded = set(names)
    todo = list(names)
    while todo:
        component = todo.pop()
        for dependency in DEPENDENCIES.get(component, ()) if integration else ():
            if dependency not in expanded:
                expanded.add(dependency)
                todo.append(dependency)
    patterns = [p for n in expanded for p in COMPONENTS[n]]
    selected = [
        p
        for p in test_files(root)
        if all_components or any(fnmatch(Path(p).stem, q) for q in patterns)
    ]
    boundary_files = sorted(
        set(selected)
        | {p for name in names for p in INTEGRATION_EXTRAS.get(name, ()) if (root / p).is_file()}
    )
    if integration:
        selected = boundary_files
    layer = "integration" if integration else "component"
    tests = layer_tests(root, selected, layer)
    return {
        "scope": layer,
        "tests": tests,
        "components": sorted(expanded),
        "deferred": {
            "integration": []
            if integration
            else sorted(set(layer_tests(root, boundary_files, "integration")) - set(tests)),
            "platform": layer_tests(root, boundary_files, "platform"),
        },
        "reasons": ["Explicit owners; transitive boundaries only in integration mode"],
    }


def platform_plan(root: Path, names: list[str]) -> dict:
    if names != ["linux"]:
        raise ValueError("platform requires linux")
    return {
        "scope": "platform",
        "platform": "linux",
        "tests": layer_tests(root, test_files(root), "platform"),
        "reasons": ["Declared Linux shell/IPC/kernel checks; no full acceptance receipt"],
    }


def reverse_test_imports(root: Path) -> dict[str, set[str]]:
    """Resolve helper consumers before applying the requested feedback layer."""
    modules: dict[str, str] = {}
    paths = list((root / "tests").rglob("*.py"))
    for path in paths:
        relative = path.relative_to(root)
        module = ".".join(relative.with_suffix("").parts)
        modules[module] = relative.as_posix()
        modules[module.removeprefix("tests.")] = relative.as_posix()
    reverse: dict[str, set[str]] = defaultdict(set)
    for path in paths:
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                prefix = node.module or ""
                names = [prefix, *(prefix + "." + alias.name for alias in node.names)]
                if node.level:
                    # Unresolved package-relative imports cannot silently narrow selection.
                    raise ValueError(
                        "Relative test-helper import requires broader component coverage"
                    )
            for name in names:
                if name in modules:
                    reverse[modules[name]].add(relative)
    return reverse


def affected_plan(root: Path, changed: list[str], *, integration: bool = False) -> dict:
    changed = sorted({path.replace("\\", "/") for path in changed})
    all_tests = test_files(root)
    reasons: list[str] = []
    owners: set[str] = set()
    selected: set[str] = set()
    uncertain = False
    for path in changed:
        if path in all_tests:
            selected.add(path)
        elif path.startswith(("docs/", "skills/", "dist/")) or path in {"README.md", "AGENTS.md"}:
            if (root / "tests/test_documentation_contract.py").is_file():
                selected.add("tests/test_documentation_contract.py")
            if path == "AGENTS.md" and (root / "tests/test_test_workflow.py").is_file():
                selected.add("tests/test_test_workflow.py")
            reasons.append(f"{path}: current documentation/artifact contract checks")
        else:
            owner = next(
                (names for pattern, names in SOURCE_COMPONENTS if fnmatch(path, pattern)), None
            )
            if owner is None:
                uncertain = True
                reasons.append(f"{path}: shared/unclassified input; all component checks")
            else:
                owners.update(owner)
                reasons.append(f"{path}: declared owner {', '.join(owner)}")
    try:
        reverse = reverse_test_imports(root)
    except (SyntaxError, ValueError, UnicodeError) as exc:
        reverse = {}
        uncertain = True
        reasons.append(f"Test dependency graph uncertain: {type(exc).__name__}")
    if uncertain:
        selected.update(all_tests)
        owners.update(COMPONENTS)
    elif owners:
        owner_plan = component_plan(root, sorted(owners), integration=integration)
        # Expand file roots before following imports; layer filtering happens last.
        patterns = [p for name in owner_plan["components"] for p in COMPONENTS[name]]
        selected.update(
            path for path in all_tests if any(fnmatch(Path(path).stem, p) for p in patterns)
        )
        selected.update(
            p for name in owners for p in INTEGRATION_EXTRAS.get(name, ()) if (root / p).is_file()
        )
        if not selected:
            selected.update(all_tests)
            reasons.append("No declared owner tests found; all component checks")
    todo = deque(selected)
    while todo:
        path = todo.popleft()
        for importer in sorted(reverse.get(path, ())):
            if importer not in selected:
                selected.add(importer)
                todo.append(importer)
                reasons.append(f"{path} -> {importer}: imports changed test helper")
    files = sorted(selected)
    layer = "integration" if integration else "component"
    tests = layer_tests(root, files, layer)
    return {
        "scope": "affected",
        "layer": layer,
        "tests": tests,
        "changed": changed,
        "components": sorted(owners),
        "deferred": {
            "integration": []
            if integration
            else sorted(set(layer_tests(root, files, "integration")) - set(tests)),
            "platform": layer_tests(root, files, "platform"),
        },
        "reasons": reasons or ["Changed component tests and their helper consumers"],
        "coverage": "development feedback only; full release acceptance is separate",
    }
