#!/usr/bin/env python3
"""Four levels of Terminal MCP checks with isolated state and exact-source receipts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata as metadata
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from test_plan import COMPONENTS, DEV_TESTS, affected_plan, component_plan

ROOT = Path(__file__).resolve().parents[1]
STATE_PATHS = {
    "DATABASE_PATH": "db.sqlite3",
    "AUTH_DATABASE_PATH": "auth.sqlite3",
    "OUTPUT_CACHE_PATH": "output.sqlite3",
    "ENV_FILE_PATH": "test.env",
    "EXECUTOR_SOCKET_PATH": "e.sock",
    "FLEET_PEER_TRANSPORTS_PATH": "peers.json",
    "MESH_VPN_STATE_DIR": "mesh-vpn",
    "CWD": "cwd",
    "LOG_PATH": "test.log",
    "RUNTIME_CONFIG_PATH": "runtime.env",
    "FLEET_CONTROL_PATH": "fleet.sqlite3",
    "FLEET_PROJECTION_PATH": "projection.sqlite3",
    "FLEET_NODE_META_PATH": "node.json",
}


def git(*args: str, root: Path = ROOT) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def changed_paths(base: str, root: Path = ROOT) -> list[str]:
    resolved = git("rev-parse", "--verify", base + "^{commit}", root=root)
    if not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise ValueError("Change-selection base must resolve to one exact commit")
    tracked = (
        subprocess.check_output(["git", "diff", "--name-only", "-z", resolved, "--"], cwd=root)
        .decode()
        .split("\0")
    )
    untracked = (
        subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=root
        )
        .decode()
        .split("\0")
    )
    return sorted(set(tracked + untracked) - {""})


def isolate(root: Path, data: Path, output: Path) -> dict[str, str]:
    fallback = data / "state"
    fallback.mkdir(mode=0o700)
    (fallback / "cwd").mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("TERMINAL_MCP_")
        and k not in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONOPTIMIZE"}
    }
    env.update({"TERMINAL_MCP_" + k: str(fallback / v) for k, v in STATE_PATHS.items()})
    env.update(
        {
            "PYTHONPATH": os.pathsep.join((str(root / "src"), str(root / "scripts"))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            # Capture any tempfile.TemporaryDirectory/mkdtemp calls in the isolated run.
            "TMPDIR": str(data),
            "TMP": str(data),
            "TEMP": str(data),
            "TMCP_PROFILE_PATH": str(output / "profile.json"),
            "TMCP_PREPARATION_STATS_PATH": str(output / "preparation.json"),
        }
    )
    return env


def counts(profile: dict) -> dict[str, int]:
    reports = profile.get("reports", [])
    result = {
        "collected": profile.get("collected", 0),
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfail": 0,
        "xpass": 0,
    }
    errors = {r["nodeid"] for r in reports if r["phase"] != "call" and r["outcome"] == "failed"}
    result["errors"] = len(errors) + len(profile.get("collection_errors", []))
    for report in reports:
        if report.get("wasxfail"):
            key = "xfail" if report["outcome"] == "skipped" else "xpass"
            result[key] += 1
        elif report["outcome"] == "skipped":
            result["skipped"] += 1
        elif report["phase"] == "call" and report["nodeid"] not in errors:
            result[report["outcome"]] += 1
    return result


def node_check(url: str, expected_release: str, transport=None) -> dict:
    """Unauthenticated, read-only liveness/version check using the supported HTTP client."""
    import httpx

    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Use a server origin without credentials, path, query, or fragment")
    try:
        with httpx.Client(timeout=10, follow_redirects=False, transport=transport) as client:
            response = client.get(url.rstrip("/") + "/health/live")
        if response.status_code != 200:
            cdn = response.headers.get("cf-mitigated") == "challenge" or (
                response.status_code == 403
                and "text/html" in response.headers.get("content-type", "")
                and "cloudflare" in response.headers.get("server", "").lower()
            )
            return {
                "ok": False,
                "classification": "cdn_client_blocked" if cdn else "http_failure",
                "status_code": response.status_code,
            }
        data = response.json()
        observed = data.get("release") if isinstance(data, dict) else None
        return {
            "ok": isinstance(data, dict)
            and data.get("ok") is True
            and observed == expected_release,
            "classification": "live_release_match"
            if observed == expected_release
            else "release_mismatch",
            "release": observed,
            "expected_release": expected_release,
            "scope": "liveness and public release only; not full SHA, auth, or Mesh acceptance",
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "classification": "probe_error", "error_type": type(exc).__name__}


def run(args: argparse.Namespace, plan: dict, root: Path = ROOT) -> int:
    sha = git("rev-parse", "HEAD", root=root)
    if args.expected_sha and sha != args.expected_sha:
        raise ValueError("HEAD differs from --expected-sha")
    dirty = git("status", "--porcelain", root=root)
    if args.mode == "full" and dirty and not args.allow_dirty:
        raise ValueError(
            "Full acceptance requires a clean checkout; --allow-dirty is diagnostic only"
        )
    output = (
        Path(args.output).resolve()
        if args.output
        else Path(tempfile.mkdtemp(prefix="tmcp-tests-report-"))
    )
    if args.output:
        output.mkdir(parents=True, exist_ok=False)
    # Test state has short, private paths for real Unix sockets and separate SQLite files.
    data = Path(tempfile.mkdtemp(prefix="tmcp-tests-"))
    try:
        return _run_isolated(args, plan, root, sha, dirty, output, data)
    except BaseException as exc:
        # Keep a usable receipt if the runner crashes outside normal pytest result handling.
        receipt_path = output / "report.json"
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text())
                receipt.update(
                    state="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                    finished_at=dt.datetime.now(dt.UTC).isoformat(),
                    error_type=type(exc).__name__,
                    full_test_evidence_eligible=False,
                    state_retained=bool(args.keep_state),
                    verdict="failed",
                )
                receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
            except (OSError, ValueError):
                pass
        raise
    finally:
        # Cleanup applies to pass, fail, timeout, exceptions and KeyboardInterrupt.
        # Report files live outside data; retain test databases only on explicit opt-in.
        if not args.keep_state:
            shutil.rmtree(data)


def _run_isolated(
    args: argparse.Namespace,
    plan: dict,
    root: Path,
    sha: str,
    dirty: str,
    output: Path,
    data: Path,
) -> int:
    env = isolate(root, data, output)
    env["TMCP_PROFILE_SAMPLE"] = "1" if args.sample else "0"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "pytest_profile",
        "-q",
        "--durations=20",
        "--basetemp=" + str(data / "pytest"),
        "--junitxml=" + str(output / "junit.xml"),
        *plan["tests"],
    ]

    if args.fresh_preparation:
        command.append("--fresh-preparation")
    if args.keep_state:
        # Explicit diagnostics must preserve pytest per-test files, too.
        command.extend(("-o", "tmp_path_retention_policy=all"))

    def now():
        return dt.datetime.now(dt.UTC).isoformat()

    dependencies = {d.metadata["Name"]: d.version for d in metadata.distributions()}
    report = {
        "schema": 1,
        "source_sha": sha,
        "source_tree": git("rev-parse", "HEAD^{tree}", root=root),
        "dirty": bool(dirty),
        "mode": args.mode,
        "selection": plan,
        "argv": command,
        "cwd": str(root),
        "state_dir": str(data),
        "started_at": now(),
        "state": "running",
        "python": sys.version,
        "runtime": {
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "implementation": sys.implementation.name,
            "machine": platform.machine(),
            "libc": list(platform.libc_ver()),
        },
        "dependencies": dependencies,
        "load_start": os.getloadavg(),
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {"state": "running", "report": str(output / "report.json"), "selection": plan["scope"]}
        ),
        flush=True,
    )
    started = time.perf_counter()
    with (output / "pytest.log").open("w") as log:
        try:
            result = subprocess.run(
                command,
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=args.timeout,
            )
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
    profile_path = output / "profile.json"
    profile = json.loads(profile_path.read_text()) if profile_path.exists() else {}
    summary = counts(profile)
    source_changed = (
        git("rev-parse", "HEAD", root=root) != sha
        or git("status", "--porcelain", root=root) != dirty
    )
    full_test_evidence_eligible = (
        args.mode == "full"
        and plan["scope"] == "full"
        and not dirty
        and not source_changed
        and exit_code == 0
        and summary["collected"] > 0
        and summary["passed"] == summary["collected"]
        and not profile.get("deselected")
        and not any(summary[k] for k in ("failed", "errors", "skipped", "xfail", "xpass"))
    )
    if source_changed:
        exit_code = exit_code or 3
    if (
        args.mode == "full"
        and not args.allow_dirty
        and exit_code == 0
        and not full_test_evidence_eligible
    ):
        exit_code = 4
    retained = bool(args.keep_state)
    report.update(
        state="completed" if exit_code != 124 else "timeout",
        exit_code=exit_code,
        finished_at=now(),
        state_retained=retained,
        source_changed=source_changed,
        tests=summary,
        deselected=len(profile.get("deselected", [])),
        full_test_evidence_eligible=full_test_evidence_eligible,
        verdict="passed" if exit_code == 0 else "failed",
        timings={
            "wall_seconds": time.perf_counter() - started,
            "collection_seconds": profile.get("collection_seconds"),
            "phase_seconds": profile.get("phase_seconds"),
        },
        resource=profile.get("resource"),
        load_end=os.getloadavg(),
    )
    report["files"] = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in output.iterdir()
        if p.name != "report.json" and p.is_file()
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("\n".join((output / "pytest.log").read_text().splitlines()[-26:]))
    print(
        json.dumps(
            {
                "report": str(output / "report.json"),
                "exit_code": exit_code,
                "tests": summary,
                "full_test_evidence_eligible": full_test_evidence_eligible,
            },
            indent=2,
        )
    )
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("dev", "component", "full", "node"))
    parser.add_argument("components", nargs="*", help="Component names: " + ", ".join(COMPONENTS))
    parser.add_argument(
        "--changed-from", help="Include committed, staged, unstaged and untracked changes"
    )
    parser.add_argument("--plan", action="store_true", help="Print selection without running tests")
    parser.add_argument(
        "--fresh-preparation", action="store_true", help="Measure original fresh preparation paths"
    )
    parser.add_argument("--expected-sha", help="Require this exact 40-character source commit")
    parser.add_argument(
        "--output", help="New output directory (defaults to an isolated /tmp directory)"
    )
    parser.add_argument(
        "--allow-dirty", action="store_true", help="Diagnostic full run; never release evidence"
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="Preserve isolated test databases on any outcome for debugging",
    )
    parser.add_argument(
        "--sample", action="store_true", help="Include a read-only frame sampler in the profile"
    )
    parser.add_argument("--url", help="Node server origin (no credentials)")
    parser.add_argument("--expected-release", help="Exact public release string for node smoke")
    args = parser.parse_args(argv)
    try:
        if args.expected_sha and not re.fullmatch(r"[0-9a-f]{40}", args.expected_sha):
            raise ValueError("--expected-sha requires exactly 40 lowercase hexadecimal characters")
        if args.timeout < 1:
            raise ValueError("Timeout must be positive")
        if args.mode == "node":
            if not args.url or not args.expected_release:
                raise ValueError("node requires --url and --expected-release")
            if args.plan:
                print(json.dumps({"method": "GET", "path": "/health/live", "mutates": False}))
                return 0
            result = node_check(args.url, args.expected_release)
            print(json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        if args.mode == "full":
            if args.changed_from or args.components:
                raise ValueError("Full acceptance always runs all tests; selectors are forbidden")
            plan = {"scope": "full", "tests": ["tests"], "reasons": ["All tests, no filters"]}
        elif args.changed_from:
            if args.components:
                raise ValueError("Use component selection or --changed-from, not both")
            plan = affected_plan(ROOT, changed_paths(args.changed_from))
        elif args.mode == "component":
            plan = component_plan(ROOT, args.components)
        else:
            if args.components:
                raise ValueError("dev does not take component names")
            plan = {
                "scope": "dev",
                "tests": [p for p in DEV_TESTS if (ROOT / p).is_file()],
                "reasons": ["Fast feedback only, not release acceptance"],
            }
        if args.plan:
            print(json.dumps(plan, indent=2))
            return 0
        return run(args, plan)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"test workflow blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
