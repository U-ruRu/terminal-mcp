"""Windows file protection against the native ACL implementation."""

import os

import pytest
import win32security

from terminal_mcp.auth import credentials
from terminal_mcp.auth.credentials import CredentialManager, OAuthCredentialFileError
from terminal_mcp.config import Settings
from terminal_mcp.platform_windows import secure_path
from terminal_mcp.storage.permissions import secure_database_path


def test_private_credentials_and_database_acl(tmp_path, monkeypatch):
    secure_path(tmp_path)
    for name, value in [("oauth-username", b"user\r\n"), ("oauth-password", b" secret \n")]:
        path = tmp_path / name
        path.write_bytes(value)
        secure_path(path)
        monkeypatch.setattr(credentials, name.upper().replace("-", "_") + "_FILE", path)
    manager = CredentialManager(Settings(_env_file=None))
    assert manager.oauth_user_valid("user", " secret ")
    assert not manager.oauth_user_valid("user", "secret")
    database = tmp_path / "data" / "runtime.db"
    secure_database_path(database)
    assert database.is_file()
    password = tmp_path / "oauth-password"
    descriptor = win32security.GetFileSecurity(
        str(password), win32security.DACL_SECURITY_INFORMATION
    )
    acl = descriptor.GetSecurityDescriptorDacl()
    acl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        0x120089,
        win32security.ConvertStringSidToSid("S-1-1-0"),
    )
    descriptor.SetSecurityDescriptorDacl(1, acl, 0)
    win32security.SetFileSecurity(
        str(password), win32security.DACL_SECURITY_INFORMATION, descriptor
    )
    with pytest.raises(OAuthCredentialFileError, match="cannot read"):
        manager.oauth_user_valid("user", " secret ")


def test_service_account_selection_is_current_identity(tmp_path):
    manager = CredentialManager(Settings(_env_file=None, env_file_path=tmp_path / "server.env"))
    manager.update_runtime(str(tmp_path), "current", "Write-Output ready")
    with pytest.raises(ValueError, match="current service account"):
        manager.update_runtime(str(tmp_path), "Administrator", "")
    assert os.path.isfile(manager.settings.env_file_path)
