import errno
import hmac
import json
import os
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path

OAUTH_USERNAME_FILE = Path("/etc/terminal-mcp/credentials/oauth-username")
OAUTH_PASSWORD_FILE = Path("/etc/terminal-mcp/credentials/oauth-password")


class OAuthCredentialFileError(RuntimeError):
    pass


def _read_oauth_credential(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        fd = os.open(path, flags)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OAuthCredentialFileError(
                f"{label} credential path must be a regular file: {path}"
            )
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
            raise OAuthCredentialFileError(
                f"{label} credential file must be root-owned and inaccessible "
                f"to group/other: {path}"
            )
        with os.fdopen(fd, "rb", closefd=True) as stream:
            fd = None
            value = stream.read()
    except FileNotFoundError as exc:
        raise OAuthCredentialFileError(f"{label} credential file is required: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OAuthCredentialFileError(
                f"{label} credential file must not be a symlink: {path}"
            ) from exc
        raise OAuthCredentialFileError(f"cannot read {label} credential file: {path}") from exc
    finally:
        if fd is not None:
            os.close(fd)
    if value.endswith(b"\r\n"):
        value = value[:-2]
    elif value.endswith(b"\n"):
        value = value[:-1]
    if not value:
        raise OAuthCredentialFileError(f"{label} credential file must not be empty: {path}")
    return value


class CredentialManager:
    def __init__(self, settings):
        self.settings = settings

    @staticmethod
    def _expiry(value: str) -> str:
        if not value:
            return ""
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("expiry requires timezone")
        if parsed <= datetime.now(UTC):
            raise ValueError("expiry must be in the future")
        return parsed.isoformat()

    @classmethod
    def _active(cls, item: dict) -> bool:
        value = item.get("expires_at", "")
        if not value:
            return True
        try:
            return datetime.fromisoformat(value) > datetime.now(UTC)
        except ValueError:
            return False

    def bearer_items(self) -> list[dict]:
        return self.settings.parse_json_list(self.settings.bearer_credentials_json)

    def oauth_users(self) -> list[dict]:
        return self.settings.parse_json_list(self.settings.oauth_users_json)

    def bearer_valid(self, token: str) -> bool:
        legacy = [
            value.strip() for value in self.settings.bearer_tokens.split(",") if value.strip()
        ]
        values = legacy + [
            item.get("token", "") for item in self.bearer_items() if self._active(item)
        ]
        return any(hmac.compare_digest(token, value) for value in values if value)

    def oauth_user_valid(self, username: str, password: str) -> bool:
        expected_username = _read_oauth_credential(OAUTH_USERNAME_FILE, "OAuth username")
        expected_password = _read_oauth_credential(OAUTH_PASSWORD_FILE, "OAuth password")
        return hmac.compare_digest(username.encode(), expected_username) and hmac.compare_digest(
            password.encode(), expected_password
        )

    def add_bearer(self, name: str, expires_at: str = "") -> dict:
        name = name.strip()
        if not name:
            raise ValueError("name is required")
        items = self.bearer_items()
        if any(item.get("name") == name for item in items):
            raise ValueError("name already exists")
        item = {
            "id": secrets.token_hex(4),
            "name": name,
            "token": "tmcp_" + secrets.token_urlsafe(32),
            "expires_at": self._expiry(expires_at),
        }
        items.append(item)
        self._update(bearer_credentials_json=json.dumps(items, separators=(",", ":")))
        return item

    def add_oauth_user(self, username: str, password: str, expires_at: str = "") -> dict:
        username = username.strip()
        if not username or not password:
            raise ValueError("username and password are required")
        items = self.oauth_users()
        if any(item.get("username") == username for item in items):
            raise ValueError("username already exists")
        item = {
            "id": secrets.token_hex(4),
            "username": username,
            "password": password,
            "expires_at": self._expiry(expires_at),
        }
        items.append(item)
        self._update(oauth_users_json=json.dumps(items, separators=(",", ":")))
        return item

    def delete(self, kind: str, item_id: str) -> None:
        attr = "bearer_credentials_json" if kind == "bearer" else "oauth_users_json"
        items = [
            item
            for item in self.settings.parse_json_list(getattr(self.settings, attr))
            if item.get("id") != item_id
        ]
        self._update(**{attr: json.dumps(items, separators=(",", ":"))})

    def update_runtime(self, cwd: str, user: str, health_command: str) -> None:
        import pwd
        from pathlib import Path

        path = Path(cwd).resolve()
        if not path.is_dir():
            raise ValueError("cwd must be an existing directory")
        pwd.getpwnam(user)
        self._update(cwd=path, terminal_user=user, health_command=health_command.strip())

    def _update(self, **values) -> None:
        for attr, value in values.items():
            setattr(self.settings, attr, value)
        self._write_env()

    def _write_env(self) -> None:
        import os
        import tempfile
        from pathlib import Path

        path = Path(self.settings.env_file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        mapping = {
            "TERMINAL_MCP_CWD": str(self.settings.cwd),
            "TERMINAL_MCP_TERMINAL_USER": self.settings.terminal_user,
            "TERMINAL_MCP_HEALTH_COMMAND": self.settings.health_command,
            "TERMINAL_MCP_BEARER_CREDENTIALS_JSON": self.settings.bearer_credentials_json,
            "TERMINAL_MCP_OAUTH_USERS_JSON": self.settings.oauth_users_json,
            "TERMINAL_MCP_ADMIN_USERNAME": self.settings.admin_username,
            "TERMINAL_MCP_ADMIN_PASSWORD": self.settings.admin_password,
            "TERMINAL_MCP_ADMIN_SESSION_SECRET": self.settings.admin_session_secret,
        }
        existing = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if "=" not in line or line.lstrip().startswith("#"):
                    continue
                key, raw = line.split("=", 1)
                raw = raw.strip()
                try:
                    existing[key] = json.loads(raw) if raw.startswith('"') else raw
                except json.JSONDecodeError:
                    existing[key] = raw
        existing.update(mapping)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".terminal-mcp-", text=True)
        try:
            with os.fdopen(fd, "w") as stream:
                for key in sorted(existing):
                    stream.write(f"{key}={json.dumps(str(existing[key]))}\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
