import errno
import os

import pytest

import terminal_mcp.auth.credentials as credential_module
from terminal_mcp.auth.credentials import CredentialManager, OAuthCredentialFileError
from terminal_mcp.config import Settings


def _force_root_fstat(monkeypatch):
    real_fstat = os.fstat

    def root_fstat(fd):
        values = list(real_fstat(fd))
        values[4] = 0
        return os.stat_result(values)

    monkeypatch.setattr(credential_module.os, "fstat", root_fstat)


def _install_files(tmp_path, monkeypatch, username: bytes, password: bytes):
    username_path = tmp_path / "oauth-username"
    password_path = tmp_path / "oauth-password"
    username_path.write_bytes(username)
    password_path.write_bytes(password)
    username_path.chmod(0o600)
    password_path.chmod(0o600)
    monkeypatch.setattr(credential_module, "OAUTH_USERNAME_FILE", username_path)
    monkeypatch.setattr(credential_module, "OAUTH_PASSWORD_FILE", password_path)
    _force_root_fstat(monkeypatch)
    return username_path, password_path


def test_file_oauth_credentials_are_authoritative_and_trim_only_final_eol(tmp_path, monkeypatch):
    _install_files(tmp_path, monkeypatch, b"file-user\n", b" secret with spaces \r\n")
    manager = CredentialManager(
        Settings(
            admin_username="stale-admin",
            admin_password="stale-secret",
            oauth_admin_username="legacy-admin",
            oauth_admin_password="legacy-secret",
            oauth_users_json='[{"username":"stale-user","password":"stale-secret"}]',
        )
    )

    assert manager.oauth_user_valid("file-user", " secret with spaces ")
    assert not manager.oauth_user_valid("file-user", "secret with spaces")
    assert not manager.oauth_user_valid("stale-admin", "stale-secret")
    assert not manager.oauth_user_valid("stale-user", "stale-secret")


def test_file_oauth_credentials_missing_fail_closed(tmp_path, monkeypatch):
    username_path = tmp_path / "missing-user"
    password_path = tmp_path / "missing-password"
    monkeypatch.setattr(credential_module, "OAUTH_USERNAME_FILE", username_path)
    monkeypatch.setattr(credential_module, "OAUTH_PASSWORD_FILE", password_path)
    manager = CredentialManager(Settings())

    with pytest.raises(OAuthCredentialFileError, match="credential file is required"):
        manager.oauth_user_valid("admin", "secret")


def test_file_oauth_credentials_reject_unsafe_permissions(tmp_path, monkeypatch):
    username_path, _ = _install_files(tmp_path, monkeypatch, b"user", b"password")
    username_path.chmod(0o644)
    manager = CredentialManager(Settings())

    with pytest.raises(OAuthCredentialFileError, match="inaccessible to group/other"):
        manager.oauth_user_valid("user", "password")


def test_file_oauth_credentials_require_root_owner(tmp_path, monkeypatch):
    _install_files(tmp_path, monkeypatch, b"user", b"password")
    real_fstat = os.fstat

    def non_root_fstat(fd):
        values = list(real_fstat(fd))
        values[4] = 1000
        return os.stat_result(values)

    monkeypatch.setattr(credential_module.os, "fstat", non_root_fstat)
    manager = CredentialManager(Settings())
    with pytest.raises(OAuthCredentialFileError, match="root-owned"):
        manager.oauth_user_valid("user", "password")


def test_file_oauth_credentials_unreadable_fail_closed(tmp_path, monkeypatch):
    username_path, _ = _install_files(tmp_path, monkeypatch, b"user", b"password")
    original_open = os.open

    def fail_open(path, flags, mode=0o777):
        if os.fspath(path) == os.fspath(username_path):
            raise PermissionError(errno.EACCES, "denied", os.fspath(path))
        return original_open(path, flags, mode)

    monkeypatch.setattr(credential_module.os, "open", fail_open)
    manager = CredentialManager(Settings())
    with pytest.raises(
        OAuthCredentialFileError, match="cannot read OAuth username credential file"
    ):
        manager.oauth_user_valid("user", "password")


def test_file_oauth_credentials_reject_symlink(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_bytes(b"user")
    target.chmod(0o600)
    username_path = tmp_path / "oauth-username"
    username_path.symlink_to(target)
    password_path = tmp_path / "oauth-password"
    password_path.write_bytes(b"password")
    password_path.chmod(0o600)
    monkeypatch.setattr(credential_module, "OAUTH_USERNAME_FILE", username_path)
    monkeypatch.setattr(credential_module, "OAUTH_PASSWORD_FILE", password_path)
    manager = CredentialManager(Settings())

    with pytest.raises(OAuthCredentialFileError, match="must not be a symlink"):
        manager.oauth_user_valid("user", "password")
