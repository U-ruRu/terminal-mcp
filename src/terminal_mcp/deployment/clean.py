"""Explicit, offline removal of one installation's managed state.

Installers stop services before applying this inventory. Configuration is removed
last so retrying an interrupted reset still discovers custom state paths. This
module never opens a database, follows a link or restores a prior backup.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

_STORE_KEYS = (
    "DATABASE_PATH",
    "AUTH_DATABASE_PATH",
    "OUTPUT_CACHE_PATH",
    "FLEET_NODE_META_PATH",
    "FLEET_CONTROL_PATH",
    "FLEET_PROJECTION_PATH",
)
_FILE_KEYS = (
    "RUNTIME_CONFIG_PATH",
    "FLEET_PEER_TRANSPORTS_PATH",
    "OAUTH_USERNAME_FILE",
    "OAUTH_PASSWORD_FILE",
    "LOG_PATH",
)
_SHARED_DIRECTORIES = {
    Path(p)
    for p in (
        "/",
        "/etc",
        "/var",
        "/var/lib",
        "/var/cache",
        "/var/log",
        "/var/backups",
        "/opt",
        "/usr",
        "/usr/local",
        "/home",
        "/root",
        "/workspace",
        "/tmp",
    )
}


def _checked(path: Path, *, directory: bool = False) -> Path:
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 3:
        raise ValueError("clean state requires an absolute dedicated path")
    if directory and (path in _SHARED_DIRECTORIES or path == Path.home()):
        raise ValueError("clean state cannot include a shared directory")
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError(f"clean state contains a linked path: {part}")
    if path.exists() and (path.is_dir() != directory):
        raise ValueError(f"clean state path has unexpected type: {path}")
    return path


def _values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    _checked(path)
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("configuration is too large")
    result = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if not sep:
            raise ValueError("unsupported configuration syntax")
        # JSON-quoted Windows paths preserve their backslashes.
        try:
            parsed = json.loads(value) if value.startswith('"') else None
        except ValueError:
            parsed = None
        if isinstance(parsed, str):
            result[key] = parsed
        else:
            tokens = shlex.split(value, comments=True)
            if len(tokens) > 1:
                raise ValueError("unsupported configuration syntax")
            result[key] = tokens[0] if tokens else ""
    return result


@dataclass(frozen=True)
class CleanState:
    directories: tuple[Path, ...]
    files: tuple[Path, ...]
    config_directory: Path

    def validate(self, *, protected: tuple[Path, ...] = ()) -> None:
        for path in (*self.directories, self.config_directory):
            _checked(path, directory=True)
            if any(p == path or p.is_relative_to(path) for p in protected):
                raise ValueError("clean state overlaps an active release or source")
        for path in self.files:
            _checked(path)
            if path in protected:
                raise ValueError("clean state overlaps a protected file")

    def require_empty(self) -> None:
        self.validate()
        if any(p.exists() for p in self.files) or any(
            p.exists() and any(p.iterdir()) for p in (*self.directories, self.config_directory)
        ):
            raise ValueError("existing managed state requires update or explicit clean-deploy")

    def apply(self, *, protected: tuple[Path, ...] = (), retain: tuple[Path, ...] = ()) -> None:
        self.validate(protected=protected)
        for path in retain:
            _checked(path)
            if any(path == p or path.is_relative_to(p) for p in protected):
                raise ValueError("retained auth file overlaps a source or release")

        def remove_unretained(path: Path) -> None:
            if path in retain:
                return
            if not any(p.is_relative_to(path) for p in retain):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
                return
            if path.is_dir():
                for child in path.iterdir():
                    remove_unretained(child)
                if not any(p.exists() and p.is_relative_to(path) for p in retain):
                    path.rmdir()

        # Retain in place: no temporary unprotected secret copies, and an
        # interrupted reset still has the original auth store and environment.
        for path in self.files:
            remove_unretained(path)
        for path in dict.fromkeys((*self.directories, self.config_directory)):
            if path.exists():
                remove_unretained(path)


def inventory(
    *,
    root: Path,
    config: Path,
    data: Path,
    cache: Path,
    backups: Path,
    extra_directories: tuple[Path, ...] = (),
) -> CleanState:
    _checked(root, directory=True)
    _checked(config, directory=True)
    values = _values(config / "terminal-mcp.env")
    runtime_path = Path(values.get("TERMINAL_MCP_RUNTIME_CONFIG_PATH") or config / "runtime.env")
    values.update(_values(runtime_path))
    directories = [data, cache, backups, root / "backups", root / "deployments", *extra_directories]
    vpn = values.get("TERMINAL_MCP_MESH_VPN_STATE_DIR")
    if vpn:
        directories.append(Path(vpn))
    files = [runtime_path, root / "CANONICAL_BASELINE.json", root / "service/release.json",
             root / "service/runtime.json"]
    for key in _STORE_KEYS:
        value = values.get("TERMINAL_MCP_" + key)
        if value:
            path = Path(value)
            if key == "DATABASE_PATH":
                files.append(path.parent / "node-identity.json")
            files.extend(
                Path(str(path) + suffix)
                for suffix in ("", "-wal", "-shm", "-journal", ".access-key", ".identity-anchor")
            )
    for key in _FILE_KEYS:
        if value := values.get("TERMINAL_MCP_" + key):
            files.append(Path(value))
    # Nested locations are deleted through their containing managed directory.
    directories = list(dict.fromkeys(directories))
    directories = [p for p in directories if p != config and not p.is_relative_to(config)]
    directories = [
        p
        for p in directories
        if not any(p != other and p.is_relative_to(other) for other in directories)
    ]
    files = [
        p
        for p in dict.fromkeys(files)
        if not any(p == d or p.is_relative_to(d) for d in (*directories, config))
    ]
    state = CleanState(tuple(directories), tuple(files), config)
    state.validate()
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "config", "data", "cache", "backups"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--extra-directory", type=Path, action="append", default=[])
    parser.add_argument("--protect", type=Path, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--preserve-auth", action="store_true")
    parser.add_argument("--require-empty", action="store_true")
    args = parser.parse_args()
    state = inventory(
        root=args.root,
        config=args.config,
        data=args.data,
        cache=args.cache,
        backups=args.backups,
        extra_directories=tuple(args.extra_directory),
    )
    protected = tuple(p.absolute() for p in args.protect)
    state.validate(protected=protected)
    if args.require_empty:
        state.require_empty()
    if args.apply:
        retained = ()
        if args.preserve_auth:
            from terminal_mcp.deployment.clean_auth import paths, preflight

            preflight(args.config, args.data)
            retained = paths(args.config, args.data)[-1]
        state.apply(protected=protected, retain=retained)
    else:
        print(
            json.dumps(
                {
                    "directories": [str(p) for p in (*state.directories, state.config_directory)],
                    "files": [str(p) for p in state.files],
                }
            )
        )


if __name__ == "__main__":
    main()
