from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts" / "check_repository_privacy.py"


def run_check(root: Path, *, reachable_history: bool = False) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(CHECKER), "--root", str(root)]
    if reachable_history:
        command.append("--reachable-history")
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )


def write_minimal_safe_repository(root: Path) -> None:
    (root / "deploy").mkdir(parents=True)
    (root / "examples").mkdir(parents=True)
    (root / ".env.example").write_text(
        'TERMINAL_MCP_HOST="127.0.0.1"\n'
        'TERMINAL_MCP_PUBLIC_BASE_URL="https://server-a.example.invalid"\n'
        'TERMINAL_MCP_ADMIN_PASSWORD="replace-at-deploy"\n'
    )
    (root / "deploy" / "Caddyfile.example").write_text(
        "server-a.example.invalid {\n    reverse_proxy 127.0.0.1:8080\n}\n"
    )
    (root / "examples" / "fleet.synthetic.json").write_text(
        json.dumps(
            {
                "instances": [
                    {
                        "instance_id": f"server-{suffix}",
                        "label": f"server-{suffix}",
                        "origin": f"https://server-{suffix}.example.invalid",
                    }
                    for suffix in ("a", "b", "c")
                ]
            }
        )
    )


def test_repository_public_examples_are_environment_neutral():
    result = run_check(REPO)
    assert result.returncode == 0, result.stderr


def test_guard_rejects_non_reserved_environment_origin(tmp_path):
    write_minimal_safe_repository(tmp_path)
    env = tmp_path / ".env.example"
    env.write_text(env.read_text().replace("server-a.example.invalid", "server-a.internal"))

    result = run_check(tmp_path)

    assert result.returncode == 1
    assert "example.invalid" in result.stderr


def test_guard_rejects_operator_style_private_ip(tmp_path):
    write_minimal_safe_repository(tmp_path)
    private_ip = ".".join(str(part) for part in (10, 23, 45, 67))
    caddy = tmp_path / "deploy" / "Caddyfile.example"
    caddy.write_text(f"server-a.example.invalid {{\n    reverse_proxy {private_ip}:8080\n}}\n")

    result = run_check(tmp_path)

    assert result.returncode == 1
    assert "IP literal" in result.stderr or "loopback" in result.stderr


def test_guard_rejects_secret_shaped_example_value(tmp_path):
    write_minimal_safe_repository(tmp_path)
    env = tmp_path / ".env.example"
    env.write_text(
        env.read_text().replace(
            'TERMINAL_MCP_ADMIN_PASSWORD="replace-at-deploy"',
            'TERMINAL_MCP_ADMIN_PASSWORD="' + ("x" * 32) + '"',
        )
    )

    result = run_check(tmp_path)

    assert result.returncode == 1
    assert "placeholder" in result.stderr


def test_guard_rejects_unsafe_reachable_commit_metadata(tmp_path):
    write_minimal_safe_repository(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Automation"],
        check=True,
    )
    unsafe_domain = "server-a" + ".internal"
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", f"automation@{unsafe_domain}"],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "synthetic fixture"],
        check=True,
    )

    result = run_check(tmp_path)

    assert result.returncode == 1
    assert "identity is not repository-safe" in result.stderr


def test_reachable_history_mode_rejects_unsafe_ancestor(tmp_path):
    write_minimal_safe_repository(tmp_path)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Automation"], check=True)
    unsafe_domain = "server-a" + ".internal"
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", f"automation@{unsafe_domain}"],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "unsafe ancestor"], check=True
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "automation@example.invalid"],
        check=True,
    )
    marker = tmp_path / "safe-marker.txt"
    marker.write_text("synthetic\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "safe-marker.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "safe head"], check=True)

    assert run_check(tmp_path).returncode == 0
    strict = run_check(tmp_path, reachable_history=True)
    assert strict.returncode == 1
    assert "identity is not repository-safe" in strict.stderr
