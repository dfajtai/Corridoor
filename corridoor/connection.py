"""One VPN connection that carries several small client sessions.

``Connection.run`` brings the tunnel up and then serves a list of client
sessions until the user disconnects:

* several SSH sessions (one PuTTY window each, up to the ``max_ssh`` setting,
  4 by default and never more than ``MAX_SSH``),
* at most one HopToDesk session,
* at most one FileZilla session.

A session can be closed on its own without touching the VPN. Disconnecting
closes every session and then the tunnel. If the launcher crashes, the Windows
job object (see :mod:`winapi`) ends OpenVPN and all PuTTY windows.

Threading: ``run`` executes on a background thread and is the only code that
starts, watches and stops client processes. The GUI thread talks to it through
a command queue (:meth:`add_ssh`, :meth:`add_hoptodesk`, :meth:`close_session`)
and reads copies of the session list (:meth:`sessions`). No locks are held while
processes are started or stopped.
"""

import logging
import queue
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import store, winapi
from .errors import Cancelled, StepError
from .session import Secret, Session

MAX_SSH: int = 10          # hard upper limit of the ``max_ssh`` setting
POLL: float = 0.2          # seconds between two looks at the sessions
CLOSE_TIMEOUT: float = 5.0  # seconds a PuTTY window gets before it is killed
# PuTTY reads the password file while parsing its command line; delete it after.
PWFILE_LIFETIME: float = 10.0
# HopToDesk and FileZilla hand the request to a running instance and exit; such a
# session is over when no program of that name runs any more. Do not judge in the
# first seconds, and look only now and then (the process list is read each time).
START_GRACE: float = 5.0
WATCH_INTERVAL: float = 2.0

SSH: str = "ssh"
HOPTODESK: str = "hoptodesk"
FILEZILLA: str = "filezilla"
KIND_NAMES: dict[str, str] = {SSH: "SSH", HOPTODESK: "HopToDesk",
                              FILEZILLA: "FileZilla"}

log: logging.Logger = logging.getLogger("corridoor")


@dataclass
class ClientSession:
    """One running client: a PuTTY window or the HopToDesk session.

    Attributes:
        number: Unique number, shown in the list and used to close it.
        kind: ``"ssh"``, ``"hoptodesk"`` or ``"filezilla"``.
        process: The launched process. For HopToDesk and FileZilla it may
            already be gone while the session still runs (they hand the request
            to a running instance), so its end alone says nothing.
        started: ``time.monotonic()`` at the start.
        password: The secret given to PuTTY, if any.
        password_deleted: True once the password file has been removed.
        image: File name of the HopToDesk or FileZilla program (``filezilla.exe``),
            used to see whether it still runs.
        checked: ``time.monotonic()`` of the last look at the process list.
    """

    number: int
    kind: str
    process: subprocess.Popen
    started: float
    password: Optional[Secret] = None
    password_deleted: bool = False
    image: str = ""
    checked: float = 0.0

    @property
    def title(self) -> str:
        """Text for the session list."""
        if self.kind == HOPTODESK:
            return "HopToDesk"
        if self.kind == FILEZILLA:
            return "FileZilla"
        return f"SSH #{self.number}"

    def ended(self) -> bool:
        """Return True if the client is known to be gone.

        PuTTY: its process has ended. HopToDesk and FileZilla: the process that
        was started has ended **and** no program with that file name runs any
        more (they usually hand the request over to a running instance). If the
        process list cannot be read, the session is assumed to be alive.
        """
        if self.kind == SSH:
            return self.process.poll() is not None
        if not self.image or self.process.poll() is None:
            return False
        now = time.monotonic()
        if now - self.started < START_GRACE or now - self.checked < WATCH_INTERVAL:
            return False
        self.checked = now
        try:
            return not winapi.process_running(self.image)
        except OSError:
            return False


class Connection:
    """The tunnel plus its client sessions; see the module docstring."""

    def __init__(self, session: Session) -> None:
        """Wrap the session that provides the VPN steps and the settings."""
        self.session = session
        self.state = "starting"  # starting -> up -> down
        self._commands: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._lock = threading.Lock()
        self._clients: list[ClientSession] = []
        self._next_number = 1
        self.target_ip = ""

    # ----------------------------------------------------------------- #
    # Called from the GUI thread
    # ----------------------------------------------------------------- #

    def sessions(self) -> list[tuple[int, str]]:
        """Return ``(number, title)`` of the running sessions (a copy)."""
        with self._lock:
            return [(client.number, client.title) for client in self._clients]

    def can_add(self, kind: str) -> bool:
        """Return True if a session of this kind may be added now.

        The tunnel must be up, the client must be switched on in the settings,
        and the limits must not be reached: one HopToDesk, one FileZilla and
        :meth:`ssh_limit` SSH sessions.
        """
        return self.state == "up" and self._allowed(kind)

    def ssh_limit(self) -> int:
        """Return how many SSH sessions may run at once (``max_ssh``, 1 to MAX_SSH)."""
        try:
            wanted = int(self.session.settings.max_ssh)
        except ValueError:
            wanted = 4
        return max(1, min(wanted, MAX_SSH))

    def add_ssh(self) -> None:
        """Ask for a new PuTTY window (started by the connection thread)."""
        self._commands.put(("add", SSH))

    def add_hoptodesk(self) -> None:
        """Ask for the HopToDesk session (started by the connection thread)."""
        self._commands.put(("add", HOPTODESK))

    def add_filezilla(self) -> None:
        """Ask for the FileZilla session (started by the connection thread)."""
        self._commands.put(("add", FILEZILLA))

    def close_session(self, number: int) -> None:
        """Ask for one session to be closed; the tunnel stays up."""
        self._commands.put(("close", number))

    def disconnect(self) -> None:
        """Close all sessions and the tunnel."""
        self.session.stop.set()

    # ----------------------------------------------------------------- #
    # The connection thread
    # ----------------------------------------------------------------- #

    def run(self, first_client: str = SSH) -> bool:
        """Connect, serve the sessions, and clean up. Blocks until disconnect.

        Args:
            first_client: The session opened as soon as the tunnel is up
                (``"ssh"`` or ``"hoptodesk"``).

        Returns:
            True after a normal disconnect, False if a step failed or the VPN
            stopped unexpectedly.
        """
        session = self.session
        session.step_number, session.step_count = 0, 4
        log.info("--- Connection started ---")
        session.log_start("connection")
        log.debug("first session: %s", first_client)
        try:
            with winapi.single_instance():
                store.remove_temp_files()
                with session.step("Checking programs"):
                    if first_client not in session.enabled_clients():
                        raise StepError(f"'{first_client}' is switched off in "
                                        "the settings")
                    session.check_programs((first_client,))
                with session.step("Checking the OpenVPN profile"):
                    profile = store.load_profile(session.settings)
                with session.step(f"Resolving {session.settings.host}"):
                    self.target_ip = socket.gethostbyname(session.settings.host)
                    log.info("  address: %s", self.target_ip)
                with session.vpn_tunnel() as connect:
                    with session.step("Connecting the VPN"):
                        vpn = connect(profile, self.target_ip)
                    try:
                        self._add(first_client)  # listed before "up" is visible
                        self.state = "up"
                        self._serve(vpn)
                    finally:
                        self.state = "down"
                        self._close_all()
        except Cancelled:
            log.info("Stopped by the user.")
        except StepError as error:
            log.error("Aborted: %s", error)
            return False
        except Exception:
            log.exception("Unexpected error")
            return False
        finally:
            self.state = "down"
            session.cleanup()
        log.info("--- Disconnected ---")
        return True

    def _serve(self, vpn: subprocess.Popen) -> None:
        """Handle commands and watch the sessions until a disconnect.

        Raises:
            Cancelled: On the user's disconnect (the normal way to end).
            StepError: If OpenVPN stops while sessions may still be running.
        """
        while not self.session.stop.wait(POLL):
            self._handle_commands()
            self._reap()
            if vpn.poll() is not None:
                raise StepError("OpenVPN stopped unexpectedly")
        raise Cancelled

    def _handle_commands(self) -> None:
        """Run the commands the GUI queued since the last look."""
        while True:
            try:
                command, argument = self._commands.get_nowait()
            except queue.Empty:
                return
            log.debug("command: %s %s", command, argument)
            if command == "add":
                self._add(str(argument))
            else:
                self._close(int(argument))

    def _add(self, kind: str) -> None:
        """Start a client session if the limits and the programs allow it."""
        if not self._allowed(kind):
            log.debug("not allowed: sessions now %s", self.sessions())
            limit = self.ssh_limit() if kind == SSH else 1
            log.warning("Cannot add another %s session (limit: %d).",
                        KIND_NAMES.get(kind, kind), limit)
            return
        session = self.session
        secret: Optional[Secret] = None
        try:
            session.check_programs((kind,))
            if kind == SSH:
                process, secret = session.launch_putty(self.target_ip)
            elif kind == FILEZILLA:
                process = session.launch_filezilla(self.target_ip)
            else:
                process = session.launch_hoptodesk(self.target_ip)
        except (StepError, OSError) as error:
            log.error("Could not start the %s session: %s", kind, error)
            return
        settings = session.settings
        program = {HOPTODESK: settings.hoptodesk_exe,
                   FILEZILLA: settings.filezilla_exe}.get(kind, "")
        client = ClientSession(self._next_number, kind, process,
                               time.monotonic(), secret,
                               image=Path(program).name if program else "")
        self._next_number += 1
        with self._lock:
            self._clients.append(client)
        log.info("%s started.", client.title)
        log.debug("sessions now: %s", self.sessions())

    def _allowed(self, kind: str) -> bool:
        """Check the settings and the limits for a new session of this kind."""
        with self._lock:
            kinds = [client.kind for client in self._clients]
        settings = self.session.settings
        if kind == SSH:
            return settings.use_ssh and kinds.count(SSH) < self.ssh_limit()
        enabled = {HOPTODESK: settings.use_hoptodesk, FILEZILLA: settings.use_filezilla}
        return enabled.get(kind, False) and kind not in kinds

    def _reap(self) -> None:
        """Drop PuTTY windows that were closed and delete old password files."""
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            if client.ended():
                log.info("%s closed.", client.title)
                self._remove(client)
            elif (client.password and not client.password_deleted
                  and not self.session.settings.pipe_ssh
                  and time.monotonic() - client.started > PWFILE_LIFETIME):
                # A temporary file is removed early; a pipe only serves PuTTY.
                client.password.release()
                client.password_deleted = True
                log.debug("%s: password file deleted after %.0f s", client.title,
                          PWFILE_LIFETIME)

    def _close(self, number: int) -> None:
        """Close one session on request; the VPN is not touched."""
        with self._lock:
            matches = [c for c in self._clients if c.number == number]
        if not matches:
            return
        client = matches[0]
        log.debug("closing %s (process id %d)", client.title, client.process.pid)
        if client.kind == SSH and client.process.poll() is None:
            client.process.terminate()
            try:
                client.process.wait(timeout=CLOSE_TIMEOUT)
            except subprocess.TimeoutExpired:
                client.process.kill()
        # HopToDesk and FileZilla cannot be closed reliably (they may belong to
        # a running instance), so they are only removed from the list. Their
        # rows also leave it on their own, see ClientSession.ended.
        log.info("%s closed.", client.title)
        self._remove(client)

    def _close_all(self) -> None:
        """Close every session; runs before the tunnel is shut down."""
        with self._lock:
            numbers = [client.number for client in self._clients]
        for number in numbers:
            self._close(number)

    def _remove(self, client: ClientSession) -> None:
        """Take a session out of the list and release its secret."""
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        if client.password and not client.password_deleted:
            client.password.release()
            client.password_deleted = True
