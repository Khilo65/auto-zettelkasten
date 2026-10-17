"""Windows private directories and per-user Codex serialization.

No environment-supplied user names are used as security identities. Native APIs
fail closed on filesystems that cannot enforce the required Windows ACL.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import errno
import os
from pathlib import Path
import re
import stat


_SECURITY_INFORMATION = 0x00000001 | 0x00000004  # OWNER | DACL


def _libraries():
    if os.name != "nt":
        raise OSError("Windows security APIs are unavailable on this platform")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcessToken": ([wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.BOOL),
        "GetTokenInformation": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "ConvertSidToStringSidW": ([ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)], wintypes.BOOL),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": ([wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "ConvertSecurityDescriptorToStringSecurityDescriptorW": ([ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "SetFileSecurityW": ([wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        "GetFileSecurityW": ([wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(advapi, name)
        function.argtypes, function.restype = args, result
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], wintypes.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    return advapi, kernel


def _check(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())


def current_user_sid() -> str:
    advapi, kernel = _libraries()
    token = wintypes.HANDLE()
    _check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)))
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        _check(advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)))
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        _check(advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)))
        try:
            return text.value
        finally:
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def _reject_reparse(path: Path) -> None:
    # Inspect the spelling of the path before resolve() can hide junctions.
    for candidate in (path, *path.parents):
        metadata = candidate.lstat()
        if metadata.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("private Windows paths cannot contain reparse points")


def _security_sddl(path: Path) -> str:
    advapi, kernel = _libraries()
    size = wintypes.DWORD()
    advapi.GetFileSecurityW(str(path), _SECURITY_INFORMATION, None, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(size.value)
    _check(advapi.GetFileSecurityW(str(path), _SECURITY_INFORMATION, buffer, size, ctypes.byref(size)))
    text = wintypes.LPWSTR()
    _check(advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, _SECURITY_INFORMATION, ctypes.byref(text), None))
    try:
        return text.value
    finally:
        kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _validate_sddl(sddl: str, sid: str, *, protected: bool) -> None:
    # Native SDDL abbreviates SYSTEM to SY and full file access to FA. Directory
    # inheritance flags may vary for children; no additional principals may read.
    match = re.fullmatch(r"O:([^:]+)D:([^()]*)((?:\([^()]+\))*)", sddl)
    if not match or match[1] != sid or (protected and "P" not in match[2]):
        raise OSError("private Windows path has an unsafe owner or DACL")
    aces = re.findall(r"\(([^()]+)\)", match[3])
    allowed = {sid, "SY", "S-1-5-18"}
    principals = set()
    for ace in aces:
        parts = ace.split(";")
        if (len(parts) != 6 or parts[0] != "A" or parts[2] not in {"FA", "0x1f01ff"}
                or parts[3] or parts[4] or parts[5] not in allowed
                or set(re.findall(r"..", parts[1])) - {"OI", "CI", "ID"}):
            raise OSError("private Windows path has unsafe access permissions")
        principals.add(parts[5])
    if sid not in principals or not aces:
        raise OSError("private Windows path is missing user access")


def verify_private_path(path: Path, *, protected: bool = False) -> None:
    path = Path(os.path.abspath(path))
    _reject_reparse(path)
    _validate_sddl(_security_sddl(path), current_user_sid(), protected=protected)


def secure_directory(path: Path) -> None:
    """Protect an existing directory before copying credentials into it."""
    path = Path(os.path.abspath(path))
    _reject_reparse(path)
    if not path.is_dir():
        raise OSError("private Windows path must be a directory")
    sid = current_user_sid()
    # Never take ownership of an attacker-owned preexisting directory.
    if not _security_sddl(path).startswith(f"O:{sid}D:"):
        raise OSError("private Windows directory is owned by another identity")
    advapi, kernel = _libraries()
    descriptor = ctypes.c_void_p()
    _check(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)", 1,
        ctypes.byref(descriptor), None,
    ))
    try:
        _check(advapi.SetFileSecurityW(str(path), _SECURITY_INFORMATION | 0x80000000, descriptor))
    finally:
        kernel.LocalFree(descriptor)
    verify_private_path(path, protected=True)


def subscription_lock_path() -> Path:
    _libraries()
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    shell.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(32768)
    result = shell.SHGetFolderPathW(None, 0x001C, None, 0, buffer)  # LOCAL_APPDATA
    if result != 0 or not buffer.value:
        raise OSError("unable to locate the user's local application data")
    directory = Path(buffer.value) / "Auto-Zettelkasten" / "private-locks"
    directory.parent.mkdir(exist_ok=True)
    directory.mkdir(exist_ok=True)
    secure_directory(directory)
    return directory / "codex-subscription.lock"


def lock_descriptor(descriptor: int, *, unlock: bool = False) -> None:
    """Lock a stable byte beyond EOF; do not insert bytes into JSON ledgers."""
    import msvcrt

    os.lseek(descriptor, 0, os.SEEK_SET)
    try:
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        if not unlock and exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise BlockingIOError("another Codex subscription run is active") from exc
        raise


class _JobBasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
    )]


class _JobExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32), ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32), ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


def _job_kernel():
    _, kernel = _libraries()
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
        "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
        "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
    }
    for name, (args, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    return kernel


def _resume_suspended_process(kernel, pid: int) -> None:
    snapshot = kernel.CreateToolhelp32Snapshot(0x00000004, 0)  # SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entry = _ThreadEntry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        found = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == pid:
                thread = kernel.OpenThread(0x0002, False, entry.th32ThreadID)
                _check(thread)
                try:
                    count = kernel.ResumeThread(thread)
                    if count == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    if count != 1:
                        raise OSError("Codex worker must start with CREATE_SUSPENDED")
                    return
                finally:
                    kernel.CloseHandle(thread)
            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise OSError("unable to locate the suspended Codex worker thread")
    finally:
        kernel.CloseHandle(snapshot)


class ProcessJob:
    """Contain a CREATE_SUSPENDED worker before it can spawn descendants.

    Windows 10/11 nested job support is required. Closing our non-inheritable,
    unnamed job handle kills every remaining worker and descendant.
    """

    def __init__(self, process, *, resume: bool = True):
        import subprocess
        import threading

        self._lock = threading.Lock()
        self._handle = None
        try:
            self._kernel = _job_kernel()
            self._handle = self._kernel.CreateJobObjectW(None, None)
            _check(self._handle)
            limits = _JobExtendedLimit()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            _check(self._kernel.SetInformationJobObject(
                self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
            ))
            _check(self._kernel.AssignProcessToJobObject(self._handle, int(process._handle)))
            if resume:
                _resume_suspended_process(self._kernel, process.pid)
        except (OSError, AttributeError, TypeError, ValueError) as exc:
            try:
                try:
                    self.close()
                finally:
                    # A failed assignment leaves a suspended child outside the job.
                    # Reap it before any credential-bearing stdin can be supplied.
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                raise OSError("unable to clean up the uncontained Codex worker") from cleanup_error
            raise OSError("unable to contain the Windows Codex worker in a Job Object") from exc

    def terminate(self) -> None:
        with self._lock:
            if self._handle:
                _check(self._kernel.TerminateJobObject(self._handle, 1))

    def close(self) -> None:
        with self._lock:
            if self._handle:
                _check(self._kernel.CloseHandle(self._handle))
                self._handle = None
