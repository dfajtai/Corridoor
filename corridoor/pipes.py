"""One-way named pipes that hand a secret to exactly one child process.

The pipe replaces a temporary file: the secret never touches the disk. Only
Administrators and SYSTEM may open it, and data is written only to the process
that was announced with :meth:`SecretPipe.allow`.
"""

from __future__ import annotations

import ctypes
import logging
import secrets
import threading
from ctypes import wintypes
from typing import Optional

PIPE_ACCESS_OUTBOUND: int = 0x00000002           # server writes, client reads
FILE_FLAG_FIRST_PIPE_INSTANCE: int = 0x00080000  # fail if the name is taken
PIPE_REJECT_REMOTE_CLIENTS: int = 0x00000008     # local processes only
ERROR_PIPE_CONNECTED: int = 535
# Full access for Administrators (BA) and SYSTEM (SY) only, nothing inherited.
SDDL: str = "D:P(A;;GA;;;BA)(A;;GA;;;SY)"
# How long a connected client is held while its process id is not announced yet.
ALLOW_TIMEOUT: float = 10.0

log: logging.Logger = logging.getLogger("corridoor")


class SecurityAttributes(ctypes.Structure):
    """Win32 SECURITY_ATTRIBUTES."""

    _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p),
                ("inherit", wintypes.BOOL)]


class SecretPipe:
    """Serve the same bytes to every connection of one allowed process.

    Serving more than once matters: a program may open its "file" several times.
    """

    def __init__(self, data: bytes) -> None:
        """Prepare a pipe with an unpredictable name; nothing is created yet."""
        self.path: str = rf"\\.\pipe\corridoor-{secrets.token_hex(16)}"
        self.delivered: bool = False  # True once the allowed process got all data
        self._data = data
        self._allowed_pid: Optional[int] = None
        self._allowed = threading.Event()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Create the pipe (it exists on return) and serve it in the background.

        Raises:
            OSError: If Windows cannot create the pipe.
        """
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, dword, pointer = ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p
        create = self._kernel32.CreateNamedPipeW
        create.argtypes = [wintypes.LPCWSTR, dword, dword, dword, dword, dword,
                           dword, pointer]
        create.restype = handle
        self._kernel32.ConnectNamedPipe.argtypes = [handle, pointer]
        self._kernel32.GetNamedPipeClientProcessId.argtypes = [
            handle, ctypes.POINTER(dword)]
        self._kernel32.WriteFile.argtypes = [
            handle, pointer, dword, ctypes.POINTER(dword), pointer]
        for name in ("FlushFileBuffers", "DisconnectNamedPipe", "CloseHandle"):
            getattr(self._kernel32, name).argtypes = [handle]

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        convert.argtypes = [wintypes.LPCWSTR, dword,
                            ctypes.POINTER(ctypes.c_void_p), pointer]
        descriptor = ctypes.c_void_p()
        if not convert(SDDL, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        # The few bytes of the descriptor are released when the process ends.
        self._attributes = SecurityAttributes(
            ctypes.sizeof(SecurityAttributes), descriptor, False)

        first = self._create_instance(first=True)
        self._thread = threading.Thread(target=self._serve, args=(first,), daemon=True)
        self._thread.start()

    def allow(self, process_id: int) -> None:
        """Announce the only process that may receive the data."""
        self._allowed_pid = process_id
        self._allowed.set()

    def close(self) -> None:
        """Stop serving. Connecting once unblocks the waiting server thread."""
        self._stopping.set()
        self._allowed.set()
        try:
            with open(self.path, "rb"):
                pass
        except OSError:
            pass  # busy or already gone; the daemon thread ends with the program
        if self._thread is not None:
            self._thread.join(timeout=3)

    def _create_instance(self, first: bool) -> int:
        """Create a pipe instance; two may exist, so there is no gap between them."""
        mode = PIPE_ACCESS_OUTBOUND | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
        size = max(len(self._data), 4096)
        handle = self._kernel32.CreateNamedPipeW(
            self.path, mode, PIPE_REJECT_REMOTE_CLIENTS, 2, size, size, 0,
            ctypes.byref(self._attributes))
        if handle == ctypes.c_void_p(-1).value:  # INVALID_HANDLE_VALUE
            raise ctypes.WinError(ctypes.get_last_error())
        return handle

    def _serve(self, handle: int) -> None:
        """Accept connections until closed and answer the allowed process."""
        while True:
            connected = self._kernel32.ConnectNamedPipe(handle, None)
            if not connected and ctypes.get_last_error() != ERROR_PIPE_CONNECTED:
                break
            # The child may connect before the parent has announced its id.
            self._allowed.wait(ALLOW_TIMEOUT)
            if self._stopping.is_set():
                break
            client = wintypes.DWORD()
            self._kernel32.GetNamedPipeClientProcessId(handle, ctypes.byref(client))
            following = self._create_instance(first=False)
            if client.value == self._allowed_pid:
                written = wintypes.DWORD()
                ok = self._kernel32.WriteFile(handle, self._data, len(self._data),
                                              ctypes.byref(written), None)
                self._kernel32.FlushFileBuffers(handle)  # wait for the reader
                if ok and written.value == len(self._data):
                    self.delivered = True
            else:
                log.warning("Pipe access by unexpected process %d refused.",
                            client.value)
            self._kernel32.DisconnectNamedPipe(handle)
            self._kernel32.CloseHandle(handle)
            handle = following
        self._kernel32.CloseHandle(handle)
