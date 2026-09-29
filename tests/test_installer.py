import os
import subprocess
import tomllib
from pathlib import Path


def test_failed_stage_never_activates_incomplete_release(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "id").write_text("#!/bin/sh\necho 0\n")
    (fake_bin / "python3").write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "-m venv" ]; then\n'
        '  mkdir -p "$3/bin"\n'
        "  printf '#!/bin/sh\nexit 17\n' > \"$3/bin/pip\"\n"
        '  chmod +x "$3/bin/pip"\n'
        "  exit 0\n"
        "fi\n"
        "exit 99\n"
    )
    for path in fake_bin.iterdir():
        path.chmod(0o755)

    root = tmp_path / "install"
    env_dir = tmp_path / "etc"
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    cache_dir = tmp_path / "cache"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TERMINAL_MCP_INSTALL_ROOT": str(root),
        "TERMINAL_MCP_ENV_DIR": str(env_dir),
        "TERMINAL_MCP_DATA_DIR": str(data_dir),
        "TERMINAL_MCP_CACHE_DIR": str(cache_dir),
        "TERMINAL_MCP_BACKUP_DIR": str(backup_dir),
        "TERMINAL_MCP_UNIT_FILE": str(tmp_path / "terminal-mcp.service"),
        "TERMINAL_MCP_SYSTEMCTL": "/bin/true",
        "TERMINAL_MCP_HEALTH_URL": "http://127.0.0.1:9/health/live",
    }
    result = subprocess.run(
        ["bash", "deploy/install.sh", "update"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not (root / "current").exists()
    assert list((root / "releases").iterdir()) == []


def test_installer_exposes_stable_cli_link():
    script = (Path(__file__).resolve().parents[1] / 'deploy' / 'install.sh').read_text()

    assert 'TERMINAL_MCP_CLI_LINK:-/usr/local/bin/terminal-mcp' in script
    assert 'ln -sfn "$ROOT/current/bin/terminal-mcp" "$CLI_LINK"' in script
    assert 'install_cli_link' in script


def test_installer_persists_console_origin_allowlist():
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install.sh").read_text()

    assert (
        'TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="${TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS:-}"'
        in script
    )
    assert 'ensure_env TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS' in script



def test_runtime_dependency_matches_required_mcp_api():
    config = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )
    dependencies = config["project"]["dependencies"]

    assert "mcp>=1.30,<2" in dependencies

    from mcp.server.transport_security import TransportSecuritySettings

    assert TransportSecuritySettings is not None


def test_installer_checks_runtime_imports_before_activation():
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install.sh").read_text()

    import_check = "from mcp.server.transport_security import TransportSecuritySettings"
    assert import_check in script
    assert "import terminal_mcp.app" in script
    assert "Staged release runtime import check failed" in script
    assert script.index(import_check) < script.index("activate(){")


def test_oauth_access_ttl_defaults_to_30_days_everywhere():
    root = Path(__file__).resolve().parents[1]
    script = (root / "deploy" / "install.sh").read_text()
    env_example = (root / ".env.example").read_text()

    assert (
        'TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC="${TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC:-2592000}"'
        in script
    )
    assert (
        'ensure_env TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC '
        '"${TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC:-2592000}"'
        in script
    )
    assert 'TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC="2592000"' in env_example
