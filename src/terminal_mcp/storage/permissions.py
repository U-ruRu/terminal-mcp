from pathlib import Path


def secure_database_path(path: Path) -> None:
    """Create and protect storage that can contain commands and OAuth state."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if not path.exists():
        path.touch(mode=0o600)
    path.chmod(0o600)
