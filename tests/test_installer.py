import os
import subprocess
from pathlib import Path


def test_failed_stage_never_activates_incomplete_release(tmp_path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "id").write_text("#!/bin/sh\necho 0\n")
    (fake_bin / "python3").write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = \"-m venv\" ]; then\n"
        "  mkdir -p \"$3/bin\"\n"
        "  printf '#!/bin/sh\nexit 17\n' > \"$3/bin/pip\"\n"
        "  chmod +x \"$3/bin/pip\"\n"
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
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TERMINAL_MCP_INSTALL_ROOT": str(root),
        "TERMINAL_MCP_ENV_DIR": str(env_dir),
        "TERMINAL_MCP_DATA_DIR": str(data_dir),
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
