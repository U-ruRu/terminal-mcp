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


def test_oauth_credential_files_preserve_password_spaces(tmp_path, monkeypatch):
    _install_files(tmp_path, monkeypatch, b"file-user\n", b" secret with spaces \r\n")
    manager = CredentialManager(Settings())

    assert manager.oauth_user_valid("file-user", " secret with spaces ")


def test_file_oauth_credentials_reject_unsafe_permissions(tmp_path, monkeypatch):
    username_path, _ = _install_files(tmp_path, monkeypatch, b"user", b"password")
    username_path.chmod(0o644)
    manager = CredentialManager(Settings())

    with pytest.raises(OAuthCredentialFileError, match="inaccessible to group/other"):
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
