"""Windows file protection against the native ACL implementation."""

import pytest
import win32security

from terminal_mcp.auth import credentials
from terminal_mcp.auth.credentials import CredentialManager, OAuthCredentialFileError
from terminal_mcp.config import Settings
from terminal_mcp.platform_windows import secure_path


def test_world_readable_password_acl_is_rejected(tmp_path, monkeypatch):
    secure_path(tmp_path)
    for name, value in [("oauth-username", b"user\r\n"), ("oauth-password", b" secret \n")]:
        path = tmp_path / name
        path.write_bytes(value)
        secure_path(path)
        monkeypatch.setattr(credentials, name.upper().replace("-", "_") + "_FILE", path)
    manager = CredentialManager(Settings(_env_file=None))
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


def test_foreign_service_account_is_rejected(tmp_path):
    manager = CredentialManager(Settings(_env_file=None, env_file_path=tmp_path / "server.env"))
    with pytest.raises(ValueError, match="current service account"):
        manager.update_runtime(str(tmp_path), "Administrator", "")
