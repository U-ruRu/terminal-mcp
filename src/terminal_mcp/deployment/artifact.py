"""Build once with the existing installer, export a content-addressed runtime, approve once.

The exported virtual environment includes the pinned native SQLite runtime. Hosts
never resolve dependencies, compile SQLite or run the candidate's full acceptance.
An approval digest is an operator-provided trust anchor, not a self-signed claim.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from terminal_mcp.deployment._release_common import (
    DIGEST,
    SHA,
    atomic,
    encoded,
    read_json,
    require,
    run,
    sha256,
    utc,
)

FORMAT = 1
MAX_UNPACKED = 4 * 1024**3
REQUIRED_GUARANTEES = {
    "schema-versioned",
    "native-defaults-preserved",
    "restore-over-50-serialized",
    "oauth-device-keys-preserved",
    "active-session-deadlines-preserved",
}


def platform_id() -> dict:
    return {
        "machine": platform.machine(),
        "python": platform.python_version(),
        "libc": list(platform.libc_ver()),
        "implementation": sys.implementation.name,
    }


def runtime_command(release: Path, *args: str) -> list[str]:
    native = release / "lib/terminal-mcp-native"
    loader = (native / "loader.path").read_text().strip()
    require(Path(loader).is_absolute() and Path(loader).is_file(), "native_loader_missing")
    return [loader, "--library-path", str(native), str(release / "bin/python"), *args]


def clean_env(home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def build(repo: Path, source_sha: str, output: Path, baseline: Path) -> dict:
    require(bool(SHA.fullmatch(source_sha)), "full_source_sha_required")
    require(not output.exists(), "bundle_output_already_exists")
    resolved = run(["git", "-C", repo, "rev-parse", source_sha + "^{commit}"]).strip()
    require(resolved == source_sha, "source_sha_mismatch")
    tree = run(["git", "-C", repo, "rev-parse", source_sha + "^{tree}"]).strip()
    base = read_json(baseline)
    require(
        all(k in base for k in ("version", "schema_fingerprint", "code_fingerprint")),
        "version_baseline_required",
    )
    # The git archive, not a dirty or floating worktree, is the sole build input.
    with tempfile.TemporaryDirectory(prefix="tmcp-release-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        archive = root / "source.tar"
        run(["git", "-C", repo, "archive", "--format=tar", "--output", archive, source_sha])
        with tarfile.open(archive) as tar:
            for m in tar.getmembers():
                parts = PurePosixPath(m.name).parts
                require(
                    not m.name.startswith("/") and ".." not in parts, "invalid_git_archive_path"
                )
                require(m.isfile() or m.isdir() or m.issym(), "invalid_git_archive_member")
            # git archive is produced locally from the exact, operator-specified commit.
            tar.extractall(source, filter="data")
        env = clean_env(root)
        env.update(
            {
                "TERMINAL_MCP_INSTALL_ROOT": str(root / "install"),
                "TERMINAL_MCP_ENV_DIR": str(root / "config"),
                "TERMINAL_MCP_DATA_DIR": str(root / "data"),
                "TERMINAL_MCP_CACHE_DIR": str(root / "cache"),
                "TERMINAL_MCP_BACKUP_DIR": str(root / "backups"),
                "TERMINAL_MCP_UNIT_FILE": str(root / "unit/terminal-mcp.service"),
                "TERMINAL_MCP_CLI_LINK": str(root / "cli"),
                "TERMINAL_MCP_CADDY_MANAGE_MODE": "off",
                "TERMINAL_MCP_SOURCE_SHA": source_sha,
                "TERMINAL_MCP_RELEASE_BASELINE": str(baseline.resolve()),
                "TERMINAL_MCP_ARTIFACT_BUILD": "1",
            }
        )
        run(
            ["bash", source / "deploy/install.sh", "build-stage"],
            env=env,
            cwd=source,
            timeout=1800,
            code="artifact_build",
        )
        releases = list((root / "install/releases").iterdir())
        require(len(releases) == 1, "one_staged_release_required")
        return pack(releases[0], source_sha, tree, output)


def pack(release: Path, source_sha: str, tree: str, output: Path) -> dict:
    require(bool(SHA.fullmatch(source_sha)), "full_source_sha_required")
    meta = read_json(release / "RELEASE_META.json")
    require(meta.get("commit_sha") in (source_sha, source_sha[:7]), "release_source_mismatch")
    require(
        (release / "lib/terminal-mcp-native/libsqlite3.so.0").is_file(),
        "pinned_sqlite_runtime_required",
    )
    env = clean_env(release)
    probe = """import json,platform,sys,sqlite3
from terminal_mcp.storage.sqlite import SCHEMA_VERSION
from terminal_mcp.auth.foundation import AUTH_SCHEMA_VERSION
from terminal_mcp.fleet.control_storage import FleetControlStore
print(json.dumps({'platform':{'machine':platform.machine(),'python':platform.python_version(),
'libc':list(platform.libc_ver()),'implementation':sys.implementation.name},
'schemas':{'runtime':SCHEMA_VERSION,'auth':AUTH_SCHEMA_VERSION,'control':FleetControlStore.SCHEMA_VERSION},
'sqlite':sqlite3.sqlite_version,'interpreter':str(__import__('pathlib').Path(sys.executable).resolve())}))
"""
    details = json.loads(run(runtime_command(release, "-c", probe), env=env))
    require(details["sqlite"] == "3.53.4", "unexpected_sqlite_runtime")
    freeze = run(runtime_command(release, "-m", "pip", "freeze", "--all"), env=env)
    # Exclude only the application build path; the runtime itself is content-addressed below.
    dependencies = (
        "\n".join(
            sorted(
                line for line in freeze.splitlines() if not line.lower().startswith("terminal-mcp")
            )
        )
        + "\n"
    )
    require(
        all("==" in line and not line.startswith("-e") for line in dependencies.splitlines()),
        "unpinned_runtime_dependency",
    )
    require(not output.exists(), "bundle_output_already_exists")
    output.mkdir(parents=True, mode=0o700)
    try:
        atomic(output / "dependencies.lock", dependencies.encode())
        links, size = {}, 0
        payload = output / "payload.tar.gz"
        with tarfile.open(payload, "w:gz", dereference=False) as tar:
            for p in sorted(release.rglob("*")):
                rel = p.relative_to(release)
                if "__pycache__" in rel.parts or any(
                    x.startswith(".runtime-validation.") for x in rel.parts
                ):
                    continue
                require(p.is_file() or p.is_dir() or p.is_symlink(), "special_payload_file")
                tar.inodes.clear()  # Encode regular files independently; never emit hardlinks.
                info = tar.gettarinfo(str(p), arcname=rel.as_posix())
                info.uid = info.gid = info.mtime = 0
                info.uname = info.gname = ""
                info.mode &= 0o777
                if p.is_symlink():
                    links[rel.as_posix()] = os.readlink(p)
                if p.is_file() and not p.is_symlink():
                    size += p.stat().st_size
                    with p.open("rb") as stream:
                        tar.addfile(info, stream)
                else:
                    tar.addfile(info)
        require(size <= MAX_UNPACKED, "payload_too_large")
        manifest = {
            "format": FORMAT,
            "source_sha": source_sha,
            "source_tree": tree,
            "release": meta["release_id"],
            "version": meta["version"],
            "payload_sha256": sha256(payload),
            "unpacked_bytes": size,
            "dependencies_sha256": sha256(output / "dependencies.lock"),
            "links": links,
            "created_at": utc(),
            **details,
        }
        atomic(output / "manifest.json", encoded(manifest))
        return manifest
    except BaseException:
        # This is a newly-created build output, never a running release or user state.
        shutil.rmtree(output)
        raise


def verify_bundle(bundle: Path, source_sha: str) -> dict:
    require(bool(SHA.fullmatch(source_sha)), "full_source_sha_required")
    m = read_json(bundle / "manifest.json")
    require(
        m.get("format") == FORMAT and m.get("source_sha") == source_sha, "artifact_source_mismatch"
    )
    require(DIGEST.fullmatch(str(m.get("payload_sha256", ""))), "invalid_payload_digest")
    require(sha256(bundle / "payload.tar.gz") == m["payload_sha256"], "artifact_digest_mismatch")
    require(
        sha256(bundle / "dependencies.lock") == m.get("dependencies_sha256"),
        "dependency_lock_mismatch",
    )
    require(
        type(m.get("unpacked_bytes")) is int and 0 < m["unpacked_bytes"] <= MAX_UNPACKED,
        "invalid_payload_size",
    )
    return m


def unpack(bundle: Path, destination: Path, manifest: dict) -> None:
    require(not destination.exists(), "staged_release_already_exists")
    with tarfile.open(bundle / "payload.tar.gz") as tar:
        members = tar.getmembers()
        names, links, total = set(), set(), 0
        require(len(members) <= 100_000, "too_many_payload_members")
        for m in members:
            p = PurePosixPath(m.name)
            require(
                not p.is_absolute()
                and ".." not in p.parts
                and str(p) == m.name
                and "\\" not in m.name
                and m.name not in names,
                "unsafe_payload_path",
            )
            require(m.isfile() or m.isdir() or m.issym(), "unsafe_payload_type")
            names.add(m.name)
            if m.issym():
                require(
                    manifest.get("links", {}).get(m.name) == m.linkname, "payload_link_mismatch"
                )
                target = PurePosixPath(m.linkname)
                if target.is_absolute():
                    require(
                        re.fullmatch(r"bin/python(?:3(?:\.[0-9]+)?)?", m.name)
                        and Path(m.linkname).resolve() == Path(manifest["interpreter"]),
                        "external_payload_link_refused",
                    )
                else:
                    require(".." not in target.parts, "parent_payload_link_refused")
                links.add(m.name)
            total += m.size
        require(total == manifest["unpacked_bytes"], "payload_size_mismatch")
        require(links == set(manifest.get("links", {})), "payload_links_incomplete")
        for m in members:
            require(
                not any(str(p) in links for p in PurePosixPath(m.name).parents),
                "payload_symlink_parent",
            )
        destination.mkdir(parents=True, mode=0o755)
        for m in members:
            target = destination / m.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if m.isdir():
                target.mkdir(exist_ok=True)
                target.chmod(m.mode & 0o777)
            elif m.issym():
                target.symlink_to(m.linkname)
            else:
                with target.open("xb") as stream, tar.extractfile(m) as source:
                    shutil.copyfileobj(source, stream)
                target.chmod(m.mode & 0o777)


def validate_receipt(report: dict, manifest: dict, policy: dict) -> None:
    require(
        report.get("schema") == 1
        and report.get("state") == "completed"
        and report.get("mode") == "full"
        and report.get("verdict") == "passed"
        and report.get("source_sha") == manifest["source_sha"]
        and report.get("source_tree") == manifest["source_tree"]
        and report.get("dirty") is False
        and report.get("source_changed") is False
        and type(report.get("exit_code")) is int
        and report["exit_code"] == 0,
        "complete_clean_full_receipt_required",
    )
    counts = report.get("tests", {})
    fields = {"collected", "passed", "failed", "errors", "skipped", "xfail", "xpass"}
    require(
        isinstance(counts, dict)
        and fields <= counts.keys()
        and all(type(counts[k]) is int and counts[k] >= 0 for k in fields),
        "complete_test_counts_required",
    )
    require(
        counts["collected"] > 0
        and counts["passed"] > 0
        and sum(counts[k] for k in fields - {"collected"}) == counts["collected"]
        and all(counts[k] == 0 for k in ("failed", "errors", "xpass")),
        "acceptance_counts_inconsistent_or_failed",
    )
    exceptions = counts["skipped"] + counts["xfail"]
    require(
        exceptions == policy.get("accepted_exceptions", -1)
        and (not exceptions or policy.get("exception_evidence")),
        "unreviewed_test_exceptions",
    )
    require(report.get("selection", {}).get("scope") == "full", "full_test_selection_required")
    files = report.get("files", {})
    require(
        isinstance(files, dict)
        and {"junit.xml", "profile.json", "pytest.log"} <= files.keys()
        and all(
            Path(name).name == name
            and name not in {".", ".."}
            and isinstance(digest, str)
            and DIGEST.fullmatch(digest)
            for name, digest in files.items()
        ),
        "test_evidence_hashes_required",
    )
    require(
        policy.get("source_sha") == manifest["source_sha"]
        and policy.get("approved_by")
        and policy.get("evidence"),
        "operator_acceptance_required",
    )
    require(REQUIRED_GUARANTEES <= set(policy.get("guarantees", [])), "upgrade_guarantees_missing")
    require(
        report.get("full_test_evidence_eligible") is True, "eligible_full_test_evidence_required"
    )
    require(
        type(report.get("deselected")) is int and report["deselected"] == 0,
        "acceptance_deselected_tests",
    )
    runtime = report.get("runtime", {})
    require(
        runtime.get("sqlite_version") == manifest["sqlite"]
        and runtime.get("python_version") == manifest["platform"]["python"]
        and runtime.get("machine") == manifest["platform"]["machine"]
        and runtime.get("implementation") == manifest["platform"]["implementation"]
        and runtime.get("libc") == manifest["platform"]["libc"],
        "acceptance_native_runtime_mismatch",
    )
    # A source-only run with a different Python/installed dependency set does not
    # accept this binary artifact, even if its source SHA is identical.
    require(
        str(report.get("python", "")).split(" ")[0] == manifest["platform"]["python"],
        "acceptance_python_mismatch",
    )


def verify_evidence_files(receipt: Path, report: dict) -> None:
    from xml.etree import ElementTree

    for name, digest in report["files"].items():
        require(sha256(receipt.parent / name) == digest, "test_evidence_file_mismatch")
    profile = read_json(receipt.parent / "profile.json")
    deselected = profile.get("deselected")
    require(
        (isinstance(deselected, list) and len(deselected) == 0)
        or (type(deselected) is int and deselected == 0),
        "acceptance_deselected_tests",
    )
    tree = ElementTree.parse(receipt.parent / "junit.xml")
    suites = [tree.getroot()] if tree.getroot().tag == "testsuite" else list(tree.iter("testsuite"))
    require(
        sum(int(s.get("tests", -1)) for s in suites) == report["tests"]["collected"]
        and sum(int(s.get("failures", -1)) for s in suites) == 0
        and sum(int(s.get("errors", -1)) for s in suites) == 0
        and sum(int(s.get("skipped", -1)) for s in suites)
        == report["tests"]["skipped"] + report["tests"]["xfail"],
        "junit_receipt_counts_mismatch",
    )


def verify_dependencies(bundle: Path, report: dict) -> None:
    def normalized(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    dependencies = report.get("dependencies")
    require(isinstance(dependencies, dict) and dependencies, "test_dependencies_required")
    tested = {normalized(k): v for k, v in dependencies.items()}
    for line in (bundle / "dependencies.lock").read_text().splitlines():
        name, sep, version = line.partition("==")
        require(sep and name and version, "invalid_dependency_lock")
        # Build frontend versions are not runtime behavior. Everything shipped
        # for the service, including transitive dependencies, must match tests.
        if normalized(name) not in {"pip", "setuptools", "wheel"}:
            require(
                tested.get(normalized(name)) == version,
                "artifact_dependencies_differ_from_acceptance",
            )


def approve(bundle: Path, receipt: Path, policy: Path, output: Path) -> dict:
    report, rules = read_json(receipt), read_json(policy)
    manifest = verify_bundle(bundle, report.get("source_sha", ""))
    validate_receipt(report, manifest, rules)
    verify_evidence_files(receipt, report)
    verify_dependencies(bundle, report)
    approval = {
        "format": FORMAT,
        "verdict": "accepted",
        "source_sha": manifest["source_sha"],
        "manifest_sha256": sha256(bundle / "manifest.json"),
        "artifact_sha256": manifest["payload_sha256"],
        "receipt_sha256": sha256(receipt),
        "receipt_content_sha256": hashlib_sha(report),
        "receipt": report,
        "policy": rules,
        "created_at": utc(),
    }
    require(not output.exists(), "approval_already_exists")
    atomic(output, encoded(approval))
    return {"approval_sha256": sha256(output), "source_sha": manifest["source_sha"]}


def verify_approval(bundle: Path, approval: Path, pin: str, source_sha: str) -> tuple[dict, dict]:
    require(bool(DIGEST.fullmatch(pin)), "approval_sha256_pin_required")
    require(sha256(approval) == pin, "approval_pin_mismatch")
    a = read_json(approval)
    m = verify_bundle(bundle, source_sha)
    require(
        a.get("format") == FORMAT
        and a.get("verdict") == "accepted"
        and a.get("source_sha") == source_sha,
        "release_not_accepted",
    )
    require(
        a.get("manifest_sha256") == sha256(bundle / "manifest.json")
        and a.get("artifact_sha256") == m["payload_sha256"],
        "approval_artifact_mismatch",
    )
    require(
        REQUIRED_GUARANTEES <= set(a.get("policy", {}).get("guarantees", [])),
        "upgrade_guarantees_missing",
    )
    require(
        hashlib_sha(a.get("receipt", {})) == a.get("receipt_content_sha256"),
        "acceptance_receipt_mismatch",
    )
    validate_receipt(a["receipt"], m, a["policy"])
    verify_dependencies(bundle, a["receipt"])
    return m, a


def hashlib_sha(value: dict) -> str:
    # Receipt bytes are normalized by approve, preserving a reproducible embedded digest.
    import hashlib

    return hashlib.sha256(encoded(value)).hexdigest()
