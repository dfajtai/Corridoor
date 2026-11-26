"""Shared test fixture: a session environment with no real Windows parts.

The tests run on any operating system. Everything that touches Windows is
replaced, and the external programs (OpenVPN, PuTTY, plink, HopToDesk) are
tiny Python scripts that behave like the real ones for the purpose of a test.

What is replaced:

* ``winapi.dpapi``              a reversible XOR instead of encryption
* ``winapi.single_instance``    no mutex
* ``winapi.exit_event``         a file instead of a Windows event
* ``winapi.kill_on_close_job``  no job object
* ``net.ping``                  the answer is ``self.ping_reply``
* ``store.SECRET_DIR`` & co.    a temporary folder
* ``subprocess.Popen``          a script in the fake ``bin`` folder is started
                                with the Python interpreter
"""

import contextlib
import json
import logging
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from corridoor import store
from corridoor.session import Session
from corridoor.settings import Settings

# Printed by the fake OpenVPN once the "tunnel is up".
READY_LINE = "Initialization Sequence Completed"

OPENVPN_SCRIPT = """
import os, sys, time
args = sys.argv[1:]
if {calls!r}:
    with open({calls!r}, "a") as stream:
        stream.write(" ".join(args) + "\\n")
if args[:1] == ["--version"]:
    print("OpenVPN fake")
    sys.exit(0)
def pipe_value(option):
    return option in args and "pipe-" in args[args.index(option) + 1]
if {reject!r} == "any":
    refused = any("pipe-" in arg for arg in args)
else:
    refused = any(pipe_value(option) for option in {reject!r})
if refused:
    print("cannot read", flush=True)
    sys.exit(1)
mode = {mode!r}
if mode == "ready":
    print({ready!r}, flush=True)
    time.sleep({sleep!r})
elif mode == "serve":
    # Runs until the launcher signals the exit event (a file in the tests).
    print({ready!r}, flush=True)
    event = args[args.index("--service") + 1]
    while not os.path.exists(event):
        time.sleep(0.05)
elif mode == "auth_failed":
    print("AUTH_FAILED", flush=True)
    sys.exit(1)
elif mode == "auth_failed_hang":
    print("AUTH_FAILED", flush=True)
    time.sleep({sleep!r})
elif mode == "exit":
    sys.exit(1)
elif mode == "hang":
    time.sleep({sleep!r})
"""

PLINK_SCRIPT = """
import sys
sys.stdout.write({out!r})
sys.stderr.write({err!r})
sys.exit({code!r})
"""

PUTTY_SCRIPT = """
import time
time.sleep({sleep!r})
"""

HOPTODESK_SCRIPT = """
import sys
with open({marker!r}, "w") as stream:
    stream.write(" ".join(sys.argv[1:]))
"""

FILEZILLA_SCRIPT = """
import json, sys
with open({marker!r}, "w") as stream:
    json.dump(sys.argv[1:], stream)
"""

GOOD_PROBE = ("command=/usr/bin/hoptodesk", "service=active", "processes=2")


class FakePipe:
    """File-backed stand-in for :class:`corridoor.pipes.SecretPipe`.

    Instances register themselves in ``made`` so a test can check that every
    pipe was closed. ``unread`` simulates a child that never reads the pipe.
    """

    made: list["FakePipe"] = []
    unread: bool = False
    folder: Path = Path(".")

    def __init__(self, data: bytes) -> None:
        self.path = str(FakePipe.folder / f"pipe-{len(FakePipe.made)}")
        self.data = data
        self.delivered = False
        self.closed = False
        FakePipe.made.append(self)

    def start(self) -> None:
        Path(self.path).write_bytes(self.data)

    def allow(self, process_id: int) -> None:
        self.delivered = not FakePipe.unread

    def close(self) -> None:
        self.closed = True
        Path(self.path).unlink(missing_ok=True)


class SessionTestCase(unittest.TestCase):
    """Base class: builds the environment and offers helpers to the tests."""

    def setUp(self) -> None:
        """Create the temporary folders, patch the Windows parts, start a server."""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(
            tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.secret_dir = root / "secret"
        self.bin_dir = root / "bin"
        self.work_dir = root / "work"
        for folder in (self.secret_dir, self.bin_dir, self.work_dir):
            folder.mkdir()
        FakePipe.folder = self.work_dir
        FakePipe.made = []
        FakePipe.unread = False

        patch = lambda target, value: stack.enter_context(  # noqa: E731
            mock.patch(target, value))
        patch("corridoor.store.SECRET_DIR", self.secret_dir)
        patch("corridoor.store.SETTINGS_FILE", self.secret_dir / "settings.bin")
        patch("corridoor.store.PROFILE_FILE", self.secret_dir / "profile.bin")
        patch("corridoor.winapi.dpapi",
              lambda data, encrypt: bytes(byte ^ 0x5A for byte in data))
        patch("corridoor.winapi.single_instance", contextlib.nullcontext)
        # The "exit event" is a file: setting it creates the file, and the
        # fake OpenVPN in mode "serve" ends when it exists.
        self.exit_file = self.work_dir / "exit_event"
        patch("corridoor.winapi.exit_event", lambda: contextlib.nullcontext(
            (str(self.exit_file), lambda: self.exit_file.write_text("x"))))
        patch("corridoor.winapi.kill_on_close_job",
              lambda: contextlib.nullcontext(lambda process: None))
        self.ping_reply = True
        patch("corridoor.net.ping", lambda address: self.ping_reply)
        bin_dir = self.bin_dir

        class ScriptAwarePopen(subprocess.Popen):
            """Start scripts of the fake bin folder with this interpreter."""

            def __init__(self, args, *more, **options):
                if (isinstance(args, list) and args
                        and Path(str(args[0])).parent == bin_dir):
                    args = [sys.executable, *args]
                super().__init__(args, *more, **options)

        patch("subprocess.Popen", ScriptAwarePopen)

        logger = logging.getLogger("corridoor")
        self.records: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: self.records.append(record.getMessage())
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        self.addCleanup(lambda: (logger.removeHandler(handler),
                                 logger.setLevel(previous_level)))

        # A server that greets like SSH and hangs up; also serves as "HopToDesk".
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.addCleanup(self.server.close)
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self.greet, daemon=True).start()

        # Fake programs. OpenVPN and plink are rewritten by the tests.
        self.profile = self.work_dir / "a.ovpn"
        self.write_profile(f"client\nremote 127.0.0.1 {self.port} tcp\n")
        self.openvpn = self.fake_openvpn()
        self.fake_plink()
        self.putty = self.fake_putty()
        self.marker = self.work_dir / "hoptodesk_args"
        self.hoptodesk = self.fake(
            "hoptodesk.exe", HOPTODESK_SCRIPT.format(marker=str(self.marker)))
        self.openvpn_calls = self.work_dir / "openvpn_calls"
        self.filezilla_marker = self.work_dir / "filezilla_args"
        self.filezilla = self.fake(
            "filezilla.exe", FILEZILLA_SCRIPT.format(marker=str(self.filezilla_marker)))

        self.settings = Settings(
            profile_file=str(self.profile), host="127.0.0.1", port=str(self.port),
            vpn_user="u", vpn_password="p", ssh_user="x", ssh_password="y",
            openvpn_exe=self.openvpn, putty_exe=self.putty,
            hoptodesk_exe=self.hoptodesk, hoptodesk_port=str(self.port),
            filezilla_exe=self.filezilla)
        store.save_settings(self.settings)

    # ---------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------- #

    def greet(self) -> None:
        """Accept connections, say ``SSH-2.0-fake`` and hang up."""
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return  # the server socket was closed at the end of the test
            with contextlib.suppress(OSError):
                connection.send(b"SSH-2.0-fake\r\n")
            connection.close()

    def write_profile(self, text: str) -> None:
        """Replace the user's profile file."""
        self.profile.write_text(text)

    def fake(self, name: str, source: str) -> str:
        """Write a fake program and return its path."""
        path = self.bin_dir / name
        path.write_text(textwrap.dedent(source))
        return str(path)

    def fake_openvpn(self, mode: str = "ready", sleep: float = 1,
                     reject: object = (), count_calls: bool = False) -> str:
        """(Re)write the fake OpenVPN.

        Args:
            mode: ``ready`` (tunnel up, then ``sleep`` seconds), ``serve``
                (tunnel up until the exit event), ``auth_failed``, ``auth_failed_hang``
                (rejects but keeps running), ``exit`` (fails at once) or
                ``hang`` (silent).
            sleep: How long it keeps running after its output.
            reject: ``"any"``, or option names whose value must not be a pipe;
                the program then fails like OpenVPN does for an unreadable file.
            count_calls: Log one line per start into ``openvpn_calls``.
        """
        calls = str(self.work_dir / "openvpn_calls") if count_calls else ""
        return self.fake("openvpn", OPENVPN_SCRIPT.format(
            calls=calls, reject=reject, mode=mode, ready=READY_LINE, sleep=sleep))

    def fake_putty(self, sleep: float = 0) -> str:
        """(Re)write the fake PuTTY; it stays open for ``sleep`` seconds."""
        return self.fake("putty.exe", PUTTY_SCRIPT.format(sleep=sleep))

    def fake_plink(self, out: str = "", err: str = "", code: int = 0) -> str:
        """(Re)write the fake plink with the given output and exit code."""
        return self.fake("plink.exe",
                         PLINK_SCRIPT.format(out=out, err=err, code=code))

    def fake_probe(self, *lines: str, code: int = 0) -> None:
        """Make plink answer like the remote HopToDesk probe."""
        self.fake_plink(out="".join(f"{line}\n" for line in lines), code=code)

    def new_session(self, settings: Settings = None,
                    stop: threading.Event = None) -> Session:
        """Create a session for ``settings`` (default: the fixture's)."""
        return Session(settings or self.settings, stop or threading.Event())

    def stop_after(self, seconds: float) -> threading.Event:
        """Return a stop event that is set after the given time."""
        stop = threading.Event()
        timer = threading.Timer(seconds, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        return stop

    def launches(self) -> int:
        """Return and reset the number of fake OpenVPN starts."""
        if not self.openvpn_calls.exists():
            return 0
        count = len(self.openvpn_calls.read_text().splitlines())
        self.openvpn_calls.unlink()
        return count

    def log_text(self) -> str:
        """Return everything logged so far."""
        return "\n".join(self.records)

    def assert_no_secret_files(self) -> None:
        """Fail if a temporary secret file was left behind."""
        self.assertEqual(list(self.secret_dir.glob("tmp_*")), [],
                         "temporary secrets left behind")

    def wait_for(self, path: Path, seconds: float = 5.0) -> str:
        """Wait until a file exists and return its text."""
        deadline = time.monotonic() + seconds
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        return path.read_text().strip()

    def use_fake_pipes(self) -> None:
        """Replace the real named pipes by :class:`FakePipe` for this test."""
        patcher = mock.patch("corridoor.session.SecretPipe", FakePipe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stored_settings_json(self, values: dict) -> None:
        """Write raw settings into the store, as another version would have."""
        store.SETTINGS_FILE.write_bytes(
            bytes(b ^ 0x5A for b in json.dumps(values).encode()))
