import os
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


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
        'TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="${TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS:-$CANONICAL_CONSOLE_ORIGINS}"'
        in script
    )
    assert (
        'CANONICAL_CONSOLE_ORIGINS='
        '${TERMINAL_MCP_CANONICAL_CONSOLE_ORIGINS:-https://localhost}'
        in script
    )
    assert 'ensure_console_origins' in script



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
        "printf '%s\\n' \"$*\" >> \"$FAKE_CURL_LOG\"\n"
        "header_file=''\n"
        "previous=''\n"
        "for arg in \"$@\"; do\n"
        "  if [ \"$previous\" = '-D' ]; then header_file=$arg; fi\n"
        "  previous=$arg\n"
        "done\n"
        "case \"$*\" in\n"
        "  */internal/fleet/identities*|*/internal/fleet/v1/source/manifest*) "
        "printf '%s' \"$FAKE_INGRESS_STATUS\" ;;\n"
        "  *'/actions/console/snapshot'*)\n"
        "    [ -z \"$header_file\" ] || "
        "printf 'HTTP/1.1 204 No Content\\r\\n"
        "Access-Control-Allow-Origin: https://localhost\\r\\n\\r\\n' "
        "> \"$header_file\"\n"
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


def test_doctor_requires_public_fleet_ingress(tmp_path):
    result, curl_log = _run_doctor_with_ingress_status(tmp_path, "401")

    assert result.returncode == 0
    assert "https://server-a.example.invalid/internal/fleet/identities" in curl_log


def test_doctor_switches_to_v1_probe_after_legacy_mesh_retirement(tmp_path):
    result, curl_log = _run_doctor_with_ingress_status(
        tmp_path, "401", legacy_replication_enabled="false"
    )

    assert result.returncode == 0, result.stderr
    assert "https://server-a.example.invalid/internal/fleet/v1/source/manifest" in curl_log
    assert "https://server-a.example.invalid/internal/fleet/identities" not in curl_log


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
    backup_body = script.split("backup(){", 1)[1].split("schema_rollback_safe(){", 1)[0]
    assert "terminal-mcp.sqlite3" in backup_body
    assert "auth.sqlite3" not in backup_body

def test_installer_blocks_rollback_to_binary_with_older_auth_schema(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = (root / "deploy" / "install.sh").read_text()
    function = "schema_rollback_safe(){" + script.split("schema_rollback_safe(){", 1)[1].split(
        "\n}\n\ninstall_cli_link(){", 1
    )[0] + "\n}"

    data = tmp_path / "data"
    data.mkdir()
    auth_db = data / "auth.sqlite3"
    with sqlite3.connect(auth_db) as db:
        db.execute("PRAGMA user_version=3")
    assert not (data / "terminal-mcp.sqlite3").exists()

    old_release = tmp_path / "old-release"
    package = old_release / "terminal_mcp" / "auth"
    package.mkdir(parents=True)
    (old_release / "terminal_mcp" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "foundation.py").write_text("AUTH_SCHEMA_VERSION = 2\n")

    runner = tmp_path / "schema-check.sh"
    runner.write_text(
        "#!/bin/bash\n"
        f"DATA={str(data)!r}\n"
        "runtime_python(){ local release=$1; shift; PYTHONPATH=\"$release\" \"$PYTHON\" \"$@\"; }\n"
        + function
        + "\n"
        + f"schema_rollback_safe {str(old_release)!r}\n"
    )
    result = subprocess.run(
        ["bash", str(runner)],
        env={**os.environ, "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 43
    assert "Refusing auth schema downgrade 3->2" in result.stderr
    assert "rollback-excluded Access security state" in result.stderr


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
    backup_body = script.split("backup(){", 1)[1].split("schema_rollback_safe(){", 1)[0]
    assert 'runtime_python "$release"' in backup_body


def _run_ingress_config(tmp_path, *, reload_ok=True):
    fake_bin = tmp_path / "fake-caddy-bin"
    fake_bin.mkdir()
    caddy_log = tmp_path / "caddy.log"
    systemctl_log = tmp_path / "systemctl.log"

    (fake_bin / "caddy").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_CADDY_LOG\"\n"
        "case \"$1\" in validate) exit 0 ;; esac\n"
        "exit 99\n"
    )
    (fake_bin / "systemctl").write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$FAKE_SYSTEMCTL_LOG\"\n"
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


def test_ingress_command_validates_backs_up_reloads_and_is_idempotent(tmp_path):
    result, caddyfile, original, caddy_log, systemctl_log, backups = _run_ingress_config(tmp_path)

    assert result.returncode == 0, result.stderr
    configured = caddyfile.read_text()
    assert configured != original
    assert "# BEGIN terminal-mcp managed console ingress" in configured
    assert "/actions/console/*" in configured
    assert "/console/*" in configured
    assert "validate --config" in caddy_log.read_text()
    assert systemctl_log.read_text().splitlines() == ["reload caddy"]
    backup_files = list(backups.glob("caddy-*.Caddyfile"))
    assert len(backup_files) == 1
    assert backup_files[0].read_text() == original

    second = subprocess.run(
        ["bash", "deploy/install.sh", "ingress"],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'fake-caddy-bin'}:{os.environ['PATH']}",
            "FAKE_CADDY_LOG": str(caddy_log),
            "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
            "TERMINAL_MCP_INSTALL_ROOT": str(tmp_path / "install"),
            "TERMINAL_MCP_ENV_DIR": str(tmp_path / "etc"),
            "TERMINAL_MCP_DATA_DIR": str(tmp_path / "data"),
            "TERMINAL_MCP_CACHE_DIR": str(tmp_path / "cache"),
            "TERMINAL_MCP_BACKUP_DIR": str(backups),
            "TERMINAL_MCP_CADDYFILE": str(caddyfile),
            "TERMINAL_MCP_CADDY_BIN": str(tmp_path / "fake-caddy-bin" / "caddy"),
            "TERMINAL_MCP_CADDY_SERVICE": "caddy",
            "TERMINAL_MCP_CADDY_MANAGE_MODE": "required",
            "TERMINAL_MCP_SYSTEMCTL": str(tmp_path / "fake-caddy-bin" / "systemctl"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert second.returncode == 0
    assert systemctl_log.read_text().splitlines() == ["reload caddy"]


def test_ingress_command_restores_caddyfile_when_reload_fails(tmp_path):
    result, caddyfile, original, _caddy_log, systemctl_log, backups = _run_ingress_config(
        tmp_path, reload_ok=False
    )

    assert result.returncode != 0
    assert "restoring" in result.stderr
    assert caddyfile.read_text() == original
    assert len(list(backups.glob("caddy-*.Caddyfile"))) == 1
    assert systemctl_log.read_text().splitlines() == ["reload caddy", "reload caddy"]


def test_doctor_skips_v1_console_probes_until_public_cutover(tmp_path):
    result, curl_log = _run_doctor_with_ingress_status(
        tmp_path, "401", fleet_v1_public_enabled="false"
    )

    assert result.returncode == 0, result.stderr
    assert "/connect" in curl_log
    assert "/actions/console/snapshot" in curl_log
    assert "/console/fleet/v1/" not in curl_log


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


def _run_ingress_defaults(tmp_path, origin_line: str):
    env_dir = tmp_path / "etc-origins"
    env_dir.mkdir()
    env_file = env_dir / "terminal-mcp.env"
    env_file.write_text(
        'TERMINAL_MCP_PUBLIC_BASE_URL="https://terminal.example.test"\n'
        + origin_line
        + "\n"
    )
    result = subprocess.run(
        ["bash", "deploy/install.sh", "ingress"],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "TERMINAL_MCP_INSTALL_ROOT": str(tmp_path / "install-origins"),
            "TERMINAL_MCP_ENV_DIR": str(env_dir),
            "TERMINAL_MCP_DATA_DIR": str(tmp_path / "data-origins"),
            "TERMINAL_MCP_CACHE_DIR": str(tmp_path / "cache-origins"),
            "TERMINAL_MCP_BACKUP_DIR": str(tmp_path / "backups-origins"),
            "TERMINAL_MCP_CADDY_MANAGE_MODE": "off",
            "TERMINAL_MCP_SYSTEMCTL": "/bin/true",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, env_file.read_text()


def test_ingress_repairs_empty_console_origin_for_existing_install(tmp_path):
    result, env_text = _run_ingress_defaults(
        tmp_path, 'TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS=""'
    )

    assert result.returncode == 0, result.stderr
    assert 'TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="https://localhost"' in env_text


def test_ingress_preserves_custom_origin_and_adds_canonical_origin_once(tmp_path):
    result, env_text = _run_ingress_defaults(
        tmp_path,
        'TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS="https://ops.example.invalid"',
    )

    assert result.returncode == 0, result.stderr
    assert (
        'TERMINAL_MCP_CONSOLE_ALLOWED_ORIGINS='
        '"https://ops.example.invalid,https://localhost"'
        in env_text
    )


def _schema_guard_function() -> str:
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install.sh").read_text()
    return "schema_rollback_safe(){" + script.split("schema_rollback_safe(){", 1)[1].split(
        "\n}\n\ninstall_cli_link(){", 1
    )[0] + "\n}"


def _run_runtime_schema_guard(tmp_path, *, current_version: int, target_version: int, legacy=False):
    data = tmp_path / f"data-{current_version}-{target_version}-{int(legacy)}"
    data.mkdir()
    runtime_db = data / "terminal-mcp.sqlite3"
    with sqlite3.connect(runtime_db) as db:
        if legacy:
            db.executescript(
                """
                CREATE TABLE work_items(namespace TEXT, task_id TEXT, state TEXT);
                CREATE TABLE work_claims(namespace TEXT, task_id TEXT, owner_kind TEXT);
                INSERT INTO work_items VALUES('legacy','T1','ready');
                INSERT INTO work_claims VALUES('legacy','T1','legacy_session');
                """
            )
        db.execute(f"PRAGMA user_version={current_version}")

    release = tmp_path / f"release-{target_version}-{int(legacy)}"
    package = release / "terminal_mcp" / "storage"
    package.mkdir(parents=True)
    (release / "terminal_mcp" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "sqlite.py").write_text(f"SCHEMA_VERSION = {target_version}\n")

    runner = tmp_path / f"schema-runtime-{current_version}-{target_version}-{int(legacy)}.sh"
    runner.write_text(
        "#!/bin/bash\n"
        f"DATA={str(data)!r}\n"
        "runtime_python(){ local release=$1; shift; PYTHONPATH=\"$release\" \"$PYTHON\" \"$@\"; }\n"
        + _schema_guard_function()
        + "\n"
        + f"schema_rollback_safe {str(release)!r}\n"
    )
    return subprocess.run(
        ["bash", str(runner)],
        env={**os.environ, "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_blocks_runtime_schema_rollback_without_persistent_rows(tmp_path):
    result = _run_runtime_schema_guard(tmp_path, current_version=18, target_version=17)
    assert result.returncode == 42
    assert "Refusing runtime schema downgrade 18->17" in result.stderr
    assert "cannot restore a compatible runtime database" in result.stderr


def test_installer_blocks_runtime_schema_rollback_with_legacy_claims(tmp_path):
    result = _run_runtime_schema_guard(
        tmp_path, current_version=18, target_version=17, legacy=True
    )
    assert result.returncode == 42
    assert "Refusing runtime schema downgrade 18->17" in result.stderr


@pytest.mark.parametrize("target_version", [18, 19])
def test_installer_allows_runtime_schema_when_target_is_compatible(tmp_path, target_version):
    result = _run_runtime_schema_guard(
        tmp_path, current_version=18, target_version=target_version
    )
    assert result.returncode == 0, result.stderr




def _run_fleet_control_schema_guard(tmp_path, *, current_version: int, target_version: int):
    data = tmp_path / f"fleet-control-{current_version}-{target_version}"
    data.mkdir()
    control_db = data / "fleet-control.sqlite3"
    with sqlite3.connect(control_db) as db:
        db.executescript(
            """
            CREATE TABLE control_schema(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                version INTEGER NOT NULL
            );
            """
        )
        db.execute("INSERT INTO control_schema(singleton,version) VALUES(1,?)", (current_version,))

    release = tmp_path / f"fleet-control-release-{target_version}"
    package = release / "terminal_mcp" / "fleet"
    package.mkdir(parents=True)
    (release / "terminal_mcp" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "control_storage.py").write_text(
        "class FleetControlStore:\n"
        f"    SCHEMA_VERSION = {target_version}\n"
    )

    runner = tmp_path / f"fleet-control-guard-{current_version}-{target_version}.sh"
    runner.write_text(
        "#!/bin/bash\n"
        f"DATA={str(data)!r}\n"
        f"ENV_FILE={str(data / 'missing.env')!r}\n"
        "runtime_python(){ local release=$1; shift; PYTHONPATH=\"$release\" \"$PYTHON\" \"$@\"; }\n"
        + _schema_guard_function()
        + "\n"
        + f"schema_rollback_safe {str(release)!r}\n"
    )
    return subprocess.run(
        ["bash", str(runner)],
        env={**os.environ, "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("current_version", "target_version"),
    [(2, 1), (3, 2)],
)
def test_installer_blocks_fleet_control_schema_downgrade(
    tmp_path,
    current_version,
    target_version,
):
    result = _run_fleet_control_schema_guard(
        tmp_path,
        current_version=current_version,
        target_version=target_version,
    )
    assert result.returncode == 45
    assert (
        f"Refusing fleet-control schema downgrade {current_version}->{target_version}"
        in result.stderr
    )
    assert "managed topology/trust/AccessPolicy state" in result.stderr


@pytest.mark.parametrize(
    ("current_version", "target_version"),
    [(2, 2), (2, 3), (3, 3)],
)
def test_installer_allows_compatible_fleet_control_schema(
    tmp_path,
    current_version,
    target_version,
):
    result = _run_fleet_control_schema_guard(
        tmp_path,
        current_version=current_version,
        target_version=target_version,
    )
    assert result.returncode == 0, result.stderr


def test_health_failure_never_switches_to_schema_incompatible_old_release():
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install.sh").read_text()
    activate = script.split("activate(){", 1)[1].split("\n}\nmkdir -p", 1)[0]
    guard = 'if schema_rollback_safe "$old"; then'
    restore = 'ln -sfn "$old" "$ROOT/current"'
    assert guard in activate
    assert restore in activate
    assert activate.index(guard) < activate.index(restore)


def _run_auth_upgrade_guard(tmp_path, *, with_access_state: bool):
    script = (Path(__file__).resolve().parents[1] / "deploy" / "install.sh").read_text()
    function = "schema_rollback_safe(){" + script.split("schema_rollback_safe(){", 1)[1].split(
        "\n}\n\ninstall_cli_link(){", 1
    )[0] + "\n}"
    data = tmp_path / ("auth-upgrade-state" if with_access_state else "auth-upgrade-empty")
    data.mkdir()
    auth_db = data / "auth.sqlite3"
    with sqlite3.connect(auth_db) as db:
        if with_access_state:
            db.executescript(
                """
                CREATE TABLE auth_access_slots(logical_agent_id TEXT PRIMARY KEY);
                CREATE TABLE auth_access_codes(code_index TEXT PRIMARY KEY);
                CREATE TABLE auth_access_code_tombstones(code_index TEXT PRIMARY KEY);
                INSERT INTO auth_access_slots VALUES('la_legacy');
                """
            )
        db.execute("PRAGMA user_version=2")

    release = tmp_path / ("release-v3-state" if with_access_state else "release-v3-empty")
    package = release / "terminal_mcp" / "auth"
    package.mkdir(parents=True)
    (release / "terminal_mcp" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    (package / "foundation.py").write_text("AUTH_SCHEMA_VERSION = 3\n")
    runner = tmp_path / ("auth-upgrade-state.sh" if with_access_state else "auth-upgrade-empty.sh")
    runner.write_text(
        "#!/bin/bash\n"
        f"DATA={str(data)!r}\n"
        "runtime_python(){ local release=$1; shift; PYTHONPATH=\"$release\" \"$PYTHON\" \"$@\"; }\n"
        + function
        + "\n"
        + f"schema_rollback_safe {str(release)!r}\n"
    )
    result = subprocess.run(
        ["bash", str(runner)],
        env={**os.environ, "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )
    with sqlite3.connect(auth_db) as db:
        version_after = db.execute("PRAGMA user_version").fetchone()[0]
        slot_rows = (
            db.execute("SELECT COUNT(*) FROM auth_access_slots").fetchone()[0]
            if with_access_state
            else 0
        )
    return result, version_after, slot_rows


def test_installer_refuses_v2_to_v3_with_existing_access_verifier_state(tmp_path):
    result, version_after, slot_rows = _run_auth_upgrade_guard(tmp_path, with_access_state=True)
    assert result.returncode == 44
    assert "Refusing auth schema upgrade 2->3" in result.stderr
    assert "cannot be safely re-keyed automatically" in result.stderr
    assert version_after == 2
    assert slot_rows == 1


def test_installer_allows_v2_to_v3_when_access_state_is_empty(tmp_path):
    result, version_after, slot_rows = _run_auth_upgrade_guard(tmp_path, with_access_state=False)
    assert result.returncode == 0, result.stderr
    assert version_after == 2
    assert slot_rows == 0
