from pathlib import Path

from terminal_mcp.platform_paths import IS_WINDOWS


def secure_database_path(path: Path) -> None:
    """Create and protect storage that can contain commands and OAuth state."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if IS_WINDOWS:
        from terminal_mcp.platform_windows import secure_path

        secure_path(path.parent)
        if not path.exists():
            path.touch()
        secure_path(path)
        return
    path.parent.chmod(0o700)
    if not path.exists():
        path.touch(mode=0o600)
    path.chmod(0o600)
