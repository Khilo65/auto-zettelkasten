from __future__ import annotations

import errno
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from auto_zettelkasten import windows_platform


SID = "S-1-5-21-100-200-300-1001"


@pytest.mark.parametrize("flags", ["P", "PAI"])
def test_private_acl_accepts_only_current_user_and_system(flags):
    windows_platform._validate_sddl(
        f"O:{SID}D:{flags}(A;OICI;FA;;;{SID})(A;OICI;FA;;;SY)", SID, protected=True
    )


@pytest.mark.parametrize("sddl", [
    f"O:{SID}D:(A;;FA;;;{SID})",  # unprotected directory
    f"O:SYD:P(A;;FA;;;{SID})",  # incorrect owner
    f"O:{SID}D:P(A;;FA;;;WD)",  # everyone
    f"O:{SID}D:P(A;;FA;;;{SID})(A;;FR;;;BA)",  # extra reader
    f"O:{SID}D:P(D;;FA;;;{SID})",  # deny-only ACL
    f"O:{SID}D:P",  # no usable access
    f"O:{SID}D:P(A;IO;FA;;;{SID})",  # inherit-only user access
    f"O:{SID}D:P(A;;FR;;;{SID})",  # read-only current user
])
def test_private_acl_rejects_unsafe_owner_or_permissions(sddl):
    with pytest.raises(OSError):
        windows_platform._validate_sddl(sddl, SID, protected=True)


def test_windows_byte_lock_uses_same_region_for_unlock(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        LK_UNLCK=0, LK_NBLCK=2,
        locking=lambda descriptor, mode, size: calls.append((os.lseek(descriptor, 0, os.SEEK_CUR), mode, size)),
    ))
    path = tmp_path / "ledger"
    path.write_bytes(b"existing payload")
    descriptor = os.open(path, os.O_RDWR)
    try:
        os.lseek(descriptor, 5, os.SEEK_SET)
        windows_platform.lock_descriptor(descriptor)
        os.lseek(descriptor, 8, os.SEEK_SET)
        windows_platform.lock_descriptor(descriptor, unlock=True)
    finally:
        os.close(descriptor)
    assert calls == [(0, 2, 1), (0, 0, 1)]
    assert path.read_bytes() == b"existing payload"


def test_windows_byte_lock_distinguishes_contention_from_io_failure(tmp_path, monkeypatch):
    failures = iter([OSError(errno.EACCES, "busy"), OSError(errno.EIO, "disk")])
    def lock(*args):
        raise next(failures)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_UNLCK=0, LK_NBLCK=2, locking=lock))
    descriptor = os.open(tmp_path / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with pytest.raises(BlockingIOError):
            windows_platform.lock_descriptor(descriptor)
        with pytest.raises(OSError) as error:
            windows_platform.lock_descriptor(descriptor)
        assert error.value.errno == errno.EIO
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows ACL and locks")
def test_native_windows_private_directory_and_cross_process_lock(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    windows_platform.secure_directory(private)
    windows_platform.verify_private_path(private, protected=True)
    lock_path = private / "subscription.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_BINARY, 0o600)
    try:
        windows_platform.verify_private_path(lock_path)
        windows_platform.lock_descriptor(descriptor)
        child = subprocess.run([
            sys.executable, "-c",
            "import os,sys; from auto_zettelkasten.windows_platform import lock_descriptor; "
            "fd=os.open(sys.argv[1],os.O_RDWR|os.O_BINARY); lock_descriptor(fd)",
            str(lock_path),
        ], capture_output=True, text=True, check=False)
        assert child.returncode != 0
        assert "BlockingIOError" in child.stderr
        windows_platform.lock_descriptor(descriptor, unlock=True)
        child = subprocess.run([
            sys.executable, "-c",
            "import os,sys; from auto_zettelkasten.windows_platform import lock_descriptor; "
            "fd=os.open(sys.argv[1],os.O_RDWR|os.O_BINARY); lock_descriptor(fd); "
            "lock_descriptor(fd,unlock=True); os.close(fd)",
            str(lock_path),
        ], capture_output=True, text=True, check=False)
        assert child.returncode == 0, child.stderr
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows directory junctions")
def test_native_windows_security_rejects_junction(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(target)], capture_output=True, check=False)
    assert result.returncode == 0
    with pytest.raises(OSError, match="reparse"):
        windows_platform.secure_directory(junction)


def test_subscription_guard_windows_branch_releases_after_error(tmp_path, monkeypatch):
    from auto_zettelkasten import codex_attempt_guard as guard

    platform_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    platform_os.name, platform_os.O_BINARY = "nt", 0
    monkeypatch.setattr(guard, "os", platform_os)
    monkeypatch.setattr(windows_platform, "subscription_lock_path", lambda: tmp_path / "lock")
    monkeypatch.setattr(windows_platform, "verify_private_path", lambda path: None)
    calls = []
    monkeypatch.setattr(windows_platform, "lock_descriptor", lambda fd, **kwargs: calls.append(kwargs.get("unlock", False)))
    with pytest.raises(RuntimeError, match="mapping failed"):
        with guard.codex_subscription_run_lock("codex"):
            raise RuntimeError("mapping failed")
    assert calls == [False, True]


def test_subscription_guard_windows_security_failure_never_locks(tmp_path, monkeypatch):
    from auto_zettelkasten import codex_attempt_guard as guard

    platform_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    platform_os.name, platform_os.O_BINARY = "nt", 0
    monkeypatch.setattr(guard, "os", platform_os)
    monkeypatch.setattr(windows_platform, "subscription_lock_path", lambda: tmp_path / "lock")
    def unsafe(path):
        raise OSError("unsafe ACL")
    monkeypatch.setattr(windows_platform, "verify_private_path", unsafe)
    monkeypatch.setattr(windows_platform, "lock_descriptor", lambda fd: pytest.fail("must not lock unsafe file"))
    with pytest.raises(guard.CodexAttemptStateError, match="unavailable"):
        with guard.codex_subscription_run_lock("codex"):
            pytest.fail("must not start mapping")


def test_frozen_live_gate_windows_ledger_fails_closed(tmp_path, monkeypatch):
    from auto_zettelkasten import codex_attempt_guard as guard

    platform_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    platform_os.name = "nt"
    monkeypatch.setattr(guard, "os", platform_os)
    with pytest.raises(guard.CodexAttemptStateError, match="Unix file locking"):
        guard._open_ledger(tmp_path / "ledger", os.O_RDWR | os.O_CREAT)
    assert not (tmp_path / "ledger").exists()


class FakeJobKernel:
    def __init__(self, *, assign=True):
        self.calls = []
        self.assign = assign

    def CreateJobObjectW(self, security, name):
        self.calls.append("create")
        assert security is None and name is None
        return 123

    def SetInformationJobObject(self, handle, info_class, limits, size):
        self.calls.append("limits")
        assert handle == 123 and info_class == 9
        assert limits._obj.BasicLimitInformation.LimitFlags == 0x2000
        return True

    def AssignProcessToJobObject(self, handle, process):
        self.calls.append("assign")
        assert handle == 123 and process == 456
        return self.assign

    def TerminateJobObject(self, handle, exit_code):
        self.calls.append("terminate")
        assert handle == 123 and exit_code == 1
        return True

    def CloseHandle(self, handle):
        self.calls.append("close")
        assert handle == 123
        return True


def test_job_sets_kill_on_close_before_assign_and_resume(monkeypatch):
    kernel = FakeJobKernel()
    monkeypatch.setattr(windows_platform, "_job_kernel", lambda: kernel)
    monkeypatch.setattr(windows_platform, "_resume_suspended_process", lambda k, pid: kernel.calls.append("resume"))
    process = SimpleNamespace(_handle=456, pid=789)
    job = windows_platform.ProcessJob(process)
    assert kernel.calls == ["create", "limits", "assign", "resume"]
    job.terminate()
    job.close()
    job.close()
    job.terminate()
    assert kernel.calls[-2:] == ["terminate", "close"]


def test_job_failed_assignment_closes_handle_kills_and_reaps_worker(monkeypatch):
    kernel = FakeJobKernel(assign=False)
    monkeypatch.setattr(windows_platform, "_job_kernel", lambda: kernel)
    def check(result):
        if not result:
            raise OSError("assignment refused")
    monkeypatch.setattr(windows_platform, "_check", check)
    monkeypatch.setattr(windows_platform, "_resume_suspended_process", lambda *args: pytest.fail("must never resume"))
    process = SimpleNamespace(_handle=456, pid=789, poll=lambda: None,
                              kill=lambda: kernel.calls.append("kill"),
                              wait=lambda **kwargs: kernel.calls.append("wait"))
    with pytest.raises(OSError, match="unable to contain") as error:
        windows_platform.ProcessJob(process)
    assert str(error.value.__cause__) == "assignment refused"
    assert kernel.calls == ["create", "limits", "assign", "close", "kill", "wait"]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
def test_native_job_close_terminates_worker_and_descendant(tmp_path):
    import ctypes
    from ctypes import wintypes
    import time

    pid_path = tmp_path / "descendant.pid"
    script = (
        "import pathlib,subprocess,sys,time; sys.stdin.readline(); "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", script, str(pid_path)],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=0x00000004)
    job = None
    descendant_handle = None
    kernel = windows_platform._job_kernel()
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    try:
        job = windows_platform.ProcessJob(process)
        process.stdin.write(b"go\n")
        process.stdin.flush()
        deadline = time.monotonic() + 10
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_path.exists(), "suspended worker did not resume and spawn its child"
        descendant_handle = kernel.OpenProcess(0x00100000, False, int(pid_path.read_text()))
        assert descendant_handle
        job.close()
        assert kernel.WaitForSingleObject(descendant_handle, 5000) == 0
        process.wait(timeout=5)
        job.close()
        assert job._handle is None
    finally:
        if job is not None:
            job.close()
        if descendant_handle:
            kernel.CloseHandle(descendant_handle)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdin.close()
