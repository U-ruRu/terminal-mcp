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
    "tests/test_access_mesh_grants.py",
    "tests/test_release_version.py",
    "tests/test_test_workflow.py",
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
        "*android_slot_read_scope*",
    ),
    "mesh": ("*mesh*", "*fleet*", "*remote*", "*access*", "*session_number*", "*idle_maintenance*"),
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
        "*schema*",
        "*openapi*",
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
        "*persistent_backend_admission*",
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
        "*idle_maintenance*",
    ),
    "events": ("*event*", "*message*"),
    "tasks": ("*task*", "*claim*"),
    "agents": ("*agent*", "*context*", "*live_claim*"),
    "testing": ("*test_workflow*",),
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
        "test_access_authority.py",
        "test_access_identity_guard.py",
        "test_access_mesh_future_policy.py",
        "test_access_mesh_grants.py",
        "test_access_mesh_registry_storage.py",
        "test_access_mesh_registry_upgrade.py",
        "test_access_mesh_unified_migration.py",
        "test_mesh_topology_transitions.py",
        "test_mesh_projection_namespace.py",
        "test_deployment_migration_preservation.py",
        "test_mesh_topology_upgrade.py",
        "test_mesh_primary_handoff.py",
        "test_mesh_pairing_boundary.py",
        "test_node_identity.py",
        "test_deployment_probes.py",
        "test_access_mesh_message_application.py",
        "test_access_mesh_message_storage.py",
        "test_access_mesh_outbox.py",
        "test_access_mesh_repeat_end_qa.py",
        "test_access_mesh_session_numbers.py",
        "test_access_mesh_session_recovery_qa.py",
        "test_access_mesh_snapshot_gap.py",
        "test_access_mesh_trusted_fleet_peer_health.py",
        "test_adapter_actor_context.py",
        "test_agent_intent_freshness.py",
        "test_agent_overview_pagination.py",
        "test_android_slot_code_generation.py",
        "test_android_slot_public_paths.py",
        "test_android_slot_rotation_replication.py",
        "test_application_boundary.py",
        "test_application_fleet_boundaries.py",
        "test_application_session_gate_routing.py",
        "test_auth_foundation.py",
        "test_auth_path_policy.py",
        "test_backup_manifest.py",
        "test_command_run_fast_path.py",
        "test_command_scope_atomic.py",
        "test_compact_contracts.py",
        "test_context_storage.py",
        "test_deployment_release_workflow.py",
        "test_deployment_retention.py",
        "test_deployment_clean.py",
        "test_deployment_windows.py",
        "test_execution_ipc.py",
        "test_execution_port.py",
        "test_execution_topology.py",
        "test_fleet_control_hardening.py",
        "test_fleet_control_http.py",
        "test_fleet_control_storage.py",
        "test_fleet_node_meta_storage.py",
        "test_fleet_protocol.py",
        "test_fleet_source_settings.py",
        "test_fleet_storage.py",
        "test_fleet_v1_authority.py",
        "test_fleet_v1_projection.py",
        "test_fleet_v1_projection_service.py",
        "test_fleet_v1_projection_settings.py",
        "test_fleet_v1_source.py",
        "test_fleet_v1_source_query.py",
        "test_fleet_v1_source_router.py",
        "test_host_resources.py",
        "test_http_actions_schema.py",
        "test_http_auth.py",
        "test_idle_maintenance.py",
        "test_local_message_sender_receipts.py",
        "test_managed_access_trust_enrollment.py",
        "test_managed_proof_auto_enrollment.py",
        "test_mcp_callback_html_metadata.py",
        "test_mcp_callback_response_metadata.py",
        "test_mcp_event_mesh_storage.py",
        "test_mcp_event_subscriptions.py",
        "test_mcp_events_diagnostics_opt_in.py",
        "test_mcp_events_managed_storage.py",
        "test_mcp_input_bounds.py",
        "test_mcp_output_contracts.py",
        "test_mcp_provider_evidence.py",
        "test_mesh_bootstrap_auth_isolation_401.py",
        "test_mesh_create_retry_idempotency.py",
        "test_oauth_store_refresh.py",
        "test_output_retention.py",
        "test_persistent_idempotency_storage.py",
        "test_persistent_session_policy.py",
        "test_provider_identity_domain.py",
        "test_public_error_boundaries.py",
        "test_public_errors.py",
        "test_qa_wireguard_probe_failure.py",
        "test_read_contract.py",
        "test_read_wire_regressions.py",
        "test_release_version.py",
        "test_service_sqlite_health.py",
        "test_sqlite_main_health.py",
        "test_sqlite_observability.py",
        "test_task_current_storage.py",
        "test_task_projection_core.py",
        "test_task_review_candidates.py",
        "test_task_storage.py",
        "test_test_workflow.py",
        "test_work_session_claim_leases.py",
        "test_work_session_claim_leases_review.py",
    }
)
PLATFORM_MODULES = frozenset(
    {
        "test_execution_port_native.py",
        "test_installer.py",
        "test_access_slot_native_http.py",
        "test_queue.py",
        "test_runtime_finalization.py",
        "test_service.py",
        "test_sqlite_main_health_symlink.py",
    }
)
# Function selectors preserve fast checks inside these mixed modules without
# executing their known app/native boundaries. Full pytest still sees every test.
MIXED_TEST_LAYERS = {
    "test_execution_port.py": {
        "integration": (
            "test_queued_cancellation_finishes_before_execution",
            "test_cancel_between_durable_claim_and_spawn_fences_stdin",
            "test_recovery_timeout_terminates_through_port_and_finalizes",
            "test_uncertain_executor_preserves_the_running_claim",
        ),
    },
    "test_execution_ipc.py": {
        "platform": (
            "test_wrong_peer_uid_denied_without_starting_any_process",
            "test_spool_is_bounded_and_truncation_is_explicit",
            "test_capture_timeout_terminates_owned_process",
            "test_old_client_cannot_steal_generation_back",
            "test_stale_socket_after_hard_crash_is_reclaimed",
        ),
    },
    "test_compact_contracts.py": {
        "integration": ("test_builder_matches_application_actual_effective_catalogue",),
    },
    "test_deployment_release_workflow.py": {
        "platform": ("test_update_env_keeps_durable_events_database_path",),
    },
}
INTEGRATION_EXTRAS = {}
# Order matters: specific owners precede shared/feature-wide boundaries.
SOURCE_COMPONENTS = (
    ("src/terminal_mcp/mcp/event_server.py", ("events",)),
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
# These shared stores and authorization ports are consumed across owner boundaries.
# Keep direct component feedback narrow; expose every real boundary in deferred
# coverage and select it only for an explicit affected integration request.
SHARED_BOUNDARY_INPUTS = (
    "src/terminal_mcp/storage/**",
    "src/terminal_mcp/auth/**",
    "src/terminal_mcp/core/managed_sessions.py",
    "src/terminal_mcp/core/work_windows.py",
    "src/terminal_mcp/application/managed_sessions.py",
)
# Reviewed fallback reasons for the currently unrouted families. New source files
# also use this explicit conservative route until their owner is reviewed.
UNKNOWN_SOURCE_FAMILIES = (
    ("src/terminal_mcp/http/**", "HTTP adapter ownership has not been separated"),
    ("src/terminal_mcp/application/**", "shared application ownership has not been separated"),
    ("src/terminal_mcp/core/**", "shared core ownership has not been separated"),
    ("src/terminal_mcp/adapters/**", "adapter ownership has not been separated"),
    ("src/**", "package/composition ownership has not been separated"),
)


def source_route(path: str) -> dict:
    path = path.replace("\\", "/")
    owner = next((names for pattern, names in SOURCE_COMPONENTS if fnmatch(path, pattern)), None)
    if owner is None:
        reason = next(
            (reason for pattern, reason in UNKNOWN_SOURCE_FAMILIES if fnmatch(path, pattern)),
            "unclassified input",
        )
        return {
            "path": path,
            "kind": "unknown",
            "owners": [],
            "boundaries": "all",
            "reason": reason,
        }
    shared = any(fnmatch(path, pattern) for pattern in SHARED_BOUNDARY_INPUTS)
    return {
        "path": path,
        "kind": "owner",
        "owners": list(owner),
        "boundaries": "all" if shared else "consumers",
        "reason": (
            "shared storage/authorization/lifecycle contracts" if shared else "declared owner"
        ),
    }


def source_boundary_owners(owners: set[str]) -> set[str]:
    """Affected source contracts include owners that consume them, then their ports."""
    consumers = set(owners)
    dependencies = DEPENDENCIES
    todo = list(owners)
    while todo:
        changed_owner = todo.pop()
        for name, ports in dependencies.items():
            if changed_owner in ports and name not in consumers:
                consumers.add(name)
                todo.append(name)
    return consumers


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
    if names == ["windows"]:
        return {
            "scope": "platform",
            "platform": "windows",
            "tests": sorted(
                p.relative_to(root).as_posix() for p in (root / "qa/windows").glob("test_*.py")
            ),
            "reasons": ["Native Windows PowerShell, Job Object, ACL and app boundaries"],
        }
    if names != ["linux"]:
        raise ValueError("platform requires linux or windows")
    return {
        "scope": "platform",
        "platform": "linux",
        "tests": layer_tests(root, test_files(root), "platform"),
        "reasons": [
            "Declared Linux shell/IPC/kernel/filesystem checks; no full acceptance receipt"
        ],
    }


def reverse_test_imports(root: Path) -> dict[str, set[str]]:
    """Resolve direct source imports and helper consumers without importing runtime."""
    modules: dict[str, str] = {}
    paths = list((root / "tests").rglob("*.py"))
    for path in paths:
        relative = path.relative_to(root)
        module = ".".join(relative.with_suffix("").parts)
        modules[module] = relative.as_posix()
        modules[module.removeprefix("tests.")] = relative.as_posix()
    for path in (root / "src").rglob("*.py"):
        relative = path.relative_to(root)
        module = ".".join(relative.with_suffix("").parts[1:]).removesuffix(".__init__")
        modules[module] = relative.as_posix()
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
    routes = []
    source_roots: set[str] = set()
    all_boundaries = False
    uncertain = False
    for path in changed:
        if path in all_tests:
            selected.add(path)
        elif path.startswith(("docs/", "skills/")) or path in {"README.md", "AGENTS.md"}:
            reasons.append(f"{path}: prose-only change; no executable tests selected")
        else:
            route = source_route(path)
            routes.append(route)
            source_roots.add(path)
            if route["kind"] == "unknown":
                uncertain = True
                reasons.append(f"{path}: {route['reason']}; all component checks and boundaries")
            else:
                owners.update(route["owners"])
                all_boundaries |= route["boundaries"] == "all"
                reasons.append(f"{path}: declared owner {', '.join(route['owners'])}")
    try:
        reverse = reverse_test_imports(root)
    except (SyntaxError, ValueError, UnicodeError) as exc:
        reverse = {}
        uncertain = True
        reasons.append(f"Test dependency graph uncertain: {type(exc).__name__}")
    if uncertain:
        selected.update(all_tests)
        owners.update(COMPONENTS)
        all_boundaries = True
    elif owners:
        owner_plan = component_plan(root, sorted(owners))
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
            all_boundaries = True
            reasons.append("No declared owner tests found; all component checks")
    # Include direct source consumers even when their filename has another owner.
    # Runtime import transitivity/dynamic imports are not claimed by this graph.
    todo = deque(sorted(selected | source_roots))
    while todo:
        path = todo.popleft()
        for importer in sorted(reverse.get(path, ())):
            if importer not in selected:
                selected.add(importer)
                todo.append(importer)
                dependency = "affected source" if path in source_roots else "changed test helper"
                reasons.append(f"{path} -> {importer}: imports {dependency}")
    files = sorted(selected.intersection(all_tests))
    boundary_files = set(files)
    if all_boundaries:
        boundary_files.update(all_tests)
        reasons.append("Shared/unknown boundary coverage: all integration and platform checks")
    elif owners:
        boundary_owners = source_boundary_owners(owners)
        boundary_plan = component_plan(root, sorted(boundary_owners), integration=True)
        boundary_files.update(p.split("::")[0] for p in boundary_plan["tests"])
        boundary_files.update(p.split("::")[0] for p in boundary_plan["deferred"]["platform"])
        reasons.append("Source consumer boundaries: " + ", ".join(sorted(boundary_owners)))
    # Helpers shared by a boundary module need the same visible treatment.
    todo = deque(sorted(boundary_files))
    while todo:
        for importer in reverse.get(todo.popleft(), ()):
            if importer not in boundary_files:
                boundary_files.add(importer)
                todo.append(importer)
    boundary_files = sorted(boundary_files.intersection(all_tests))
    layer = "integration" if integration else "component"
    tests = layer_tests(root, boundary_files if integration else files, layer)
    return {
        "scope": "affected",
        "layer": layer,
        "tests": tests,
        "changed": changed,
        "components": sorted(owners),
        "routes": routes,
        "deferred": {
            "integration": []
            if integration
            else sorted(set(layer_tests(root, boundary_files, "integration")) - set(tests)),
            "platform": layer_tests(root, boundary_files, "platform"),
        },
        "reasons": reasons or ["Changed component tests and their helper consumers"],
        "coverage": "development feedback only; full release acceptance is separate",
    }
