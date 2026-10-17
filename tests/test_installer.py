import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("action", ["update", "clean-deploy"])
def test_failed_stage_never_activates_incomplete_release(tmp_path, action):
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
        f'exec "{sys.executable}" "$@"\n'
    )
    for path in fake_bin.iterdir():
        path.chmod(0o755)

    root = tmp_path / "install"
    env_dir = tmp_path / "etc"
    data_dir = tmp_path / "data"
    backup_dir = tmp_path / "backups"
    cache_dir = tmp_path / "cache"
    data_dir.mkdir()
    retained = data_dir / "existing-state"
    retained.write_text("keep until staging succeeds")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TERMINAL_MCP_ADMIN_PASSWORD": "isolated-test-only",
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
        ["bash", "deploy/install.sh", action],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert retained.read_text() == "keep until staging succeeds"
    assert not (root / "current").exists()
    assert list((root / "releases").iterdir()) == []


def _run_doctor_with_ingress_status(
    tmp_path,
    ingress_status: str,
    *,
    legacy_replication_enabled: str = "true",
    fleet_v1_public_enabled: str = "true",
):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    curl_log = tmp_path / "curl.log"
    (fake_bin / "curl").write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_CURL_LOG"\n'
        "header_file=''\n"
        "previous=''\n"
        'for arg in "$@"; do\n'
        "  if [ \"$previous\" = '-D' ]; then header_file=$arg; fi\n"
        "  previous=$arg\n"
        "done\n"
        'case "$*" in\n'
        "  */internal/fleet/identities*|*/internal/fleet/v1/source/manifest*) "
        "printf '%s' \"$FAKE_INGRESS_STATUS\" ;;\n"
        "  *'/actions/console/snapshot'*)\n"
        '    [ -z "$header_file" ] || '
        "printf 'HTTP/1.1 204 No Content\\r\\n"
        "Access-Control-Allow-Origin: https://localhost\\r\\n\\r\\n' "
        '> "$header_file"\n'
        "    printf '204' ;;\n"
        "  *'/connect'*) printf '200' ;;\n"
        "  *'/console/ws-ticket'*) printf '401' ;;\n"
        "  *'/console/fleet/v1/'*) printf '401' ;;\n"
        "esac\n"
        "exit 0\n"
    )
    (fake_bin / "curl").chmod(0o755)

    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    (env_dir / "terminal-mcp.env").write_text(
        'TERMINAL_MCP_OAUTH_ACCESS_TTL_SEC="2592000"\n'
        'TERMINAL_MCP_PUBLIC_BASE_URL="https://server-a.example.invalid"\n'
        'TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="https://localhost"\n'
        'TERMINAL_MCP_QUEUE_WORKERS="4"\n'
        f'TERMINAL_MCP_FLEET_LEGACY_REPLICATION_ENABLED="{legacy_replication_enabled}"\n'
        f'TERMINAL_MCP_FLEET_V1_PUBLIC_ENABLED="{fleet_v1_public_enabled}"\n'
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


def _run_ingress_config(tmp_path, *, reload_ok=True):
    fake_bin = tmp_path / "fake-caddy-bin"
    fake_bin.mkdir()
    caddy_log = tmp_path / "caddy.log"
    systemctl_log = tmp_path / "systemctl.log"

    (fake_bin / "caddy").write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_CADDY_LOG"\n'
        'case "$1" in validate) exit 0 ;; esac\n'
        "exit 99\n"
    )
    (fake_bin / "systemctl").write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$FAKE_SYSTEMCTL_LOG"\n'
        + ("exit 0\n" if reload_ok else "exit 1\n")
    )
    for path in fake_bin.iterdir():
        path.chmod(0o755)

    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    (env_dir / "terminal-mcp.env").write_text(
        'TERMINAL_MCP_PUBLIC_BASE_URL="https://terminal.example.test"\n'
        'TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="https://localhost"\n'
    )
    caddyfile = tmp_path / "Caddyfile"
    original = (
        "terminal.example.test {\n"
        "    @legacy path /mcp /health/* /internal/fleet/*\n"
        "    reverse_proxy @legacy 127.0.0.1:8080\n"
        "}\n"
    )
    caddyfile.write_text(original)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_CADDY_LOG": str(caddy_log),
        "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
        "TERMINAL_MCP_INSTALL_ROOT": str(tmp_path / "install"),
        "TERMINAL_MCP_ENV_DIR": str(env_dir),
        "TERMINAL_MCP_DATA_DIR": str(tmp_path / "data"),
        "TERMINAL_MCP_CACHE_DIR": str(tmp_path / "cache"),
        "TERMINAL_MCP_BACKUP_DIR": str(tmp_path / "backups"),
        "TERMINAL_MCP_CADDYFILE": str(caddyfile),
        "TERMINAL_MCP_CADDY_BIN": str(fake_bin / "caddy"),
        "TERMINAL_MCP_CADDY_SERVICE": "caddy",
        "TERMINAL_MCP_CADDY_MANAGE_MODE": "required",
        "TERMINAL_MCP_SYSTEMCTL": str(fake_bin / "systemctl"),
    }
    result = subprocess.run(
        ["bash", "deploy/install.sh", "ingress"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, caddyfile, original, caddy_log, systemctl_log, tmp_path / "backups"


def test_ingress_command_restores_caddyfile_when_reload_fails(tmp_path):
    result, caddyfile, original, _caddy_log, systemctl_log, backups = _run_ingress_config(
        tmp_path, reload_ok=False
    )

    assert result.returncode != 0
    assert "restoring" in result.stderr
    assert caddyfile.read_text() == original
    assert len(list(backups.glob("caddy-*.Caddyfile"))) == 1
    assert systemctl_log.read_text().splitlines() == ["reload caddy", "reload caddy"]


def test_doctor_probes_console_fallback_and_read_v2_surfaces(tmp_path):
    result, curl_log = _run_doctor_with_ingress_status(tmp_path, "401")

    assert result.returncode == 0, result.stderr
    for route in (
        "/connect",
        "/actions/console/snapshot",
        "/console/ws-ticket",
        "/console/fleet/v1/probe",
        "/console/fleet/v1/snapshot",
        "/console/fleet/v1/activity",
        "/console/fleet/v1/query/tasks",
        "/console/fleet/v1/detail/tasks?entity_id=ingress-probe",
        "/console/fleet/v1/namespaces",
        "/console/fleet/v1/task-graph?namespace=ingress-probe&task_id=ingress-probe",
        "/console/fleet/v1/ws-ticket",
    ):
        assert route in curl_log
