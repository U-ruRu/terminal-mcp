"""Supported release workflow: build / approve / plan / apply / status / resume / recover."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

if os.name != "nt":
    import fcntl

from terminal_mcp.deployment._release_common import (
    ReleaseError,
    atomic,
    encoded,
    read_json,
    require,
    schema_versions,
    sha256,
    utc,
)
from terminal_mcp.deployment.artifact import approve, build
from terminal_mcp.deployment.migration_preservation import receipt_cipher
from terminal_mcp.deployment.update import (
    NodeDriver,
    Update,
    create_profile,
    installed_unit_files,
    preserved_rows,
    snapshot,
)


def offline_driver(profile: Path) -> NodeDriver:
    p = read_json(profile)
    driver = NodeDriver(profile, p["target"])
    require(all(not driver.active(u) for u in driver.services), "offline_services_required")
    driver.drained()
    return driver


async def migrate_stores(profile: dict, backup: Path, *, settings=None, access_bridge=False) -> dict:
    # The process environment is supplied by the already-configured node; no .env
    # from the source checkout or packaging environment is inherited.
    from terminal_mcp.auth.identity_migration import (
        assert_identity_isolated,
        initialize_fresh_identity,
    )

    stores = profile["stores"]
    fresh = assert_identity_isolated(Path(stores["runtime"]), Path(stores["auth"]))
    if access_bridge:
        from terminal_mcp.deployment.access_bridge import prepare_access_v26, source_revision
        from terminal_mcp.storage.sqlite import SqliteRepository

        runtime_path = Path(stores["runtime"])
        version = source_revision(runtime_path)
        await SqliteRepository(runtime_path, Path(stores["output"])).initialize(
            preserve_active_commands=True
        )
        prepare_access_v26(runtime_path, source_version=version)
    # Constructors perform synchronous schema preparation; the main service and
    # executor are already stopped and their entire persistent state snapshotted.
    from terminal_mcp.app import create_app

    app = create_app() if settings is None else create_app(settings)

    state = app.state
    await state.service.repo.initialize(preserve_active_commands=True)
    await state.migrate_runtime_state()
    for name in (
        "fleet_node_meta",
        "fleet_projection",
        "fleet_control",
        "oauth_store",
        "auth_foundation",
        "pairing_store",
    ):
        store = getattr(state, name, None)
        if store is not None:
            await store.initialize()
    if fresh:
        initialize_fresh_identity(Path(stores["runtime"]), Path(stores["auth"]))
    # One-time conversion preserves existing identities, policy and deadlines.
    # Never open a new work cycle, reconcile peers or start background workers.
    snap = read_json(backup / "snapshot.json")
    for name, data in snap["stores"].items():
        old = backup / data["backup"]
        require(sha256(old) == data["sha256"], "backup_tampered")
        if name != "output":
            preserved_rows(
                old,
                Path(data["path"]),
                cipher=receipt_cipher(Path(stores["runtime"])) if name == "runtime" else None,
            )
    for path, data in snap["configs"].items():
        require(sha256(Path(path)) == data["sha256"], "protected_configuration_changed")
    return schema_versions(stores)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build", help="Build exact source once using the existing installer stage")
    b.add_argument("--repo", type=Path, default=Path.cwd())
    b.add_argument("--sha", required=True)
    b.add_argument("--baseline", type=Path, required=True)
    b.add_argument("--output", type=Path, required=True)
    a = sub.add_parser(
        "approve", help="Bind a full acceptance receipt and operator policy to a bundle"
    )
    for name in ("bundle", "receipt", "policy", "output"):
        a.add_argument("--" + name, type=Path, required=True)
    f = sub.add_parser("profile", help="Print a non-secret target profile; does not write it")
    f.add_argument("--target", required=True)
    f.add_argument("--env-file", type=Path, default=Path("/etc/terminal-mcp/terminal-mcp.env"))
    f.add_argument("--root", type=Path, default=Path("/opt/terminal-mcp"))
    f.add_argument("--caddyfile", type=Path, default=Path("/etc/caddy/Caddyfile"))
    f.add_argument("--unit-file", type=Path, action="append", default=[])
    for action in ("plan", "apply", "initial", "status", "resume", "recover"):
        q = sub.add_parser(action)
        q.add_argument("--bundle", type=Path, required=True)
        q.add_argument("--approval", type=Path, required=True)
        q.add_argument("--approval-sha256", required=True)
        q.add_argument("--sha", required=True)
        q.add_argument("--target", required=True)
        q.add_argument(
            "--attempt",
            type=int,
            default=1,
            help="New attempt only after explicit pre-migration recovery",
        )
        q.add_argument("--profile", type=Path, default=Path("/etc/terminal-mcp/deploy-target.json"))
        if action in ("apply", "initial", "resume", "recover"):
            q.add_argument("--confirm-window", required=True)
        if action == "plan":
            q.add_argument("--initial", action="store_true")
    q = sub.add_parser("_snapshot", help=argparse.SUPPRESS)
    q.add_argument("--profile", type=Path, required=True)
    q.add_argument("--destination", type=Path, required=True)
    q = sub.add_parser("_migrate", help=argparse.SUPPRESS)
    q.add_argument("--profile", type=Path, required=True)
    q.add_argument("--backup", type=Path, required=True)
    q.add_argument("--receipt", type=Path, required=True)
    q.add_argument("--sha", required=True)
    return p


def main() -> None:
    args = parser().parse_args()
    driver = operation = None
    try:
        if args.action not in ("status", "plan", "profile"):
            require(os.geteuid() == 0, "root_required_for_release_mutations")
        if args.action == "build":
            result = build(
                args.repo.resolve(), args.sha, args.output.resolve(), args.baseline.resolve()
            )
        elif args.action == "approve":
            result = approve(args.bundle, args.receipt, args.policy, args.output)
        elif args.action == "profile":
            units = args.unit_file or installed_unit_files(["terminal-mcp.service"])
            result = create_profile(args.env_file, args.target, args.root, args.caddyfile, units)
            if len(result["services"]) == 2 and not args.unit_file:
                units = installed_unit_files(result["services"])
                result = create_profile(
                    args.env_file, args.target, args.root, args.caddyfile, units
                )
            if len(result["services"]) == 1:
                result["admission_guard_file"] = str(args.env_file.parent / "deploy-window.json")
        elif args.action == "_snapshot":
            driver = offline_driver(args.profile)
            journal = read_json(args.destination.parent / "operation.json")
            require(journal.get("phase") == "backing_up", "snapshot_operation_phase_mismatch")
            result = snapshot(driver.p, args.destination, driver.configs)
            # No backup paths, hashes of secret material or contents in stdout.
            result = {"completed": True, "stores": len(result["stores"])}
        elif args.action == "_migrate":
            driver = offline_driver(args.profile)
            journal = read_json(args.receipt.parent / "operation.json")
            require(
                journal.get("phase") == "migrating"
                and journal["binding"]["identity"].startswith(args.sha + "-"),
                "migration_operation_binding_mismatch",
            )
            require(not args.receipt.exists(), "migration_already_completed")
            # Durable once-only marker: an interrupted migrator is never invoked blindly again.
            fd = os.open(
                args.receipt.with_suffix(".started"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.fsync(fd)
                versions = asyncio.run(migrate_stores(driver.p, args.backup))
                result = {
                    "completed": True,
                    "source_sha": args.sha,
                    "schemas": versions,
                    "snapshot_sha256": sha256(args.backup / "snapshot.json"),
                    "at": utc(),
                }
                atomic(args.receipt, encoded(result))
            finally:
                os.close(fd)
        else:
            driver = NodeDriver(args.profile, args.target)
            operation = Update(
                driver, args.bundle, args.approval, args.approval_sha256, args.sha, args.attempt
            )
            if args.action == "plan":
                result = operation.plan(initial=args.initial)
            elif args.action in ("apply", "initial"):
                result = operation.apply(args.confirm_window, initial=args.action == "initial")
            elif args.action == "status":
                result = operation.status()
            elif args.action == "resume":
                result = operation.resume(args.confirm_window)
            else:
                result = operation.recover(args.confirm_window)
        print(json.dumps(result, sort_keys=True))
        if result.get("outcome") == "partially_applied":
            raise SystemExit(30)
    except Exception as exc:
        # An unreadable status or interrupted migrator does not prove that an
        # earlier invocation made no changes. Never label that outcome not_applied.
        state_unknown = args.action in {"status", "resume", "recover", "_migrate"}
        result = {
            "outcome": "partially_applied" if state_unknown else "not_applied",
            "error": str(exc)
            if isinstance(exc, ReleaseError)
            else "unclassified_failure_inspect_private_operation_state",
        }
        if state_unknown:
            result["state_unknown"] = True
        if operation is not None:
            try:
                result.update(operation.status())
                result.pop("state_unknown", None)
            except Exception:
                result["outcome"] = "partially_applied"
                result["state_unknown"] = True
            result["error"] = (
                str(exc) if isinstance(exc, ReleaseError) else "unclassified_operation_failure"
            )
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(30 if result["outcome"] == "partially_applied" else 20) from None
    finally:
        if driver is not None:
            driver.close()


if __name__ == "__main__":
    main()
