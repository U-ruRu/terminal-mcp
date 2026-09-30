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


def _run_doctor_with_ingress_status(tmp_path, ingress_status: str):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl_log = tmp_path / "curl.log"
    (fake_bin / "curl").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CURL_LOG\"\n"
        "case \"$*\" in\n"
        "  */internal/fleet/identities*) printf '%s' \"$FAKE_INGRESS_STATUS\" ;;\n"
        "esac\n"
        "exit 0\n"
    )
    (fake_bin / "curl").chmod(0o755)

    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    (env_dir / "terminal-mcp.env").write_text(
        'TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC="2592000"\n'
        'TERMINAL_MCP_PUBLIC_BASE_URL="https://server-a.example.invalid"\n'
        'TERMINAL_MCP_QUEUE_WORKERS="4"\n'
    )
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_CURL_LOG": str(curl_log),
        "FAKE_INGRESS_STATUS": ingress_status,
        "TERMINAL_MCP_INSTALL_ROOT": str(tmp_path / "install"),
        "TERMINAL_MCP_ENV_DIR": str(env_dir),
        "TERMINAL_MCP_DATA_DIR": str(tmp_path / "data"),
        "TERMINAL_MCP_CACHE_DIR": str(tmp_path / "cache"),
        "TERMINAL_MCP_BACKUP_DIR": str(tmp_path / "backups"),
        "TERMINAL_MCP_UNIT_FILE": str(tmp_path / "terminal-mcp.service"),
        "TERMINAL_MCP_SYSTEMCTL": "/bin/true",
        "TERMINAL_MCP_HEALTH_URL": "http://127.0.0.1:8080/health/live",
    }
    result = subprocess.run(
        ["bash", "deploy/install.sh", "doctor"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, curl_log.read_text()


def test_doctor_requires_public_fleet_ingress(tmp_path):
    result, curl_log = _run_doctor_with_ingress_status(tmp_path, "401")

    assert result.returncode == 0
    assert "https://server-a.example.invalid/internal/fleet/identities" in curl_log


def test_doctor_rejects_proxy_404_for_public_fleet_ingress(tmp_path):
    result, _curl_log = _run_doctor_with_ingress_status(tmp_path, "404")

    assert result.returncode != 0
    assert "Public fleet ingress check failed" in result.stderr
    assert "reverse proxy forwards /internal/fleet/*" in result.stderr


def test_caddy_example_documents_fleet_ingress_contract():
    caddy = (Path(__file__).resolve().parents[1] / "deploy" / "Caddyfile.example").read_text()

    assert "/internal/fleet/*" in caddy
    assert "application fleet-signing check" in caddy


def test_installer_separates_durable_auth_database_from_runtime_backup():
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install.sh").read_text()

    assert 'TERMINAL_MCP_AUTH_DATABASE_PATH="$DATA/auth.sqlite3"' in script
    assert 'ensure_env TERMINAL_MCP_AUTH_DATABASE_PATH "$DATA/auth.sqlite3"' in script
    assert 'chmod 0600 "$DATA/auth.sqlite3"' in script
    backup_body = script.split("backup(){", 1)[1].split("install_cli_link(){", 1)[0]
    assert "terminal-mcp.sqlite3" in backup_body
    assert "auth.sqlite3" not in backup_body

def test_installer_pins_isolated_fixed_sqlite_runtime():
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install.sh").read_text()

    assert "SQLITE_VERSION=3.53.4" in script
    assert "sqlite-autoconf-3530400.tar.gz" in script
    assert "454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338" in script
    assert 'native/libsqlite3.so.0' in script
    assert r'--library-path "\$RELEASE/lib/terminal-mcp-native"' in script
    assert "LD_LIBRARY_PATH" not in script
    assert "LD_PRELOAD" not in script
    assert "import sqlite3; print(sqlite3.sqlite_version)" in script


def test_installer_builds_sqlite_before_runtime_import_check():
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install.sh").read_text()

    build_call = 'build_sqlite_runtime "$STAGED_RELEASE"'
    import_check = "from mcp.server.transport_security import TransportSecuritySettings"
    assert script.index(build_call) < script.index(import_check)
    assert 'runtime_python "$STAGED_RELEASE" -c' in script


def test_update_stages_fixed_sqlite_before_backup_and_activation():
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install.sh").read_text()

    update = next(line for line in script.splitlines() if line.startswith(" update)"))
    assert (
        update.index("stage")
        < update.index('backup "$STAGED_RELEASE"')
        < update.index("activate")
    )
    backup_body = script.split("backup(){", 1)[1].split("install_cli_link(){", 1)[0]
    assert 'runtime_python "$release"' in backup_body
