#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

SAFE_DOMAIN_SUFFIX = ".example.invalid"
SENSITIVE_ENV_PARTS = ("PASSWORD", "SECRET", "TOKEN", "PRIVATE_KEY", "API_KEY")
PLACEHOLDER_PREFIXES = ("replace-", "placeholder-", "example-")
SAFE_COMMIT_EMAIL_SUFFIXES = ("example.invalid", "users.noreply.github.com")
CHECKED_TEXT_FILES = (
    Path(".env.example"),
    Path("deploy/Caddyfile.example"),
)
OPERATOR_PATH_PREFIXES = ("/workspace/", "/root/", "/home/", "/srv/")
IPV4_RE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")


class PrivacyError(ValueError):
    pass


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _is_safe_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    documentation = (
        ipaddress.ip_network("192.0.2.0/24"),
        ipaddress.ip_network("198.51.100.0/24"),
        ipaddress.ip_network("203.0.113.0/24"),
        ipaddress.ip_network("2001:db8::/32"),
    )
    return any(address in network for network in documentation)


def _is_safe_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host == "example.invalid" or host.endswith(SAFE_DOMAIN_SUFFIX)
    return _is_safe_ip(host)


def _validate_url(value: str, context: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.hostname:
        raise PrivacyError(f"{context}: expected explicit HTTP(S)/WS(S) URL")
    if not _is_safe_hostname(parsed.hostname):
        raise PrivacyError(f"{context}: environment URL must use example.invalid or loopback")


def _validate_text_literals(path: Path, text: str) -> None:
    for match in IPV4_RE.finditer(text):
        value = match.group(0)
        if not _is_safe_ip(value):
            raise PrivacyError(f"{path}: non-documentation IP literal is not repository-safe")
    for prefix in OPERATOR_PATH_PREFIXES:
        if prefix in text:
            raise PrivacyError(f"{path}: operator filesystem path is not repository-safe")


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PrivacyError(f"{path}: malformed environment example line")
        key, value = line.split("=", 1)
        values[key.strip()] = _unquote(value)
    return values


def check_env_example(root: Path) -> None:
    path = root / ".env.example"
    values = _read_env(path)
    for key, value in values.items():
        if not value:
            continue
        if key.endswith("_PUBLIC_BASE_URL") or key.endswith("_ISSUER") or key.endswith("_AUDIENCE"):
            _validate_url(value, key)
        if "ORIGINS" in key:
            for index, origin in enumerate(part.strip() for part in value.split(",")):
                if origin:
                    _validate_url(origin, f"{key}[{index}]")
        if key.endswith("_HOST") and not _is_safe_hostname(value):
            raise PrivacyError(f"{key}: host example must be loopback or example.invalid")
        if any(part in key for part in SENSITIVE_ENV_PARTS):
            if value in {"[]", "{}"}:
                continue
            if not value.startswith(PLACEHOLDER_PREFIXES):
                raise PrivacyError(
                    f"{key}: credential example must be empty or an explicit placeholder"
                )


def check_caddy_example(root: Path) -> None:
    path = root / "deploy/Caddyfile.example"
    for raw in path.read_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw == stripped and stripped.endswith(" {"):
            site = stripped[:-2].strip().split()[0]
            hostname = site.split(":", 1)[0]
            if not _is_safe_hostname(hostname):
                raise PrivacyError(f"{path}: site address must use example.invalid")
        if stripped.startswith("reverse_proxy "):
            target = stripped.split()[1]
            host = target.rsplit(":", 1)[0].strip("[]")
            if not _is_safe_ip(host) or not ipaddress.ip_address(host).is_loopback:
                raise PrivacyError(f"{path}: example reverse proxy must target loopback")


def check_fleet_fixture(root: Path) -> None:
    path = root / "examples/fleet.synthetic.json"
    payload = json.loads(path.read_text())
    instances = payload.get("instances")
    if not isinstance(instances, list) or len(instances) < 3:
        raise PrivacyError(f"{path}: expected at least three synthetic instances")
    seen: set[str] = set()
    for index, item in enumerate(instances):
        if not isinstance(item, dict):
            raise PrivacyError(f"{path}: instance {index} must be an object")
        instance_id = item.get("instance_id")
        label = item.get("label")
        origin = item.get("origin")
        expected = f"server-{chr(ord('a') + index)}"
        if instance_id != expected or label != expected:
            raise PrivacyError(
                f"{path}: synthetic instance IDs/labels must use server-a/server-b/server-c"
            )
        if instance_id in seen:
            raise PrivacyError(f"{path}: duplicate synthetic instance ID")
        seen.add(instance_id)
        if not isinstance(origin, str):
            raise PrivacyError(f"{path}: origin must be a string")
        _validate_url(origin, f"{path}:instances[{index}].origin")


def _git_root_available(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _safe_commit_email(email: str) -> bool:
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower()
    return domain.endswith(SAFE_COMMIT_EMAIL_SUFFIXES)


def check_git_metadata(root: Path, ref: str = "HEAD", *, reachable: bool = False) -> None:
    if not _git_root_available(root):
        return
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            *([] if reachable else ["-1"]),
            ref,
            "--format=%H%x09%ae%x09%ce",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PrivacyError(f"git metadata check failed for {ref}")
    for row in result.stdout.splitlines():
        commit, author_email, committer_email = row.split("\t", 2)
        if not _safe_commit_email(author_email):
            raise PrivacyError(
                f"{commit[:12]}: author identity is not repository-safe; rewrite before integration"
            )
        if not _safe_commit_email(committer_email):
            raise PrivacyError(
                f"{commit[:12]}: committer identity is not repository-safe; "
                "rewrite before integration"
            )


def check_repository(root: Path, *, reachable_history: bool = False) -> None:
    for relative in CHECKED_TEXT_FILES:
        path = root / relative
        _validate_text_literals(relative, path.read_text())
    examples = root / "examples"
    if examples.exists():
        for path in examples.rglob("*"):
            if path.is_file():
                _validate_text_literals(path.relative_to(root), path.read_text())
    check_env_example(root)
    check_caddy_example(root)
    check_fleet_fixture(root)
    check_git_metadata(root, reachable=reachable_history)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate repository-safe public environment examples."
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--reachable-history",
        action="store_true",
        help="validate author/committer metadata for every commit reachable from HEAD",
    )
    args = parser.parse_args()
    try:
        check_repository(args.root.resolve(), reachable_history=args.reachable_history)
    except (OSError, json.JSONDecodeError, PrivacyError) as exc:
        print(f"repository privacy check failed: {exc}", file=sys.stderr)
        return 1
    print("repository privacy check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
