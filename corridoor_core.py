"""Logic of the tool: encrypted storage, profile import and the connection steps.

Everything lives in a folder that only Administrators and SYSTEM can access:

* ``settings.bin``  DPAPI-encrypted settings including the passwords
* ``profile.bin``   optional DPAPI-encrypted copy of the .ovpn profile
* ``corridoor.log`` plain-text log (never contains passwords)
"""

import base64
import ctypes
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Iterator, Optional

from corridoor_pipes import SecretPipe

SECRET_DIR: Path = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Corridoor"
SETTINGS_FILE: Path = SECRET_DIR / "settings.bin"
PROFILE_FILE: Path = SECRET_DIR / "profile.bin"
LOG_FILE: Path = SECRET_DIR / "corridoor.log"

READY_MARKER: str = "Initialization Sequence Completed"
VPN_TIMEOUT: float = 60.0
SSH_TIMEOUT: float = 30.0
TCP_TIMEOUT: float = 5.0
# Default port of the "Direct IP access" option of HopToDesk.
HOP_DEFAULT_PORT: str = "21118"
# Oldest version seen working; 1.44.4 on the remote side kept reconnecting.
HOP_MIN_VERSION: str = "1.46.90"
# Read-only shell command for a Linux remote host; prints "key=value" lines.
# Only single quotes are used, so the text survives Windows argument quoting.
HOP_PROBE: str = (
    r"dpkg-query -W -f='package=${db:Status-Status} ${Version}\n' hoptodesk 2>/dev/null; "
    r"rpm -q --qf 'package=installed %{VERSION}\n' hoptodesk 2>/dev/null; "
    "echo command=$(command -v hoptodesk); "
    "echo service=$(systemctl is-active hoptodesk 2>/dev/null); "
    "echo processes=$(ps -e -o comm= | grep -ci '^hoptodesk')"
)
# PuTTY reads the password file while parsing its command line; delete it after.
PWFILE_LIFETIME: float = 10.0

CRYPTPROTECT_UI_FORBIDDEN: int = 0x1
ERROR_ALREADY_EXISTS: int = 183
# Job object: information class and the flag "kill the members with the job".
JOB_EXTENDED_LIMIT_INFORMATION: int = 9
JOB_KILL_ON_CLOSE: int = 0x2000
# Keeps console programs from opening a window (the constant is Windows only).
NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Would reroute all traffic, load a DLL or pull in an unchecked file.
FORBIDDEN_DIRECTIVES: tuple[str, ...] = (
    "redirect-gateway", "redirect-private", "plugin", "config")
# Directives whose file argument can be embedded as an inline <block>.
INLINE_DIRECTIVES: tuple[str, ...] = (
    "ca", "cert", "key", "extra-certs", "pkcs12", "tls-auth", "tls-crypt",
    "tls-crypt-v2", "crl-verify", "secret", "dh")
# Known OpenVPN log fragments and what they usually mean.
VPN_HINTS: dict[str, str] = {
    "AUTH_FAILED": "wrong VPN user name or password",
    "TLS key negotiation failed": "server unreachable (address, port, firewall) "
                                  "or wrong tls-auth/tls-crypt key",
    "Cannot resolve host address": "the VPN server name cannot be resolved",
    "Access is denied": "missing administrator rights",
    "private key password": "encrypted private keys are not supported",
}

log: logging.Logger = logging.getLogger("corridoor")


class StepError(Exception):
    """A connection step failed; the message explains why."""


class Cancelled(Exception):
    """The user asked to disconnect."""


@dataclass
class Settings:
    """Everything the user can configure."""

    profile_file: str = ""
    copy_profile: bool = True
    vpn_user: str = ""
    vpn_password: str = ""
    host: str = ""
    port: str = "22"
    ssh_user: str = ""
    ssh_password: str = ""
    openvpn_exe: str = r"C:\Program Files\OpenVPN\bin\openvpn.exe"
    putty_exe: str = r"C:\Program Files\PuTTY\putty.exe"
    use_ssh: bool = True
    ssh_key: str = ""
    use_hoptodesk: bool = False
    hoptodesk_exe: str = r"C:\Program Files\HopToDesk\hoptodesk.exe"
    hoptodesk_port: str = HOP_DEFAULT_PORT
    # Test features: pass data through named pipes instead of temporary files.
    pipe_vpn_config: bool = False  # only used with a copied profile
    pipe_vpn_auth: bool = False
    pipe_ssh: bool = False


@dataclass
class Profile:
    """A checked OpenVPN profile."""

    text: str
    path: Optional[Path]  # the user's file; None for the copy from the store


# --------------------------------------------------------------------------- #
# Windows helpers
# --------------------------------------------------------------------------- #

def is_admin() -> bool:
    """Return True if the process is elevated."""
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


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


def prepare_storage() -> None:
    """Create the admin-only folder and attach the file logger to it.

    Raises:
        OSError, subprocess.CalledProcessError: If the folder cannot be secured.
    """
    SECRET_DIR.mkdir(exist_ok=True)
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    icacls = str(system32 / "icacls.exe")  # full path: no PATH hijacking
    admins, system = "*S-1-5-32-544", "*S-1-5-18"  # language independent SIDs
    for options in (
        ["/setowner", admins],
        ["/inheritance:r", "/grant:r", f"{admins}:(OI)(CI)F", f"{system}:(OI)(CI)F"],
    ):
        subprocess.run([icacls, str(SECRET_DIR), *options], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)

    handler = RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=2,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


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

        def assign(process: subprocess.Popen) -> None:
            # Popen keeps the Windows process handle in this attribute.
            if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
                error = ctypes.get_last_error()
                process.kill()  # never run an untracked tunnel
                raise ctypes.WinError(error)

        yield assign
    finally:
        kernel32.CloseHandle(job)


# --------------------------------------------------------------------------- #
# Profile handling and storage
# --------------------------------------------------------------------------- #

def split_directive(line: str) -> list[str]:
    """Split a config line into words; comments and empty lines give ``[]``."""
    stripped = line.strip()
    if not stripped or stripped[0] in "#;":
        return []
    words = shlex.split(stripped, comments=True)
    if words:
        words[0] = words[0].removeprefix("--")
    return words


def iter_directives(text: str) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(line, words)``; lines of inline <blocks> get an empty word list."""
    closing: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        if closing is not None:
            if stripped == closing:
                closing = None
            yield line, []
        elif (stripped.startswith("<") and stripped.endswith(">")
              and not stripped.startswith("</") and stripped != "<connection>"):
            closing = f"</{stripped[1:]}"
            yield line, []
        else:
            yield line, split_directive(line)


def check_profile(text: str) -> None:
    """Reject forbidden directives.

    Raises:
        StepError: Naming the offending line.
    """
    for line, words in iter_directives(text):
        if words and words[0] in FORBIDDEN_DIRECTIVES:
            raise StepError(f"remove this line from the profile: {line.strip()}")


def bundle_profile(path: Path) -> str:
    """Turn a profile and its referenced files into one self-contained text.

    Args:
        path: The original .ovpn file; relative references start at its folder.

    Returns:
        The profile with all certificate and key files embedded inline.

    Raises:
        StepError: If a referenced file is missing or cannot be embedded.
    """
    output: list[str] = []
    for line, words in iter_directives(path.read_text("utf-8-sig", errors="replace")):
        name = words[0] if words else ""
        if name == "askpass" and len(words) > 1:
            raise StepError("'askpass <file>' cannot be imported")
        if name == "auth-user-pass":
            output.append("auth-user-pass")  # credentials come from the settings
        elif name in INLINE_DIRECTIVES and len(words) > 1 and words[1] != "[inline]":
            referenced = path.parent / words[1]  # absolute paths stay unchanged
            if not referenced.is_file():
                raise StepError(f"file referenced by '{name}' not found: {referenced}")
            if name == "pkcs12":  # binary file, stored as base64
                content = base64.encodebytes(referenced.read_bytes()).decode("ascii")
            else:
                content = referenced.read_text("utf-8", errors="replace")
            if name == "tls-auth" and len(words) > 2:
                output.append(f"key-direction {words[2]}")
            output += [f"<{name}>", content.rstrip("\n"), f"</{name}>"]
        else:
            output.append(line)
    return "\n".join(output) + "\n"


# --------------------------------------------------------------------------- #
# Reachability checks
# --------------------------------------------------------------------------- #

def vpn_remotes(text: str) -> list[tuple[str, int, str]]:
    """Return the ``(host, port, protocol)`` entries of the VPN servers.

    ``port`` and ``proto`` lines are the defaults for ``remote`` lines that do
    not specify them. This is a diagnostic helper, not a full config parser.
    """
    directives = [words for _, words in iter_directives(text) if len(words) > 1]
    defaults = {"port": "1194", "proto": "udp"}
    for words in directives:
        key = "port" if words[0] == "rport" else words[0]
        if key in defaults:
            defaults[key] = words[1]
    remotes: list[tuple[str, int, str]] = []
    for words in directives:
        if words[0] != "remote":
            continue
        port = words[2] if len(words) > 2 else defaults["port"]
        protocol = words[3] if len(words) > 3 else defaults["proto"]
        if port.isdecimal():
            remotes.append((words[1], int(port),
                            "tcp" if protocol.startswith("tcp") else "udp"))
    return remotes


def ping(address: str) -> bool:
    """Send two ICMP echo requests with the Windows ping tool (IPv4).

    The exit code is not reliable ("destination unreachable" counts as a reply),
    therefore a ``TTL=`` field is required. It is the same in every language.
    """
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    try:
        result = subprocess.run(
            [str(system32 / "ping.exe"), "-4", "-n", "2", "-w", "2000", address],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=15, creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False
    return "TTL=" in result.stdout.upper()


def source_address(target_ip: str, port: int) -> str:
    """Return the local address the system would use to reach the target.

    Connecting a UDP socket only consults the routing table, nothing is sent.
    A changed address after the VPN is up shows that the route uses the tunnel.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((target_ip, port))
        return probe.getsockname()[0]


def load_settings() -> Optional[Settings]:
    """Return the saved settings, or None if nothing has been saved yet."""
    if not SETTINGS_FILE.is_file():
        return None
    stored = json.loads(dpapi(SETTINGS_FILE.read_bytes(), encrypt=False))
    # Ignore options that a different version of the program has saved.
    known = {field.name for field in fields(Settings)}
    return Settings(**{key: value for key, value in stored.items() if key in known})


def save_settings(settings: Settings) -> None:
    """Encrypt and store the settings and, if requested, the profile copy.

    Raises:
        StepError: If the profile cannot be imported.
        OSError: If writing or encryption fails.
    """
    if settings.copy_profile:
        source = Path(settings.profile_file)
        previous = load_settings()
        if source.is_file():
            bundle = bundle_profile(source)
            check_profile(bundle)
            PROFILE_FILE.write_bytes(dpapi(bundle.encode("utf-8"), encrypt=True))
            log.info("Profile copied into the encrypted store.")
        elif not (PROFILE_FILE.is_file() and previous
                  and previous.profile_file == settings.profile_file):
            raise StepError(f"profile not found: {source}")
    else:
        PROFILE_FILE.unlink(missing_ok=True)
    raw = json.dumps(asdict(settings)).encode("utf-8")
    SETTINGS_FILE.write_bytes(dpapi(raw, encrypt=True))
    log.info("Settings saved.")


# --------------------------------------------------------------------------- #
# Connection session
# --------------------------------------------------------------------------- #

class Session:
    """One connection attempt, either as a test or as real work."""

    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        """Store the settings and the event that requests a disconnect."""
        self.settings = settings
        self.stop = stop
        self.temp_files: list[Path] = []
        self.pipes: list[SecretPipe] = []
        self.auth_failed = False  # set when the VPN server rejects the login
        self.log_reader: Optional[threading.Thread] = None  # OpenVPN output
        self.step_number = 0
        self.step_count = 0
        self.warned = False

    @contextmanager
    def step(self, title: str) -> Iterator[None]:
        """Log the start and the outcome of one numbered step."""
        if self.stop.is_set():
            raise Cancelled
        self.step_number += 1
        self.warned = False
        label = f"[{self.step_number}/{self.step_count}] {title}"
        log.info("%s ...", label)
        try:
            yield
        except Cancelled:
            raise
        except Exception as error:
            log.error("%s FAILED: %s", label, error)
            raise StepError(str(error)) from error
        log.info("%s %s", label, "done with WARNING" if self.warned else "OK")

    def warn(self, message: str) -> None:
        """Report a problem that does not stop the session."""
        self.warned = True
        log.warning("  warning: %s", message)

    def temp_file(self, content: str) -> Path:
        """Write secret content into the admin-only folder; removed at the end."""
        handle, name = tempfile.mkstemp(prefix="tmp_", suffix=".txt", dir=SECRET_DIR)
        path = Path(name)
        self.temp_files.append(path)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        return path

    def secret(self, content: str,
               use_pipe: bool) -> tuple[str, Callable[[int], None]]:
        """Offer secret content to a child process.

        Args:
            content: Text the child process has to read.
            use_pipe: Use a named pipe instead of a temporary file.

        Returns:
            The path to give to the program, and a function that must be called
            with the process id right after the program has been started.
        """
        if not use_pipe:
            return str(self.temp_file(content)), lambda process_id: None
        pipe = SecretPipe(content.encode("utf-8"))
        pipe.start()
        self.pipes.append(pipe)
        return pipe.path, pipe.allow

    def cleanup(self) -> None:
        """Close all pipes and delete all temporary files of this session."""
        while self.pipes:
            self.pipes.pop().close()
        for path in self.temp_files:
            path.unlink(missing_ok=True)

    def check_programs(self, clients: tuple[str, ...]) -> None:
        """Verify that OpenVPN and the executables of the clients exist.

        Args:
            clients: Any of ``"ssh"`` and ``"hoptodesk"``.
        """
        programs = [Path(self.settings.openvpn_exe)]
        if "ssh" in clients:
            putty = Path(self.settings.putty_exe)
            programs += [putty, putty.with_name("plink.exe")]
            if self.settings.ssh_key:  # not a program, but it must exist too
                programs.append(Path(self.settings.ssh_key))
        if "hoptodesk" in clients:
            programs.append(Path(self.settings.hoptodesk_exe))
        for program in programs:
            if not program.is_file():
                raise StepError(f"file not found: {program}")

    def enabled_clients(self) -> tuple[str, ...]:
        """Return the clients switched on in the settings."""
        choices = (("ssh", self.settings.use_ssh),
                   ("hoptodesk", self.settings.use_hoptodesk))
        return tuple(name for name, enabled in choices if enabled)

    def prepare_profile(self) -> Profile:
        """Load and check the profile."""
        path: Optional[Path] = None
        if self.settings.copy_profile:
            text = dpapi(PROFILE_FILE.read_bytes(), encrypt=False).decode("utf-8")
        else:
            path = Path(self.settings.profile_file).resolve()
            text = path.read_text("utf-8-sig", errors="replace")
        check_profile(text)
        return Profile(text, path)

    def check_vpn_server(self, profile: Profile) -> None:
        """Check before connecting whether the VPN server can be reached.

        Name resolution must work. A TCP server must accept a connection. A UDP
        port cannot be probed reliably, only the VPN handshake can prove it.

        Raises:
            StepError: If no server resolves, or only TCP servers are configured
                and none of them accepts a connection.
        """
        text = profile.text
        remotes = vpn_remotes(text)
        if not remotes:
            self.warn("no 'remote' line found in the profile, check skipped")
            return
        # Behind a proxy a direct test would give a false error.
        proxied = any(words and words[0] in ("http-proxy", "socks-proxy")
                      for _, words in iter_directives(text))
        usable = False
        for host, port, protocol in remotes:
            if self.stop.is_set():
                raise Cancelled
            name = f"{host}:{port}/{protocol}"
            try:
                info = socket.getaddrinfo(host, port)
            except OSError as error:
                log.warning("  %s: cannot resolve (%s)", name, error)
                continue
            address = info[0][4][0]
            log.info("  %s resolves to %s", name, address)
            if ":" not in address:  # IPv4
                reply = ping(address)
                log.info("  ping %s: %s", address,
                         "reply" if reply else "no reply (ICMP is often blocked)")
            if proxied:
                log.info("  proxy configured, direct port test skipped")
                usable = True
            elif protocol == "udp":
                log.info("  UDP port cannot be tested here, the handshake will show")
                usable = True
            else:
                try:
                    with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
                        log.info("  TCP port %d is open", port)
                        usable = True
                except OSError as error:
                    log.warning("  TCP port %d not reachable: %s", port, error)
        if not usable:
            raise StepError("no VPN server is reachable (internet, address, "
                            "port, firewall)")

    def ping_target(self, target_ip: str) -> None:
        """Ping the host inside the VPN; a missing reply is only a warning."""
        if ping(target_ip):
            log.info("  ping %s: reply", target_ip)
        else:
            self.warn(f"{target_ip} does not answer to ping (host down, routing "
                      "problem, or ICMP blocked); the port test decides")

    def start_vpn(self, profile: Profile, target_ip: str,
                  event_name: str) -> tuple[subprocess.Popen, threading.Event]:
        """Launch OpenVPN so that only the target host uses the tunnel.

        Returns:
            The process and an event that is set when the tunnel is up.
        """
        announce: list[Callable[[int], None]] = []
        if profile.path is None:  # decrypted copy from the store
            config, allow = self.secret(profile.text, self.settings.pipe_vpn_config)
            announce.append(allow)
            folder = SECRET_DIR
        else:
            config, folder = str(profile.path), profile.path.parent
        command = [
            self.settings.openvpn_exe,
            "--config", config,
            "--route-nopull",                         # ignore pushed routes/DNS
            "--route", target_ip, "255.255.255.255",  # route only the SSH target
            "--service", event_name, "0",             # allows a clean shutdown
            "--script-security", "1",                 # no user scripts as admin
        ]
        if self.settings.vpn_user:
            auth, allow = self.secret(
                f"{self.settings.vpn_user}\n{self.settings.vpn_password}\n",
                self.settings.pipe_vpn_auth)
            announce.append(allow)
            command += ["--auth-user-pass", auth]
        process = subprocess.Popen(
            command, cwd=folder, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace", creationflags=NO_WINDOW,
        )
        for allow in announce:
            allow(process.pid)
        ready = threading.Event()

        def forward_log() -> None:
            """Drain the output (a full pipe would block OpenVPN) and log it."""
            assert process.stdout is not None
            for line in process.stdout:
                log.info("  openvpn: %s", line.rstrip())
                if "AUTH_FAILED" in line:
                    self.auth_failed = True
                for fragment, hint in VPN_HINTS.items():
                    if fragment in line:
                        log.warning("  hint: %s", hint)
                if READY_MARKER in line:
                    ready.set()

        self.log_reader = threading.Thread(target=forward_log, daemon=True)
        self.log_reader.start()
        return process, ready

    def wait_for_vpn(self, vpn: subprocess.Popen, ready: threading.Event) -> None:
        """Block until the tunnel is up, OpenVPN exits, or the time runs out."""
        deadline = time.monotonic() + VPN_TIMEOUT
        while not ready.wait(0.2):
            if self.stop.is_set():
                raise Cancelled
            exited = vpn.poll() is not None
            if exited and self.log_reader is not None:
                self.log_reader.join(timeout=2)  # the reason may still be unread
            if self.auth_failed:  # give up at once, retries could lock the account
                raise StepError("the VPN server rejected the login (AUTH_FAILED)")
            if exited:
                raise StepError(f"OpenVPN exited with code {vpn.returncode}")
            if time.monotonic() > deadline:
                raise StepError(f"no connection within {VPN_TIMEOUT:.0f} s")

    def check_ssh_port(self, target_ip: str) -> None:
        """Open a TCP connection and expect an SSH greeting."""
        address = (target_ip, int(self.settings.port))
        with socket.create_connection(address, timeout=10) as connection:
            banner = connection.recv(256)
        if b"SSH-" not in banner:
            raise StepError("the port is open but does not answer like an SSH server")

    def check_login(self, target_ip: str) -> None:
        """Log in non-interactively with plink and run ``exit``."""
        self.ssh_command(target_ip, "exit")

    def ssh_command(self, target_ip: str, remote_command: str) -> str:
        """Run a command on the target with plink, without any user interaction.

        Returns:
            The standard output of the remote command.

        Raises:
            StepError: If the login or the command fails.
        """
        plink = Path(self.settings.putty_exe).with_name("plink.exe")
        command = [str(plink), "-ssh", "-batch", "-P", self.settings.port,
                   *self.key_option()]
        announce: Callable[[int], None] = lambda process_id: None
        if self.settings.ssh_password:
            password, announce = self.secret(self.settings.ssh_password + "\n",
                                             self.settings.pipe_ssh)
            command += ["-pwfile", password]
        command += [f"{self.settings.ssh_user}@{target_ip}", remote_command]
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            creationflags=NO_WINDOW,
        )
        announce(process.pid)
        try:
            output, errors = process.communicate(timeout=SSH_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        if process.returncode != 0:
            message = errors.strip() or f"plink exit code {process.returncode}"
            if "host key" in message.lower():
                message += ("\nStart the normal mode once and verify and accept the "
                            "host key in PuTTY, then test again.")
            raise StepError(message)
        return output

    def key_option(self) -> list[str]:
        """Return the PuTTY / plink arguments selecting the private key, if any."""
        return ["-i", self.settings.ssh_key] if self.settings.ssh_key else []

    def check_hoptodesk_port(self, target_ip: str) -> None:
        """Check that the direct IP port of HopToDesk accepts connections.

        This proves reachability only; the HopToDesk password is not tested.
        """
        address = (target_ip, int(self.settings.hoptodesk_port))
        try:
            with socket.create_connection(address, timeout=10):
                pass
        except OSError as error:
            raise StepError(
                f"port {address[1]} not reachable ({error}). On the remote machine "
                "enable Settings > Network > Direct IP access and allow the port "
                "in the firewall.") from error

    def check_remote_hoptodesk(self, target_ip: str) -> None:
        """Ask the Linux target over SSH whether HopToDesk is ready.

        Reports the installed package version, the service state and the number
        of HopToDesk processes. Nothing is changed on the remote machine.

        Raises:
            StepError: If the answer is unusable, or HopToDesk is not installed
                or not running.
        """
        output = self.ssh_command(target_ip, HOP_PROBE)
        values = dict(line.strip().split("=", 1)
                      for line in output.splitlines() if "=" in line)
        if "processes" not in values:
            raise StepError("no usable answer (this check needs a Linux target)")
        state, _, version = values.get("package", "").partition(" ")
        installed = state == "installed"
        if not installed and not values.get("command"):
            raise StepError("HopToDesk is not installed on the remote machine")

        service = values.get("service") or "unknown"
        processes = int(values["processes"] or 0)
        log.info("  installed version: %s",
                 version if installed else f"unknown ({values['command']})")
        log.info("  service: %s, running processes: %d", service, processes)
        if service != "active" and processes == 0:
            raise StepError("HopToDesk is installed but not running")

        def numbers(text: str) -> list[int]:
            """Split a version string into comparable numbers."""
            return [int(part) for part in re.findall(r"\d+", text)[:3]]

        if not installed:
            self.warn("no package information, the version cannot be checked")
        elif numbers(version) < numbers(HOP_MIN_VERSION):
            self.warn(f"version {version} is older than {HOP_MIN_VERSION}, "
                      "update HopToDesk on the remote machine")

    def run_hoptodesk(self, target_ip: str, vpn: subprocess.Popen) -> None:
        """Start a direct IP session and keep the tunnel up until disconnect.

        The launched process may hand the request over to an already running
        HopToDesk instance and exit at once, so its end says nothing about the
        session. The tunnel therefore stays until the user disconnects.
        """
        peer = target_ip
        if self.settings.hoptodesk_port != HOP_DEFAULT_PORT:
            peer = f"{target_ip}:{self.settings.hoptodesk_port}"
        # No password argument: "--password" would change this computer's setting.
        subprocess.Popen([self.settings.hoptodesk_exe, "--connect", peer])
        log.info("  Enter the password in HopToDesk. Click Disconnect when done.")
        while not self.stop.wait(0.2):
            if vpn.poll() is not None:
                raise StepError("OpenVPN stopped unexpectedly")
        raise Cancelled

    def run_putty(self, target_ip: str, vpn: subprocess.Popen) -> None:
        """Start PuTTY and wait until it is closed or a disconnect is requested."""
        login = target_ip
        if self.settings.ssh_user:
            login = f"{self.settings.ssh_user}@{target_ip}"
        command = [self.settings.putty_exe, "-ssh", login, "-P", self.settings.port,
                   *self.key_option()]
        announce: Callable[[int], None] = lambda process_id: None
        password: Optional[str] = None
        if self.settings.ssh_password:
            password, announce = self.secret(self.settings.ssh_password + "\n",
                                             self.settings.pipe_ssh)
            command += ["-pwfile", password]
        putty = subprocess.Popen(command)
        announce(putty.pid)
        started = time.monotonic()
        while putty.poll() is None:
            if self.stop.wait(0.2):
                raise Cancelled
            # A temporary file is removed early; a pipe only serves PuTTY anyway.
            if (password and not self.settings.pipe_ssh
                    and time.monotonic() - started > PWFILE_LIFETIME):
                Path(password).unlink(missing_ok=True)
            if vpn.poll() is not None:
                raise StepError("OpenVPN stopped unexpectedly")

    @contextmanager
    def vpn_tunnel(self) -> Iterator[Callable[[Profile, str], subprocess.Popen]]:
        """Provide a connect function and always shut the tunnel down afterwards.

        Yields:
            A function taking the profile and the target address. It returns
            the OpenVPN process once the tunnel is up.
        """
        with exit_event() as (event_name, signal_exit), kill_on_close_job() as assign:
            processes: list[subprocess.Popen] = []

            def connect(profile: Profile, target_ip: str) -> subprocess.Popen:
                vpn, ready = self.start_vpn(profile, target_ip, event_name)
                processes.append(vpn)  # registered first, so failures are cleaned
                assign(vpn)            # dies with us even after a crash
                self.wait_for_vpn(vpn, ready)
                return vpn

            try:
                yield connect
            finally:
                for vpn in processes:
                    if vpn.poll() is None:
                        log.info("Disconnecting the VPN.")
                        signal_exit()
                        try:
                            vpn.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            vpn.kill()

    def diagnose(self) -> bool:
        """Run every possible check and finish with a summary.

        Unlike :meth:`run`, a failure does not abort: only the checks depending
        on the failed one are skipped.

        Returns:
            True if no check failed.
        """
        results: list[tuple[str, str]] = []  # (title, outcome)
        values: dict[str, object] = {}       # data passed between the checks

        def check(title: str, action: Callable[[], None],
                  needs: tuple[str, ...] = ()) -> bool:
            """Run one check unless required data is missing; record the outcome."""
            if self.stop.is_set():
                raise Cancelled
            if any(key not in values for key in needs):
                log.info("--- %s: SKIPPED (an earlier check failed)", title)
                results.append((title, "SKIPPED"))
                return False
            log.info("--- %s", title)
            self.warned = False
            try:
                action()
            except Cancelled:
                raise
            except Exception as error:
                log.error("  FAILED: %s", error)
                results.append((title, "FAILED"))
                return False
            results.append((title, "WARNING" if self.warned else "OK"))
            return True

        def openvpn_version() -> None:
            output = subprocess.run(
                [self.settings.openvpn_exe, "--version"], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, errors="replace", timeout=10,
                creationflags=NO_WINDOW,
            ).stdout.strip()
            if not output:
                raise StepError("openvpn did not report a version")
            log.info("  %s", output.splitlines()[0])

        def profile() -> None:
            values["profile"] = self.prepare_profile()

        def resolve() -> None:
            values["target"] = socket.gethostbyname(self.settings.host)
            log.info("  %s -> %s", self.settings.host, values["target"])

        def route_before() -> None:
            values["source"] = source_address(values["target"],
                                              9)  # any port will do
            log.info("  local address without VPN: %s", values["source"])

        def route_after() -> None:
            source = source_address(values["target"], 9)
            log.info("  local address with VPN: %s", source)
            if source == values.get("source"):
                self.warn("the route did not change, the traffic may bypass the VPN")

        log.info("=== Diagnostics started ===")
        cancelled = False
        try:
            with single_instance():
                for leftover in SECRET_DIR.glob("tmp_*.txt"):
                    leftover.unlink(missing_ok=True)
                clients = self.enabled_clients()
                check("Programs", lambda: self.check_programs(clients))
                check("OpenVPN version", openvpn_version)
                check("OpenVPN profile", profile)
                check("Target name", resolve)
                check("VPN server reachability",
                      lambda: self.check_vpn_server(values["profile"]), ("profile",))
                # Informational, a failure here does not block anything.
                check("Route without VPN", route_before, ("target",))
                with self.vpn_tunnel() as connect:
                    def tunnel() -> None:
                        connect(values["profile"], values["target"])
                        values["vpn"] = True

                    check("VPN connection", tunnel, ("profile", "target"))
                    check("Route through VPN", route_after, ("vpn",))
                    check("Ping through VPN",
                          lambda: self.ping_target(values["target"]), ("vpn",))
                    if "ssh" in clients:
                        check("SSH port",
                              lambda: self.check_ssh_port(values["target"]), ("vpn",))
                        if self.settings.ssh_user:
                            def login() -> None:
                                self.check_login(values["target"])
                                values["login"] = True

                            check("SSH login", login, ("vpn",))
                    if "hoptodesk" in clients:
                        check("HopToDesk port",
                              lambda: self.check_hoptodesk_port(values["target"]),
                              ("vpn",))
                        if "ssh" in clients and self.settings.ssh_user:
                            check("Remote HopToDesk (via SSH)",
                                  lambda: self.check_remote_hoptodesk(
                                      values["target"]),
                                  ("login",))
                        else:
                            log.info("--- Remote HopToDesk: not checked, it needs "
                                     "SSH to be enabled with a user name")
        except Cancelled:
            cancelled = True
            log.info("Stopped by the user.")
        except Exception as error:
            log.error("Diagnostics aborted: %s", error)
            return False
        finally:
            self.cleanup()

        log.info("=== Summary%s ===", " (incomplete)" if cancelled else "")
        for title, outcome in results:
            log.info("  %-8s %s", outcome, title)
        return all(outcome != "FAILED" for _, outcome in results)

    def pipe_test(self) -> Optional[dict[str, bool]]:
        """Try the named pipes with real connections.

        The VPN is connected with pipes first, falling back step by step to
        temporary files. Through the working tunnel an SSH login is tested with
        the password coming from a pipe.

        Returns:
            New values for the ``pipe_*`` settings, or None if the test could not
            be completed (nothing should be changed then).
        """
        original = self.settings
        result = {"pipe_vpn_config": False, "pipe_vpn_auth": False,
                  "pipe_ssh": False}
        # (profile through pipe, credentials through pipe), best variant first.
        variants: list[tuple[bool, bool]] = []
        for config, auth in ((True, True), (False, True), (True, False),
                             (False, False)):
            variant = (config and original.copy_profile,
                       auth and bool(original.vpn_user))
            if variant not in variants:
                variants.append(variant)
        test_ssh = bool(original.use_ssh and original.ssh_user
                        and original.ssh_password)
        log.info("=== Pipe test started ===")
        try:
            with single_instance():
                self.check_programs(("ssh",) if test_ssh else ())
                profile = self.prepare_profile()
                target_ip = socket.gethostbyname(original.host)
                connected = False
                for config, auth in variants:
                    self.settings = replace(original, pipe_vpn_config=config,
                                            pipe_vpn_auth=auth)
                    log.info("--- VPN with profile via %s, credentials via %s",
                             "pipe" if config else "file",
                             "pipe" if auth else "file")
                    try:
                        with self.vpn_tunnel() as connect:
                            connect(profile, target_ip)
                            connected = True
                            result.update(pipe_vpn_config=config, pipe_vpn_auth=auth)
                            log.info("  VPN connected")
                            if test_ssh:
                                result["pipe_ssh"] = self.try_ssh_pipe(target_ip)
                            else:
                                log.info("  SSH pipe not tested (needs SSH, user "
                                         "and password)")
                    except (StepError, OSError) as error:
                        if connected:
                            raise
                        log.warning("  did not work: %s", error)
                        if self.auth_failed:  # retrying could lock the account
                            raise StepError("VPN login rejected, test stopped")
                    finally:
                        self.cleanup()
                    if connected:
                        break
                if not connected:
                    raise StepError("the VPN did not connect in any variant")
        except Cancelled:
            log.info("Stopped by the user, settings unchanged.")
            return None
        except Exception as error:
            log.error("Pipe test aborted: %s", error)
            return None
        finally:
            self.cleanup()
            self.settings = original
        log.info("=== Pipe test result ===")
        for name, works in result.items():
            log.info("  %-16s %s", name, "pipe" if works else "file (not enabled)")
        return result

    def try_ssh_pipe(self, target_ip: str) -> bool:
        """Log in with the password from a pipe; True if that really worked."""
        log.info("--- SSH login with the password via pipe")
        # Without a key, so that only the password can make the login succeed.
        self.settings = replace(self.settings, pipe_ssh=True, ssh_key="")
        count = len(self.pipes)
        try:
            self.check_login(target_ip)
        except (StepError, OSError, subprocess.TimeoutExpired) as error:
            log.warning("  did not work: %s", error)
            return False
        works = all(pipe.delivered for pipe in self.pipes[count:])
        log.info("  %s", "login OK" if works else "the pipe was not read")
        return works

    def run(self, test_only: bool, client: str = "ssh") -> bool:
        """Execute all steps, stopping at the first failure.

        Args:
            test_only: Check every enabled client and disconnect again.
            client: ``"ssh"`` or ``"hoptodesk"``; used only for real work.

        Returns:
            False if a step failed, True otherwise (including a user disconnect).
        """
        clients = self.enabled_clients() if test_only else (client,)
        test_ssh = test_only and "ssh" in clients
        login_test = test_ssh and bool(self.settings.ssh_user)
        test_hop = test_only and "hoptodesk" in clients
        self.step_number = 0
        # Programs, profile, name, VPN (+ VPN server and ping in a test).
        self.step_count = (6 + test_ssh + login_test + test_hop) if test_only else 5
        log.info("--- %s started ---",
                 "Test" if test_only else f"Connection ({client})")
        try:
            with single_instance():
                # Remove leftovers of a session that was killed.
                for leftover in SECRET_DIR.glob("tmp_*.txt"):
                    leftover.unlink(missing_ok=True)
                with self.step("Checking programs"):
                    if not set(clients) <= set(self.enabled_clients()):
                        raise StepError(f"'{client}' is switched off in the settings")
                    self.check_programs(clients)
                with self.step("Checking the OpenVPN profile"):
                    profile = self.prepare_profile()
                with self.step(f"Resolving {self.settings.host}"):
                    target_ip = socket.gethostbyname(self.settings.host)
                    log.info("  address: %s", target_ip)
                if test_only:
                    with self.step("Checking the VPN server"):
                        self.check_vpn_server(profile)
                with self.vpn_tunnel() as connect:
                    with self.step("Connecting the VPN"):
                        vpn = connect(profile, target_ip)
                    if test_only:
                        with self.step(f"Pinging {target_ip} through the VPN"):
                            self.ping_target(target_ip)
                        if test_ssh:
                            with self.step(f"Reaching SSH port {self.settings.port}"):
                                self.check_ssh_port(target_ip)
                            if login_test:
                                with self.step("Testing the SSH login"):
                                    self.check_login(target_ip)
                            else:
                                log.info("No SSH user set, login test skipped.")
                        if test_hop:
                            port = self.settings.hoptodesk_port
                            with self.step(f"Reaching HopToDesk port {port}"):
                                self.check_hoptodesk_port(target_ip)
                    elif client == "hoptodesk":
                        with self.step("Running HopToDesk"):
                            self.run_hoptodesk(target_ip, vpn)
                    else:
                        with self.step("Running PuTTY (close it to disconnect)"):
                            self.run_putty(target_ip, vpn)
        except Cancelled:
            log.info("Stopped by the user.")
        except StepError as error:
            log.error("Aborted: %s", error)
            return False
        except Exception:
            log.exception("Unexpected error")
            return False
        finally:
            self.cleanup()
        log.info("--- Finished successfully ---")
        return True
