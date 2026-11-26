"""The only module that talks to Windows directly (through ``ctypes``).

Everything here is a thin wrapper with one job, so the rest of the program is
plain Python. The tests replace the functions of this module with fakes, which
is why other modules call them as ``winapi.name(...)`` and never import the
names directly.
"""

import ctypes
import logging
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

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
