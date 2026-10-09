"""Write a document only its owner can read (7.89.0, PR-S3).

For the two stores that hold proxy secrets: ``proxy_sources.json`` (a
source's username and password) and ``proxy_chains.json``, whose URLs may
carry ``user:pass`` (the user's 2026-09-25 decision 6, "option A": the
credential stays in the chain store's URL, and that file is owner-only).

The same staging ``write_json_document_atomically`` uses -- a sibling file
renamed over the document, so a reader never sees half of one -- with the
restriction applied to the staged file **before a byte is written to it**:

* POSIX: the file is created ``0600`` and ``fchmod``-ed to it, whatever the
  umask says.
* Windows: mode bits mean nothing there, so the staged file is given an
  explicit, protected access list -- full control for this user, ``SYSTEM``
  and ``Administrators``, nothing inherited. Those three are exactly the
  entries a user profile directory grants, so under ``%USERPROFILE%\\.mcc``
  nothing changes in practice (the user's decision 8 of 2026-09-25 keeps the
  profile's ACL as the control); what it adds is that the same holds when the
  config directory is somewhere the profile's ACL does not reach -- another
  drive, ``MCC_CONFIG_DIR`` -- where an inherited "Users: read" would
  otherwise let every account on the machine read the file. ``os.replace``
  keeps the staged file's own security descriptor.

If the restriction cannot be applied, the document is still written -- a
store that cannot be saved loses the user's configuration, which is worse --
and the failure is logged once, without the path's contents.
"""

import os
import stat
import sys
import threading
from pathlib import Path

from loguru import logger

from my_claude_code.config.atomic_json import ATOMIC_TEMP_SUFFIX, json_document_bytes

#: The access list a Windows secret file gets: this user, SYSTEM and the
#: Administrators group, full control, protected from inheritance. ``{user}``
#: is the current process user's SID.
WINDOWS_OWNER_ONLY_SDDL = "D:P(A;;FA;;;{user})(A;;FA;;;SY)(A;;FA;;;BA)"

_WARNED = threading.Event()


def write_owner_only_json(path: Path, data: object) -> bool:
    """Write ``data`` as the same JSON bytes the atomic writer writes, owner-only.

    Returns whether the restriction was applied (it is the content that must
    land; the caller need not act on ``False``).
    """

    return write_owner_only_bytes(path, json_document_bytes(data))


def write_owner_only_bytes(path: Path, content: bytes) -> bool:
    """Write ``content`` to ``path`` atomically, readable by its owner only."""

    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + ATOMIC_TEMP_SUFFIX)
    restricted = False
    try:
        # A staging file a crashed writer left behind carries whatever access
        # it was created with: start from a fresh one.
        staged.unlink(missing_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(staged, flags, stat.S_IRUSR | stat.S_IWUSR)
        try:
            restricted = _restrict(staged, descriptor)
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
        finally:
            os.close(descriptor)
        os.replace(staged, path)
    except OSError:
        staged.unlink(missing_ok=True)
        raise
    if not restricted and not _WARNED.is_set():
        _WARNED.set()
        logger.warning(
            "CONFIG: could not restrict {} to its owner; it keeps the access "
            "its folder gives it",
            path.name,
        )
    return restricted


def _restrict(path: Path, descriptor: int) -> bool:
    if sys.platform == "win32":
        return windows_owner_only(str(path))
    try:
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        return False
    return True


def windows_owner_only(path: str) -> bool:
    """Give ``path`` :data:`WINDOWS_OWNER_ONLY_SDDL`. ``False`` if it could not.

    Plain Win32 through ``ctypes`` (the ``kernel32`` precedent in
    ``application/release_updates.py``): the current user's SID from the
    process token, the SDDL turned into a security descriptor, and its DACL
    set as protected so nothing is inherited.
    """

    if sys.platform != "win32":  # pragma: no cover - Windows only
        return False
    try:
        # Imported here, not at module scope: ``ctypes.wintypes`` raises on
        # non-Windows, and this module is imported on every platform (the
        # ``application/release_updates.py`` precedent).
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
            wintypes.BOOL
        )
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.ULONG),
        ]
        advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        advapi32.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.BOOL),
        ]
        advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
        advapi32.SetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]

        token_query = 0x0008
        token_user = 1
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            return False
        try:
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(
                token, token_user, None, 0, ctypes.byref(needed)
            )
            if not needed.value:
                return False
            buffer = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(
                token, token_user, buffer, needed, ctypes.byref(needed)
            ):
                return False
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is
            # the PSID.
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            text = ctypes.c_void_p()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                return False
            try:
                user = ctypes.wstring_at(text)
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)

        descriptor = ctypes.c_void_p()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            WINDOWS_OWNER_ONLY_SDDL.format(user=user), 1, ctypes.byref(descriptor), None
        ):
            return False
        try:
            present = wintypes.BOOL()
            dacl = ctypes.c_void_p()
            defaulted = wintypes.BOOL()
            if not advapi32.GetSecurityDescriptorDacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            ):
                return False
            se_file_object = 1
            dacl_security_information = 0x00000004
            protected_dacl_security_information = 0x80000000
            result = advapi32.SetNamedSecurityInfoW(
                path,
                se_file_object,
                dacl_security_information | protected_dacl_security_information,
                None,
                None,
                dacl,
                None,
            )
            return result == 0
        finally:
            kernel32.LocalFree(descriptor)
    except AttributeError, OSError, ValueError:
        return False


__all__ = [
    "WINDOWS_OWNER_ONLY_SDDL",
    "windows_owner_only",
    "write_owner_only_bytes",
    "write_owner_only_json",
]
