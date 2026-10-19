"""Windows identity, ACL and process observation primitives."""

from __future__ import annotations

import msvcrt
import os
import stat
from pathlib import Path

import ntsecuritycon
import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32security


def identity():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    name, domain, _ = win32security.LookupAccountSid(None, sid)
    return sid, f"{domain}\\{name}"


def trusted_sids():
    current, _ = identity()
    return [
        current,
        win32security.ConvertStringSidToSid("S-1-5-18"),
        win32security.ConvertStringSidToSid("S-1-5-32-544"),
    ]


def secure_path(path: Path, *, include_current=True):
    """Seal an existing path; directories propagate the service ACL to new children."""
    path = Path(path)
    if path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise PermissionError("Windows storage requires a regular local path")
    acl = win32security.ACL()
    flags = (win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE) if path.is_dir() else 0
    for sid in trusted_sids() if include_current else trusted_sids()[1:]:
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, sid
        )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(1, acl, 0)
    win32security.SetFileSecurity(
        str(path),
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        descriptor,
    )


def secure_deployment_path(path: Path):
    administrators = win32security.ConvertStringSidToSid("S-1-5-32-544")
    secure_path(path, include_current=not win32security.CheckTokenMembership(None, administrators))


def check_private_fd(fd: int):
    """Validate the descriptor of the same opened file whose bytes will be read."""
    descriptor = win32security.GetSecurityInfo(
        msvcrt.get_osfhandle(fd),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    allowed = {win32security.ConvertSidToStringSid(sid) for sid in trusted_sids()}
    if win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner()) not in allowed:
        raise PermissionError("Windows credential owner must be a service administrator")
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None or not acl.GetAceCount():
        raise PermissionError("Windows credential requires a private DACL")
    for index in range(acl.GetAceCount()):
        kind_flags, _mask, sid = acl.GetAce(index)
        if kind_flags[0] == win32security.ACCESS_DENIED_ACE_TYPE:
            continue
        if (
            kind_flags[0] != win32security.ACCESS_ALLOWED_ACE_TYPE
            or win32security.ConvertSidToStringSid(sid) not in allowed
        ):
            raise PermissionError("Windows credential grants access outside service administrators")


def open_private_fd(path: Path):
    try:
        handle = win32file.CreateFile(
            str(path), win32con.GENERIC_READ, win32con.FILE_SHARE_READ, None,
            win32con.OPEN_EXISTING, win32file.FILE_FLAG_OPEN_REPARSE_POINT, None,
        )
    except pywintypes.error as exc:
        if exc.winerror in {2, 3}:
            raise FileNotFoundError(str(path)) from exc
        raise OSError("Windows credential file is unreadable") from exc
    try:
        attributes = win32file.GetFileInformationByHandle(handle)[0]
        if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise PermissionError("Windows credential requires a regular file")
        fd = msvcrt.open_osfhandle(handle.Detach(), os.O_RDONLY | os.O_BINARY)
    finally:
        handle.Close()
    try:
        check_private_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_private(path: Path):
    with os.fdopen(open_private_fd(path), "rb") as stream:
        return stream.read()


def process_exists(pid):
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        return False
    try:
        process = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        try:
            return win32event.WaitForSingleObject(process, 0) == win32event.WAIT_TIMEOUT
        finally:
            process.Close()
    except pywintypes.error as exc:
        if exc.winerror == 87:  # ERROR_INVALID_PARAMETER: process has exited.
            return False
        if exc.winerror == 5:  # Access denied is an uncertain, potentially live PID.
            return True
        raise


def validate_service_user(user: str):
    _sid, current = identity()
    if user.casefold() not in {"current", current.casefold()}:
        raise ValueError("Windows terminal_user must identify the current service account")


def is_system():
    sid, _ = identity()
    return win32security.ConvertSidToStringSid(sid) == "S-1-5-18"
