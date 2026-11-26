"""One connection attempt: the steps, the child processes and their cleanup.

A :class:`Session` is created per job (test, real connection, diagnostics or
pipe test) and runs on a background thread. It talks to the user only through
the ``corridoor`` logger; the GUI shows whatever is logged.

The class is organised in four groups of methods:

1. step bookkeeping (``step``, ``warn``),
2. secrets and cleanup (``secret``, ``cleanup``),
3. single checks and child processes (``check_*``, ``start_vpn``, ``run_*``),
4. the complete jobs (``run_test``; ``diagnose`` and ``pipe_test`` live in
   their own modules and are only delegated to here). A long-lived connection
   with several client sessions is :class:`corridoor.connection.Connection`.
"""

import logging
import os
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Optional

from . import hoptodesk, net, store, winapi
from .errors import Cancelled, StepError
from .pipes import SecretPipe
from .profile import Profile, iter_directives, vpn_remotes
from .settings import Settings

# OpenVPN prints this line once the tunnel is up.
READY_MARKER: str = "Initialization Sequence Completed"
VPN_TIMEOUT: float = 60.0   # seconds to wait for the tunnel
SSH_TIMEOUT: float = 30.0   # seconds for one non-interactive plink call
TCP_TIMEOUT: float = 5.0    # seconds for a TCP reachability probe

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


def ignore_process_id(process_id: int) -> None:
    """Announcer for secrets that need no announcement (temporary files)."""


class Secret(NamedTuple):
    """Secret content that a child process reads from a path.

    ``path`` is given to the program. ``announce`` must be called with the
    process id right after the program has been started: a named pipe only
    serves that process. For a temporary file it does nothing. ``release``
    deletes the file or closes the pipe of this one secret, before the
    session-wide :meth:`Session.cleanup`.
    """

    path: str
    announce: Callable[[int], None]
    release: Callable[[], None]


class Session:
    """One connection attempt, either as a test or as real work.

    Attributes:
        settings: The settings used by this session (the pipe test swaps them
            temporarily and restores them afterwards).
        stop: Set from the GUI thread to request a disconnect or a cancel.
        pipes: Named pipes created so far, closed by :meth:`cleanup`.
        warned: True if the current step reported a warning.
        auth_failed: True once the VPN server rejected the login.
    """

    def __init__(self, settings: Settings, stop: threading.Event) -> None:
        """Store the settings and the event that requests a disconnect."""
        self.settings = settings
        self.stop = stop
        self.temp_files: list[Path] = []
        self.pipes: list[SecretPipe] = []
        self.auth_failed = False
        self.log_reader: Optional[threading.Thread] = None  # OpenVPN output
        self.step_number = 0
        self.step_count = 0
        self.warned = False
        # Adds a process to the kill-on-close job; set while a tunnel exists.
        self.job_assign: Optional[Callable[[subprocess.Popen], None]] = None

    # ----------------------------------------------------------------- #
    # 1. Step bookkeeping
    # ----------------------------------------------------------------- #

    @contextmanager
    def step(self, title: str) -> Iterator[None]:
        """Log the start and the outcome of one numbered step.

        Raises:
            Cancelled: If a stop was requested before the step began.
            StepError: Wrapping any other exception of the step body.
        """
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

    # ----------------------------------------------------------------- #
    # 2. Secrets and cleanup
    # ----------------------------------------------------------------- #

    def temp_file(self, content: str) -> Path:
        """Write secret content into the admin-only folder; removed at the end."""
        handle, name = tempfile.mkstemp(prefix="tmp_", suffix=".txt",
                                        dir=store.SECRET_DIR)
        path = Path(name)
        self.temp_files.append(path)
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        return path

    def secret(self, content: str, use_pipe: bool) -> Secret:
        """Offer secret content to a child process.

        Args:
            content: Text the child process has to read.
            use_pipe: Use a named pipe instead of a temporary file.

        Returns:
            The path to give to the program and the announcer to call with the
            process id right after the program has been started.
        """
        if not use_pipe:
            path = self.temp_file(content)
            return Secret(str(path), ignore_process_id,
                          lambda: path.unlink(missing_ok=True))
        pipe = SecretPipe(content.encode("utf-8"))
        pipe.start()
        self.pipes.append(pipe)

        def release() -> None:
            pipe.close()
            if pipe in self.pipes:
                self.pipes.remove(pipe)

        return Secret(pipe.path, pipe.allow, release)

    def cleanup(self) -> None:
        """Close all pipes and delete all temporary files of this session."""
        while self.pipes:
            self.pipes.pop().close()
        for path in self.temp_files:
            path.unlink(missing_ok=True)

    # ----------------------------------------------------------------- #
    # 3. Single checks and child processes
    # ----------------------------------------------------------------- #

    def enabled_clients(self) -> tuple[str, ...]:
        """Return the clients switched on in the settings."""
        choices = (("ssh", self.settings.use_ssh),
                   ("hoptodesk", self.settings.use_hoptodesk))
        return tuple(name for name, enabled in choices if enabled)

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
                reply = net.ping(address)
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
        if net.ping(target_ip):
            log.info("  ping %s: reply", target_ip)
        else:
            self.warn(f"{target_ip} does not answer to ping (host down, routing "
                      "problem, or ICMP blocked); the port test decides")

    # --- OpenVPN ---

    def start_vpn(self, profile: Profile, target_ip: str,
                  event_name: str) -> tuple[subprocess.Popen, threading.Event]:
        """Launch OpenVPN so that only the target host uses the tunnel.

        Args:
            profile: The checked profile.
            target_ip: The only address routed through the tunnel.
            event_name: Name of the event that asks OpenVPN to exit cleanly.

        Returns:
            The process and an event that is set when the tunnel is up.
        """
        announce: list[Callable[[int], None]] = []
        if profile.path is None:  # decrypted copy from the store
            config = self.secret(profile.text, self.settings.pipe_vpn_config)
            announce.append(config.announce)
            config_path, folder = config.path, store.SECRET_DIR
        else:
            config_path, folder = str(profile.path), profile.path.parent
        command = [
            self.settings.openvpn_exe,
            "--config", config_path,
            "--route-nopull",                         # ignore pushed routes/DNS
            "--route", target_ip, "255.255.255.255",  # route only the target
            "--service", event_name, "0",             # allows a clean shutdown
            "--script-security", "1",                 # no user scripts as admin
        ]
        if self.settings.vpn_user:
            auth = self.secret(
                f"{self.settings.vpn_user}\n{self.settings.vpn_password}\n",
                self.settings.pipe_vpn_auth)
            announce.append(auth.announce)
            command += ["--auth-user-pass", auth.path]
        process = subprocess.Popen(
            command, cwd=folder, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            errors="replace", creationflags=winapi.NO_WINDOW,
        )
        for announcer in announce:
            announcer(process.pid)
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
        """Block until the tunnel is up, OpenVPN exits, or the time runs out.

        Raises:
            Cancelled: If the user stops while waiting.
            StepError: On a rejected login (reported at once, because retries
                could lock the account), an early exit or a timeout.
        """
        deadline = time.monotonic() + VPN_TIMEOUT
        while not ready.wait(0.2):
            if self.stop.is_set():
                raise Cancelled
            exited = vpn.poll() is not None
            if exited and self.log_reader is not None:
                self.log_reader.join(timeout=2)  # the reason may still be unread
            if self.auth_failed:
                raise StepError("the VPN server rejected the login (AUTH_FAILED)")
            if exited:
                raise StepError(f"OpenVPN exited with code {vpn.returncode}")
            if time.monotonic() > deadline:
                raise StepError(f"no connection within {VPN_TIMEOUT:.0f} s")

    @contextmanager
    def vpn_tunnel(self) -> Iterator[Callable[[Profile, str], subprocess.Popen]]:
        """Provide a connect function and always shut the tunnel down afterwards.

        OpenVPN is started inside a Windows job object (it dies with this
        program) and is asked to exit through an event when the block ends.

        Yields:
            A function taking the profile and the target address. It returns
            the OpenVPN process once the tunnel is up.
        """
        with winapi.exit_event() as (event_name, signal_exit), \
                winapi.kill_on_close_job() as assign:
            processes: list[subprocess.Popen] = []
            self.job_assign = assign  # clients started later join the job too

            def connect(profile: Profile, target_ip: str) -> subprocess.Popen:
                vpn, ready = self.start_vpn(profile, target_ip, event_name)
                processes.append(vpn)  # registered first, so failures are cleaned
                assign(vpn)            # dies with us even after a crash
                self.wait_for_vpn(vpn, ready)
                return vpn

            try:
                yield connect
            finally:
                self.job_assign = None
                for vpn in processes:
                    if vpn.poll() is None:
                        log.info("Disconnecting the VPN.")
                        signal_exit()
                        try:
                            vpn.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            vpn.kill()

    # --- SSH ---

    def check_ssh_port(self, target_ip: str) -> None:
        """Open a TCP connection and expect an SSH greeting."""
        address = (target_ip, int(self.settings.port))
        with socket.create_connection(address, timeout=10) as connection:
            banner = connection.recv(256)
        if b"SSH-" not in banner:
            raise StepError("the port is open but does not answer like an SSH server")

    def key_option(self) -> list[str]:
        """Return the PuTTY / plink arguments selecting the private key, if any."""
        return ["-i", self.settings.ssh_key] if self.settings.ssh_key else []

    def ssh_secret(self) -> Optional[Secret]:
        """Offer the SSH password to PuTTY / plink, or None if there is none."""
        if not self.settings.ssh_password:
            return None
        return self.secret(self.settings.ssh_password + "\n", self.settings.pipe_ssh)

    def ssh_command(self, target_ip: str, remote_command: str) -> str:
        """Run a command on the target with plink, without any user interaction.

        Returns:
            The standard output of the remote command.

        Raises:
            StepError: If the login or the command fails.
            subprocess.TimeoutExpired: If it takes longer than ``SSH_TIMEOUT``.
        """
        plink = Path(self.settings.putty_exe).with_name("plink.exe")
        command = [str(plink), "-ssh", "-batch", "-P", self.settings.port,
                   *self.key_option()]
        password = self.ssh_secret()
        if password:
            command += ["-pwfile", password.path]
        command += [f"{self.settings.ssh_user}@{target_ip}", remote_command]
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, errors="replace",
            creationflags=winapi.NO_WINDOW,
        )
        if password:
            password.announce(process.pid)
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

    def check_login(self, target_ip: str) -> None:
        """Log in non-interactively with plink and run ``exit``."""
        self.ssh_command(target_ip, "exit")

    def launch_putty(self, target_ip: str
                     ) -> tuple[subprocess.Popen, Optional[Secret]]:
        """Start one PuTTY window; the caller watches and closes it.

        PuTTY joins the kill-on-close job when a tunnel exists, so it cannot
        outlive a crashed launcher.

        Returns:
            The PuTTY process and the password secret (None without password).
            The caller releases the secret when it is no longer needed.

        Raises:
            OSError: If PuTTY cannot be started.
        """
        login = target_ip
        if self.settings.ssh_user:
            login = f"{self.settings.ssh_user}@{target_ip}"
        command = [self.settings.putty_exe, "-ssh", login, "-P", self.settings.port,
                   *self.key_option()]
        password = self.ssh_secret()
        if password:
            command += ["-pwfile", password.path]
        try:
            putty = subprocess.Popen(command)
            if password:
                password.announce(putty.pid)
            if self.job_assign is not None:
                self.job_assign(putty)
        except BaseException:
            if password:
                password.release()
            raise
        return putty, password

    # --- HopToDesk ---

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
        values = hoptodesk.parse_probe(
            self.ssh_command(target_ip, hoptodesk.PROBE_COMMAND))
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

        if not installed:
            self.warn("no package information, the version cannot be checked")
        elif (hoptodesk.version_numbers(version)
              < hoptodesk.version_numbers(hoptodesk.MIN_VERSION)):
            self.warn(f"version {version} is older than {hoptodesk.MIN_VERSION}, "
                      "update HopToDesk on the remote machine")

    def launch_hoptodesk(self, target_ip: str) -> subprocess.Popen:
        """Start a direct IP session with HopToDesk.

        The launched process may hand the request over to an already running
        HopToDesk instance and exit at once, so its end says nothing about the
        session. It is therefore not put into the job and not watched.

        Raises:
            OSError: If HopToDesk cannot be started.
        """
        peer = target_ip
        if self.settings.hoptodesk_port != hoptodesk.DEFAULT_PORT:
            peer = f"{target_ip}:{self.settings.hoptodesk_port}"
        # No password argument: "--password" would change this computer's setting.
        process = subprocess.Popen([self.settings.hoptodesk_exe, "--connect", peer])
        log.info("  Enter the password in HopToDesk.")
        return process

    # ----------------------------------------------------------------- #
    # 4. Complete jobs
    # ----------------------------------------------------------------- #

    def diagnose(self) -> bool:
        """Run every check and finish with a summary (see :mod:`diagnose`)."""
        from . import diagnose
        return diagnose.run(self)

    def pipe_test(self) -> Optional[dict[str, bool]]:
        """Try the named pipes with real connections (see :mod:`pipetest`)."""
        from . import pipetest
        return pipetest.run(self)

    def run_test(self) -> bool:
        """Test every enabled client step by step, then disconnect again.

        Stops at the first failing step. Real sessions are handled by
        :class:`corridoor.connection.Connection`.

        Returns:
            False if a step failed, True otherwise (including a user cancel).
        """
        clients = self.enabled_clients()
        test_ssh = "ssh" in clients
        login_test = test_ssh and bool(self.settings.ssh_user)
        test_hop = "hoptodesk" in clients
        self.step_number = 0
        # Programs, profile, name, VPN server, VPN, ping + the client checks.
        self.step_count = 6 + test_ssh + login_test + test_hop
        log.info("--- Test started ---")
        try:
            with winapi.single_instance():
                store.remove_temp_files()
                with self.step("Checking programs"):
                    self.check_programs(clients)
                with self.step("Checking the OpenVPN profile"):
                    profile = store.load_profile(self.settings)
                with self.step(f"Resolving {self.settings.host}"):
                    target_ip = socket.gethostbyname(self.settings.host)
                    log.info("  address: %s", target_ip)
                with self.step("Checking the VPN server"):
                    self.check_vpn_server(profile)
                with self.vpn_tunnel() as connect:
                    with self.step("Connecting the VPN"):
                        connect(profile, target_ip)
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
