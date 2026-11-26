"""The only module that talks to Windows directly (through ``ctypes``).

Everything here is a thin wrapper with one job, so the rest of the program is
plain Python. The tests replace the functions of this module with fakes, which
is why other modules call them as ``winapi.name(...)`` and never import the
names directly.
"""

import ctypes
import functools
import logging
import os
import subprocess
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Iterator, Optional

from .errors import StepError

log = logging.getLogger("corridoor")

CRYPTPROTECT_UI_FORBIDDEN: int = 0x1
ERROR_ALREADY_EXISTS: int = 183
# Job object: information class and the flag "kill the members with the job".
JOB_EXTENDED_LIMIT_INFORMATION: int = 9
JOB_KILL_ON_CLOSE: int = 0x2000
# Keeps console programs from opening a window (the constant is Windows only).
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def system32(name: str) -> str:
    """Return the full path of a Windows tool, so ``PATH`` cannot hijack it."""
    folder = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    return str(folder / name)


def is_admin() -> bool:
    """Return True if the process is elevated."""
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


# --------------------------------------------------------------------------- #
# DPAPI: encryption bound to the current Windows user
# --------------------------------------------------------------------------- #

class DataBlob(ctypes.Structure):
    """The Win32 DATA_BLOB structure used by the DPAPI functions."""

    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.c_void_p)]


def dpapi(data: bytes, encrypt: bool) -> bytes:
    """Encrypt or decrypt bytes with DPAPI, bound to the current Windows user.

    Args:
        data: Plain bytes (encrypt) or protected bytes (decrypt).
        encrypt: Direction of the operation.

    Returns:
        The transformed bytes.

    Raises:
        OSError: If Windows rejects the operation.
    """
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(data, len(data))
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    result = DataBlob()

    function = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    blob_pointer = ctypes.POINTER(DataBlob)
    function.argtypes = [blob_pointer, ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                         blob_pointer]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(source), None, None, None, None,
                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)  # the output is allocated by Windows


# --------------------------------------------------------------------------- #
# Mutex, event and job object
# --------------------------------------------------------------------------- #

@contextmanager
def single_instance() -> Iterator[None]:
    """Allow only one running session (it owns the temporary files).

    Raises:
        StepError: If another session is already active.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel32.CreateMutexW(None, False, "corridoor_session")
    already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
    log.debug("session mutex: handle %s, already running: %s", mutex, already_running)
    try:
        if not mutex or already_running:
            raise StepError("another session is already running")
        yield
    finally:
        if mutex:
            kernel32.CloseHandle(mutex)


@contextmanager
def exit_event() -> Iterator[tuple[str, Callable[[], None]]]:
    """Create the event that asks OpenVPN to shut down cleanly.

    Yields:
        The event name and a function that signals the event.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    name = f"corridoor_exit_{os.getpid()}"
    handle = kernel32.CreateEventW(None, True, False, name)  # manual reset, unset
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    log.debug("exit event %s created", name)
    try:
        yield name, lambda: kernel32.SetEvent(handle)
    finally:
        kernel32.CloseHandle(handle)


class JobBasicLimits(ctypes.Structure):
    """Win32 JOBOBJECT_BASIC_LIMIT_INFORMATION."""

    _fields_ = [
        ("process_time_limit", ctypes.c_int64),
        ("job_time_limit", ctypes.c_int64),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class JobExtendedLimits(ctypes.Structure):
    """Win32 JOBOBJECT_EXTENDED_LIMIT_INFORMATION."""

    _fields_ = [
        ("basic", JobBasicLimits),
        ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


@contextmanager
def kill_on_close_job() -> Iterator[Callable[[subprocess.Popen], None]]:
    """Tie child processes to the lifetime of this program.

    Windows terminates all members of the job when its last handle is closed.
    This also happens if this program crashes or is killed, so no OpenVPN
    process (and tunnel) is left behind.

    Yields:
        A function that adds a started process to the job.

    Raises:
        OSError: If the job cannot be created or a process cannot be added.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    job = kernel32.CreateJobObjectW(None, None)  # unnamed, handle not inherited
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = JobExtendedLimits()
        limits.basic.limit_flags = JOB_KILL_ON_CLOSE
        if not kernel32.SetInformationJobObject(
                job, JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits),
                ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())

        log.debug("job object created (kill on close)")

        def assign(process: subprocess.Popen) -> None:
            log.debug("process %d added to the job", process.pid)
            # Popen keeps the Windows process handle in this attribute.
            if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
                error = ctypes.get_last_error()
                process.kill()  # never run an untracked tunnel
                raise ctypes.WinError(error)

        yield assign
    finally:
        kernel32.CloseHandle(job)


# --------------------------------------------------------------------------- #
# Starting programs without administrator rights
# --------------------------------------------------------------------------- #

# Token access: query, duplicate, assign as a primary token.
TOKEN_ACCESS: int = 0x8 | 0x2 | 0x1
TOKEN_USER: int = 1
TOKEN_ELEVATION_TYPE: int = 18
TOKEN_LINKED_TOKEN: int = 19
TOKEN_INTEGRITY_LEVEL: int = 25
ELEVATION_FULL: int = 2           # the token is the elevated half of a split token
MAXIMUM_ALLOWED: int = 0x02000000
SECURITY_IMPERSONATION: int = 2
TOKEN_PRIMARY: int = 1
DISABLE_MAX_PRIVILEGE: int = 0x1  # CreateRestrictedToken: drop all privileges
LUA_TOKEN: int = 0x4              # ... and make Administrators "deny only"
MEDIUM_INTEGRITY: str = "S-1-16-8192"
SE_GROUP_INTEGRITY: int = 0x20
TH32CS_SNAPPROCESS: int = 0x2
STILL_ACTIVE: int = 259
WAIT_TIMEOUT: int = 258
INFINITE: int = 0xFFFFFFFF


class StartupInfo(ctypes.Structure):
    """Win32 STARTUPINFOW."""

    _fields_ = [
        ("size", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD), ("y", wintypes.DWORD),
        ("x_size", wintypes.DWORD), ("y_size", wintypes.DWORD),
        ("x_chars", wintypes.DWORD), ("y_chars", wintypes.DWORD),
        ("fill_attribute", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("show_window", wintypes.WORD), ("reserved2", wintypes.WORD),
        ("reserved2_pointer", ctypes.c_void_p),
        ("std_input", ctypes.c_void_p), ("std_output", ctypes.c_void_p),
        ("std_error", ctypes.c_void_p),
    ]


class ProcessInformation(ctypes.Structure):
    """Win32 PROCESS_INFORMATION."""

    _fields_ = [("process", ctypes.c_void_p), ("thread", ctypes.c_void_p),
                ("process_id", wintypes.DWORD), ("thread_id", wintypes.DWORD)]


class SidAndAttributes(ctypes.Structure):
    """Win32 SID_AND_ATTRIBUTES (also the start of TOKEN_USER)."""

    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]


@functools.lru_cache(maxsize=None)
def _libraries() -> tuple:
    """Load kernel32 and advapi32 once, with the argument types set."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    handle, dword, pointer = ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p
    dword_pointer = ctypes.POINTER(dword)
    handle_pointer = ctypes.POINTER(handle)

    kernel32.GetCurrentProcess.restype = handle
    kernel32.CloseHandle.argtypes = [handle]
    kernel32.LocalFree.argtypes = [pointer]
    kernel32.LocalFree.restype = pointer
    kernel32.GetExitCodeProcess.argtypes = [handle, dword_pointer]
    kernel32.WaitForSingleObject.argtypes = [handle, dword]
    kernel32.WaitForSingleObject.restype = dword
    kernel32.TerminateProcess.argtypes = [handle, ctypes.c_uint]

    advapi32.OpenProcessToken.argtypes = [handle, dword, handle_pointer]
    advapi32.GetTokenInformation.argtypes = [handle, ctypes.c_int, pointer, dword,
                                             dword_pointer]
    advapi32.SetTokenInformation.argtypes = [handle, ctypes.c_int, pointer, dword]
    advapi32.DuplicateTokenEx.argtypes = [handle, dword, pointer, ctypes.c_int,
                                          ctypes.c_int, handle_pointer]
    advapi32.CreateRestrictedToken.argtypes = [
        handle, dword, dword, pointer, dword, pointer, dword, pointer, handle_pointer]
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR,
                                                ctypes.POINTER(pointer)]
    advapi32.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(pointer)]
    startup, information = ctypes.POINTER(StartupInfo), ctypes.POINTER(ProcessInformation)
    advapi32.CreateProcessAsUserW.argtypes = [
        handle, wintypes.LPCWSTR, wintypes.LPWSTR, pointer, pointer, wintypes.BOOL,
        dword, pointer, wintypes.LPCWSTR, startup, information]
    advapi32.CreateProcessWithTokenW.argtypes = [
        handle, dword, wintypes.LPCWSTR, wintypes.LPWSTR, dword, pointer,
        wintypes.LPCWSTR, startup, information]
    return kernel32, advapi32


def _open_own_token() -> int:
    """Open the access token of this process; the caller closes it."""
    kernel32, advapi32 = _libraries()
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_ACCESS,
                                     ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    return token.value


def user_sid() -> str:
    """Return the SID (text form) of the Windows user running this program.

    The elevated token and its limited twin belong to the same user, so this is
    also the user of the programs started by :func:`start_limited`.
    """
    kernel32, advapi32 = _libraries()
    token = _open_own_token()
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER, buffer, size.value,
                                            ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = SidAndAttributes.from_buffer(buffer).sid
        text = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.wstring_at(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def grant_read(path: Path, sid: str) -> None:
    """Let one user read a file that is otherwise reserved for administrators.

    Raises:
        OSError: If the access rule cannot be added.
    """
    try:
        subprocess.run([system32("icacls.exe"), str(path), "/grant", f"*{sid}:R"],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    except subprocess.CalledProcessError as error:
        raise OSError(f"cannot grant read access on {path}: {error}") from error


def _linked_token(token: int) -> Optional[int]:
    """Return a primary copy of the limited twin of an elevated token, or None.

    Only an elevated administrator token has one (UAC on, split token).
    """
    kernel32, advapi32 = _libraries()
    kind, size = wintypes.DWORD(), wintypes.DWORD()
    if not advapi32.GetTokenInformation(token, TOKEN_ELEVATION_TYPE, ctypes.byref(kind),
                                        ctypes.sizeof(kind), ctypes.byref(size)):
        return None
    if kind.value != ELEVATION_FULL:
        return None
    linked = ctypes.c_void_p()
    if not advapi32.GetTokenInformation(token, TOKEN_LINKED_TOKEN, ctypes.byref(linked),
                                        ctypes.sizeof(linked), ctypes.byref(size)):
        return None
    primary = ctypes.c_void_p()
    try:
        if not advapi32.DuplicateTokenEx(linked, MAXIMUM_ALLOWED, None,
                                         SECURITY_IMPERSONATION, TOKEN_PRIMARY,
                                         ctypes.byref(primary)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(linked)
    return primary.value


def _restricted_token(token: int) -> int:
    """Return a weakened copy of ``token``: no Administrators, no privileges, medium level."""
    kernel32, advapi32 = _libraries()
    restricted = ctypes.c_void_p()
    if not advapi32.CreateRestrictedToken(
            token, DISABLE_MAX_PRIVILEGE | LUA_TOKEN, 0, None, 0, None, 0, None,
            ctypes.byref(restricted)):
        raise ctypes.WinError(ctypes.get_last_error())
    # The handle returned above cannot be adjusted; a duplicate can.
    primary = ctypes.c_void_p()
    try:
        if not advapi32.DuplicateTokenEx(restricted, MAXIMUM_ALLOWED, None,
                                         SECURITY_IMPERSONATION, TOKEN_PRIMARY,
                                         ctypes.byref(primary)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(restricted)
    sid = ctypes.c_void_p()
    try:
        if not advapi32.ConvertStringSidToSidW(MEDIUM_INTEGRITY, ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        label = SidAndAttributes(sid, SE_GROUP_INTEGRITY)
        if not advapi32.SetTokenInformation(primary, TOKEN_INTEGRITY_LEVEL,
                                            ctypes.byref(label), ctypes.sizeof(label)):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel32.CloseHandle(primary)
        raise
    finally:
        if sid:
            kernel32.LocalFree(sid)
    return primary.value


def _create_process(token: int, linked: bool, command: list[str]) -> tuple[int, int]:
    """Start ``command`` with ``token``; return the process handle and id."""
    kernel32, advapi32 = _libraries()
    startup = StartupInfo()
    startup.size = ctypes.sizeof(startup)
    startup.desktop = r"winsta0\default"
    information = ProcessInformation()
    line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
    if linked:
        # A token from GetTokenInformation: needs SeImpersonatePrivilege (admins have it).
        ok = advapi32.CreateProcessWithTokenW(
            token, 0, None, line, 0, None, None, ctypes.byref(startup),
            ctypes.byref(information))
    else:
        # A restricted copy of our own token needs no extra privilege.
        ok = advapi32.CreateProcessAsUserW(
            token, None, line, None, None, False, 0, None, None,
            ctypes.byref(startup), ctypes.byref(information))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel32.CloseHandle(information.thread)
    return information.process, information.process_id


class LimitedProcess:
    """A started program, with the parts of ``subprocess.Popen`` the program uses.

    Attributes:
        pid: Process id.
        returncode: Exit code, None while the process runs.
        args: The command line as a list.
    """

    def __init__(self, handle: int, pid: int, args: list[str]) -> None:
        self._handle: int = handle  # also read by the job object code
        self.pid = pid
        self.args = args
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        """Return the exit code, or None while the process still runs."""
        if self.returncode is None:
            kernel32, _ = _libraries()
            code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(self._handle, ctypes.byref(code)):
                if code.value != STILL_ACTIVE:
                    self.returncode = code.value
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        """Wait for the end; raise ``subprocess.TimeoutExpired`` after ``timeout`` s."""
        kernel32, _ = _libraries()
        milliseconds = INFINITE if timeout is None else int(timeout * 1000)
        if kernel32.WaitForSingleObject(self._handle, milliseconds) == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.poll() or 0

    def terminate(self) -> None:
        """End the process at once (Windows has no gentler variant for windows)."""
        kernel32, _ = _libraries()
        if self.poll() is None:
            kernel32.TerminateProcess(self._handle, 1)

    kill = terminate

    def __del__(self) -> None:
        try:
            _libraries()[0].CloseHandle(self._handle)
        except Exception:
            pass  # interpreter shutdown; the handle dies with the process anyway


def start_limited(command: list[str]) -> LimitedProcess:
    """Start a program as the same user but without administrator rights.

    Two ways are tried in this order, and the first that works is used:

    1. the *linked token*: the limited twin that UAC keeps for every elevated
       administrator, which is what a double click in Explorer would use;
    2. a *restricted token* made from ours: Administrators only "deny only", no
       privileges, medium integrity level.

    Args:
        command: Program and arguments.

    Raises:
        OSError: If neither way works.
    """
    kernel32, _ = _libraries()
    own = _open_own_token()
    problems: list[str] = []
    try:
        for name, make, linked in (("linked token", _linked_token, True),
                                   ("restricted token", _restricted_token, False)):
            token = None
            try:
                token = make(own)
                if token is None:
                    log.debug("  %s: not available", name)
                    continue
                handle, pid = _create_process(token, linked, command)
            except OSError as error:
                log.debug("  %s: failed (%s)", name, error)
                problems.append(f"{name}: {error}")
                continue
            finally:
                if token:
                    kernel32.CloseHandle(token)
            log.debug("  started process %d with the %s", pid, name)
            return LimitedProcess(handle, pid, command)
    finally:
        kernel32.CloseHandle(own)
    raise OSError("; ".join(problems) or "no limited token available")


class ProcessEntry(ctypes.Structure):
    """Win32 PROCESSENTRY32W."""

    _fields_ = [
        ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
        ("process_id", wintypes.DWORD), ("heap_id", ctypes.c_size_t),
        ("module_id", wintypes.DWORD), ("threads", wintypes.DWORD),
        ("parent_id", wintypes.DWORD), ("priority", ctypes.c_long),
        ("flags", wintypes.DWORD), ("exe_file", ctypes.c_wchar * 260),
    ]


def process_running(image: str) -> bool:
    """Return True if a program with this file name runs in this Windows session.

    Used for programs that hand a request over to a running instance (FileZilla,
    HopToDesk): the process that was started is gone, but the program may still
    be open. Programs of other Windows sessions (other users) are not counted.

    Args:
        image: File name of the program, e.g. ``filezilla.exe`` (case is ignored).

    Raises:
        OSError: If the process list cannot be read.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot_pointer = ctypes.POINTER(ProcessEntry)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    for name in ("Process32FirstW", "Process32NextW"):
        function = getattr(kernel32, name)
        function.argtypes = [ctypes.c_void_p, snapshot_pointer]
        function.restype = wintypes.BOOL
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        mine = wintypes.DWORD()
        kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(mine))
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        found = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.exe_file.lower() == image.lower():
                theirs = wintypes.DWORD()
                if (kernel32.ProcessIdToSessionId(entry.process_id, ctypes.byref(theirs))
                        and theirs.value == mine.value):
                    return True
            found = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(snapshot)
