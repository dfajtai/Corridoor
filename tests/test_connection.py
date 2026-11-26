"""One VPN connection with several client sessions."""

import subprocess
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from corridoor import store
from corridoor.connection import Connection
from tests.support import FakePipe, SessionTestCase


class ConnectionTestCase(SessionTestCase):
    """Runs a :class:`Connection` on a thread, as the GUI does."""

    def setUp(self) -> None:
        super().setUp()
        self.fake_openvpn(mode="serve")  # stays up until the exit event
        self.fake_putty(sleep=30)        # PuTTY windows stay open

    def start(self, first: str = "ssh", settings=None) -> Connection:
        """Start the connection thread and wait until the tunnel is up."""
        self.connection = Connection(self.new_session(settings))
        self.result: list[bool] = []
        self.thread = threading.Thread(
            target=lambda: self.result.append(self.connection.run(first)))
        self.thread.start()
        self.addCleanup(self.finish)
        self.wait_until(lambda: self.connection.state != "starting")
        return self.connection

    def finish(self) -> None:
        """Disconnect (if needed) and wait for the thread."""
        self.connection.disconnect()
        self.thread.join(timeout=20)

    def wait_until(self, condition, seconds: float = 8.0) -> None:
        """Wait for a condition; fail the test if it never becomes true."""
        deadline = time.monotonic() + seconds
        while not condition():
            if time.monotonic() > deadline:
                self.fail(f"condition not met; log:\n{self.log_text()}")
            time.sleep(0.05)

    def titles(self) -> list[str]:
        """Return the titles of the listed sessions."""
        return [title for _, title in self.connection.sessions()]

    def numbers(self) -> list[int]:
        """Return the numbers of the listed sessions."""
        return [number for number, _ in self.connection.sessions()]

    def disconnect(self) -> None:
        """Disconnect and wait until the connection thread has ended."""
        self.connection.disconnect()
        self.thread.join(timeout=20)
        self.assertFalse(self.thread.is_alive())


class SessionListTest(ConnectionTestCase):
    """Adding and closing sessions."""

    def test_first_session_is_added_when_the_tunnel_is_up(self) -> None:
        connection = self.start()
        self.assertEqual(connection.state, "up")
        self.assertEqual(self.titles(), ["SSH #1"])

    def test_several_ssh_sessions_can_be_added(self) -> None:
        connection = self.start()
        connection.add_ssh()
        connection.add_ssh()
        self.wait_until(lambda: len(self.numbers()) == 3)
        self.assertEqual(self.titles(), ["SSH #1", "SSH #2", "SSH #3"])

    def test_closing_one_session_keeps_the_others_and_the_vpn(self) -> None:
        connection = self.start()
        connection.add_ssh()
        connection.add_ssh()
        self.wait_until(lambda: len(self.numbers()) == 3)
        connection.close_session(2)
        self.wait_until(lambda: self.numbers() == [1, 3])
        self.assertEqual(connection.state, "up")
        self.assertNotIn("Disconnecting the VPN", self.log_text())

    def test_closing_the_last_session_keeps_the_vpn(self) -> None:
        connection = self.start()
        connection.close_session(1)
        self.wait_until(lambda: self.numbers() == [])
        time.sleep(0.5)
        self.assertEqual(connection.state, "up")
        self.assertTrue(connection.can_add("ssh"))

    def test_session_closed_by_the_user_leaves_the_list(self) -> None:
        self.fake_putty(sleep=1)
        self.start()
        self.wait_until(lambda: self.numbers() == [])
        self.assertIn("SSH #1 closed.", self.log_text())

    def test_unknown_number_is_ignored(self) -> None:
        connection = self.start()
        connection.close_session(99)
        time.sleep(0.5)
        self.assertEqual(self.numbers(), [1])

    def test_numbers_are_not_reused(self) -> None:
        connection = self.start()
        connection.close_session(1)
        self.wait_until(lambda: self.numbers() == [])
        connection.add_ssh()
        self.wait_until(lambda: self.numbers() == [2])

    def test_ssh_limit(self) -> None:
        with mock.patch("corridoor.connection.MAX_SSH", 2):
            connection = self.start()
            connection.add_ssh()
            self.wait_until(lambda: len(self.numbers()) == 2)
            self.assertFalse(connection.can_add("ssh"))
            connection.add_ssh()
            time.sleep(0.5)
            self.assertEqual(len(self.numbers()), 2)
            self.assertIn("Cannot add another SSH session", self.log_text())

    def test_default_limit_is_four_ssh_windows(self) -> None:
        connection = self.start()
        self.assertEqual(connection.ssh_limit(), 4)
        for _ in range(5):
            connection.add_ssh()
        self.wait_until(lambda: len(self.numbers()) == 4)
        time.sleep(0.5)
        self.assertEqual(len(self.numbers()), 4)
        self.assertFalse(connection.can_add("ssh"))
        self.assertIn("Cannot add another SSH session (limit: 4)", self.log_text())

    def test_the_limit_is_a_setting(self) -> None:
        connection = self.start(settings=replace(self.settings, max_ssh="2"))
        self.assertEqual(connection.ssh_limit(), 2)
        connection.add_ssh()
        self.wait_until(lambda: len(self.numbers()) == 2)
        self.assertFalse(connection.can_add("ssh"))

    def test_unusable_limits_are_corrected(self) -> None:
        connection = Connection(self.new_session())
        for text, expected in (("x", 4), ("", 4), ("0", 1), ("-3", 1),
                               ("99", 10), (" 7 ", 7)):
            connection.session.settings = replace(self.settings, max_ssh=text)
            self.assertEqual(connection.ssh_limit(), expected, text)

    def test_hoptodesk_and_filezilla_stay_at_one(self) -> None:
        settings = replace(self.settings, use_hoptodesk=True, use_filezilla=True)
        connection = self.start(settings=settings)
        connection.add_hoptodesk()
        connection.add_filezilla()
        self.wait_until(lambda: len(self.numbers()) == 3)
        self.assertFalse(connection.can_add("hoptodesk"))
        self.assertFalse(connection.can_add("filezilla"))
        self.wait_for(self.marker)  # let the fake programs finish before cleanup
        self.wait_for(self.filezilla_marker)

    def test_ssh_can_be_switched_off(self) -> None:
        connection = self.start(settings=replace(self.settings, use_hoptodesk=True))
        connection.session.settings = replace(connection.session.settings,
                                              use_ssh=False)
        self.assertFalse(connection.can_add("ssh"))


class HopToDeskSessionTest(ConnectionTestCase):
    """At most one HopToDesk session, closed only by removing it."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = replace(self.settings, use_hoptodesk=True)
        store.save_settings(self.settings)

    def test_session_gets_address_and_port(self) -> None:
        self.start(first="hoptodesk")
        self.assertEqual(self.titles(), ["HopToDesk"])
        self.assertEqual(self.wait_for(self.marker),
                         f"--connect 127.0.0.1:{self.port}")

    def test_default_port_is_not_appended(self) -> None:
        self.start(first="hoptodesk",
                   settings=replace(self.settings, hoptodesk_port="21118"))
        self.assertEqual(self.wait_for(self.marker), "--connect 127.0.0.1")

    def test_only_one_hoptodesk_session(self) -> None:
        connection = self.start(first="hoptodesk")
        self.assertFalse(connection.can_add("hoptodesk"))
        connection.add_hoptodesk()
        time.sleep(0.5)
        self.assertEqual(self.titles(), ["HopToDesk"])
        self.assertIn("Cannot add another HopToDesk session", self.log_text())

    def test_ssh_and_hoptodesk_side_by_side(self) -> None:
        connection = self.start()
        connection.add_hoptodesk()
        self.wait_until(lambda: self.titles() == ["SSH #1", "HopToDesk"])

    def test_hoptodesk_stays_listed_after_its_process_ended(self) -> None:
        self.start(first="hoptodesk")  # the fake exits at once
        time.sleep(0.7)
        self.assertEqual(self.titles(), ["HopToDesk"])

    def test_hoptodesk_can_be_removed_and_added_again(self) -> None:
        connection = self.start(first="hoptodesk")
        connection.close_session(1)
        self.wait_until(lambda: self.numbers() == [])
        connection.add_hoptodesk()
        self.wait_until(lambda: self.titles() == ["HopToDesk"])


class FileZillaTest(ConnectionTestCase):
    """FileZilla: one session, opened on an sftp address, without a password."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = replace(self.settings, use_filezilla=True,
                                ssh_password="SSHsecret456")
        store.save_settings(self.settings)

    def arguments(self) -> list:
        import json
        return json.loads(self.wait_for(self.filezilla_marker))

    def test_opens_an_sftp_address_and_asks_for_the_password_itself(self) -> None:
        connection = self.start()
        connection.add_filezilla()
        self.wait_until(lambda: self.titles() == ["SSH #1", "FileZilla"])
        self.assertEqual(self.arguments(),
                         ["-l", "interactive", f"sftp://x@127.0.0.1:{self.port}"])

    def test_the_password_never_reaches_the_command_line(self) -> None:
        connection = self.start()
        connection.add_filezilla()
        self.wait_until(lambda: len(self.numbers()) == 2)
        self.assertNotIn("SSHsecret456", " ".join(self.arguments()))

    def test_user_name_is_quoted_in_the_address(self) -> None:
        settings = replace(self.settings, ssh_user="a b@c")
        connection = self.start(settings=settings)
        connection.add_filezilla()
        self.assertIn("sftp://a%20b%40c@127.0.0.1", self.arguments()[-1])

    def test_without_a_user_name_filezilla_asks_for_it(self) -> None:
        settings = replace(self.settings, ssh_user="")
        connection = self.start(settings=settings)
        connection.add_filezilla()
        self.assertEqual(self.arguments()[-1], f"sftp://127.0.0.1:{self.port}")

    def test_only_one_file_transfer_session(self) -> None:
        connection = self.start()
        connection.add_filezilla()
        self.wait_until(lambda: len(self.numbers()) == 2)
        self.assertFalse(connection.can_add("filezilla"))
        connection.add_filezilla()
        time.sleep(0.5)
        self.assertEqual(len(self.numbers()), 2)
        self.assertIn("Cannot add another FileZilla session", self.log_text())

    def test_it_is_only_removed_from_the_list(self) -> None:
        connection = self.start()
        connection.add_filezilla()
        self.wait_until(lambda: len(self.numbers()) == 2)
        connection.close_session(2)
        self.wait_until(lambda: self.numbers() == [1])
        self.assertTrue(connection.can_add("filezilla"))

    def test_can_be_switched_off(self) -> None:
        connection = self.start(
            settings=replace(self.settings, use_filezilla=False))
        self.assertFalse(connection.can_add("filezilla"))

    def test_missing_program_does_not_stop_the_vpn(self) -> None:
        connection = self.start()
        connection.session.settings = replace(
            connection.session.settings, filezilla_exe="/nonexistent")
        connection.add_filezilla()
        time.sleep(0.5)
        self.assertEqual(self.titles(), ["SSH #1"])
        self.assertEqual(connection.state, "up")


class WatchedClientsTest(ConnectionTestCase):
    """HopToDesk and FileZilla leave the list when the program is gone."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = replace(self.settings, use_hoptodesk=True, use_filezilla=True)
        store.save_settings(self.settings)
        for name, value in (("START_GRACE", 0.0), ("WATCH_INTERVAL", 0.0)):
            patcher = mock.patch(f"corridoor.connection.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def open_both(self) -> Connection:
        connection = self.start(settings=self.settings)
        connection.add_hoptodesk()
        connection.add_filezilla()
        self.wait_until(lambda: len(self.numbers()) == 3)
        self.wait_for(self.marker)
        self.wait_for(self.filezilla_marker)
        return connection

    def test_the_rows_stay_while_the_programs_run(self) -> None:
        self.open_both()
        time.sleep(0.8)
        self.assertEqual(self.titles(), ["SSH #1", "HopToDesk", "FileZilla"])

    def test_the_rows_leave_when_the_programs_are_gone(self) -> None:
        connection = self.open_both()
        self.running_programs = False
        self.wait_until(lambda: self.titles() == ["SSH #1"])
        self.assertIn("FileZilla closed.", self.log_text())
        self.assertIn("HopToDesk closed.", self.log_text())
        self.assertTrue(connection.can_add("filezilla"))  # can be opened again

    def test_the_program_is_found_by_its_file_name(self) -> None:
        asked: list[str] = []
        self.running_programs = True
        with mock.patch("corridoor.winapi.process_running",
                        lambda image: asked.append(image) or True):
            self.open_both()
            time.sleep(0.5)
        self.assertEqual(set(asked), {"hoptodesk.exe", "filezilla.exe"})

    def test_a_young_session_is_not_judged(self) -> None:
        with mock.patch("corridoor.connection.START_GRACE", 60.0):
            self.running_programs = False
            self.open_both()
            time.sleep(0.8)
            self.assertEqual(len(self.numbers()), 3)

    def test_an_unreadable_process_list_keeps_the_row(self) -> None:
        with mock.patch("corridoor.winapi.process_running", side_effect=OSError("no")):
            self.open_both()
            time.sleep(0.8)
            self.assertEqual(len(self.numbers()), 3)

    def test_a_running_launcher_process_counts_as_open(self) -> None:
        """If the started process itself still runs, the list is not even read."""
        self.fake("filezilla.exe", "import time; time.sleep(30)")
        connection = self.start(settings=self.settings)
        self.running_programs = False
        connection.add_filezilla()
        self.wait_until(lambda: len(self.numbers()) == 2)
        time.sleep(0.8)
        self.assertEqual(len(self.numbers()), 2)


class LimitedClientsTest(ConnectionTestCase):
    """PuTTY, HopToDesk and FileZilla start without admin rights."""

    SID = "S-1-5-21-1-2-3-1001"

    def setUp(self) -> None:
        super().setUp()
        self.limited_commands: list[list[str]] = []
        self.grants: list[tuple] = []
        self.use_fake_pipes()
        self.settings = replace(self.settings, use_hoptodesk=True, use_filezilla=True,
                                ssh_password="SSHsecret456")
        store.save_settings(self.settings)

        def start_limited(command):
            # Behaves like the real one for the tests: the program just runs.
            self.limited_commands.append(list(command))
            return subprocess.Popen(command)

        for target, value in (
                ("corridoor.winapi.is_admin", lambda: True),
                ("corridoor.winapi.start_limited", start_limited),
                ("corridoor.winapi.user_sid", lambda: self.SID),
                ("corridoor.winapi.grant_read",
                 lambda path, sid: self.grants.append((path, sid)))):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def programs(self) -> list[str]:
        """Names of the programs that were started without admin rights."""
        return [Path(command[0]).name for command in self.limited_commands]

    def test_every_client_is_started_limited(self) -> None:
        connection = self.start()
        connection.add_hoptodesk()
        connection.add_filezilla()
        self.wait_until(lambda: len(self.numbers()) == 3)
        self.assertEqual(self.programs(), ["putty.exe", "hoptodesk.exe", "filezilla.exe"])

    def test_the_password_file_is_readable_by_the_user(self) -> None:
        self.start()
        self.wait_until(lambda: len(self.grants) == 1)
        path, sid = self.grants[0]
        self.assertEqual(sid, self.SID)
        self.assertEqual(path.parent, self.secret_dir)

    def test_the_password_pipe_is_readable_by_the_user(self) -> None:
        settings = replace(self.settings, pipe_ssh=True)
        self.start(settings=settings)
        self.assertEqual([pipe.reader_sid for pipe in FakePipe.made], [self.SID])
        self.assertEqual(self.grants, [])

    def test_the_vpn_and_the_checks_stay_elevated(self) -> None:
        """OpenVPN and plink are not started through the limited path."""
        self.start()
        self.new_session().check_login("127.0.0.1")
        self.assertNotIn("openvpn", self.programs())
        self.assertNotIn("plink.exe", self.programs())
        self.assertEqual(len(self.grants), 1)  # only the one of the PuTTY window

    def test_switched_off_starts_with_admin_rights(self) -> None:
        settings = replace(self.settings, clients_without_admin=False)
        self.start(settings=settings)
        self.assertEqual(self.limited_commands, [])
        self.assertEqual(self.grants, [])

    def test_without_admin_rights_there_is_nothing_to_drop(self) -> None:
        with mock.patch("corridoor.winapi.is_admin", lambda: False):
            self.start()
        self.assertEqual(self.limited_commands, [])

    def test_a_failure_is_reported_and_never_falls_back_to_admin(self) -> None:
        connection = self.start()
        with mock.patch("corridoor.winapi.start_limited",
                        side_effect=OSError("no token")):
            connection.add_filezilla()
            self.wait_until(lambda: "Could not start the filezilla session"
                            in self.log_text())
        self.assertIn("no token", self.log_text())
        self.assertIn("without administrator rights", self.log_text())
        self.assertFalse(self.filezilla_marker.exists())  # not started elevated
        self.assertEqual(connection.state, "up")
        self.assertEqual(self.titles(), ["SSH #1"])

    def test_a_failed_grant_stops_that_session_only(self) -> None:
        connection = self.start()
        with mock.patch("corridoor.winapi.grant_read", side_effect=OSError("denied")):
            connection.add_ssh()
            self.wait_until(lambda: "Could not start the ssh session" in self.log_text())
        self.assertEqual(connection.state, "up")
        self.assertEqual(self.titles(), ["SSH #1"])


class DisconnectTest(ConnectionTestCase):
    """Ending the connection closes everything."""

    def test_disconnect_closes_every_session_and_the_vpn(self) -> None:
        connection = self.start()
        connection.add_ssh()
        self.wait_until(lambda: len(self.numbers()) == 2)
        processes = [client.process for client in connection._clients]
        self.disconnect()
        self.assertEqual(self.result, [True])
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertEqual(connection.state, "down")
        self.assertEqual(self.numbers(), [])
        self.assertIn("Disconnecting the VPN", self.log_text())
        self.assert_no_secret_files()

    def test_vpn_dying_ends_the_connection(self) -> None:
        self.fake_openvpn(mode="ready", sleep=1.5)
        connection = self.start()
        self.thread.join(timeout=10)
        self.assertEqual(self.result, [False])
        self.assertIn("OpenVPN stopped unexpectedly", self.log_text())
        self.assertEqual(connection.state, "down")
        self.assertEqual(self.numbers(), [])
        self.assert_no_secret_files()

    def test_switched_off_first_client_is_refused(self) -> None:
        self.start(first="hoptodesk")  # use_hoptodesk is off
        self.thread.join(timeout=10)
        self.assertEqual(self.result, [False])
        self.assertIn("switched off", self.log_text())

    def test_rejected_vpn_login_fails_the_connection(self) -> None:
        self.fake_openvpn(mode="auth_failed")
        self.start()
        self.thread.join(timeout=10)
        self.assertEqual(self.result, [False])
        self.assertIn("wrong VPN user name or password", self.log_text())

    def test_missing_putty_does_not_stop_the_vpn(self) -> None:
        settings = replace(self.settings, use_hoptodesk=True)
        store.save_settings(settings)
        connection = self.start(first="hoptodesk", settings=settings)
        connection.session.settings = replace(settings, putty_exe="/nonexistent")
        connection.add_ssh()
        time.sleep(0.5)
        self.assertEqual(self.titles(), ["HopToDesk"])
        self.assertIn("Could not start the ssh session", self.log_text())
        self.assertEqual(connection.state, "up")


class DebugLogTest(ConnectionTestCase):
    """The debug log of a connection shows the details but no password."""

    def test_debug_log_has_details_and_no_password(self) -> None:
        settings = replace(self.settings, vpn_password="VPNsecret123",
                           ssh_password="SSHsecret456", log_level="debug")
        store.save_settings(settings)
        connection = self.start(settings=settings)
        connection.add_ssh()
        self.wait_until(lambda: len(self.numbers()) == 2)
        connection.close_session(1)
        self.wait_until(lambda: self.numbers() == [2])
        self.disconnect()
        text = self.log_text()
        for expected in ("command: add ssh", "PuTTY:", "closing SSH #1",
                         "sessions now:"):
            self.assertIn(expected, text)
        self.assertNotIn("VPNsecret123", text)
        self.assertNotIn("SSHsecret456", text)


class SecretsTest(ConnectionTestCase):
    """Passwords are removed early and never left behind."""

    def test_password_file_is_deleted_while_the_window_stays_open(self) -> None:
        with mock.patch("corridoor.connection.PWFILE_LIFETIME", 1.0):
            self.start()
            before = len(list(self.secret_dir.glob("tmp_*")))
            self.wait_until(
                lambda: len(list(self.secret_dir.glob("tmp_*"))) == before - 1)
            self.assertEqual(self.numbers(), [1])

    def test_each_session_gets_its_own_password_pipe(self) -> None:
        self.use_fake_pipes()
        settings = replace(self.settings, pipe_vpn_config=True,
                           pipe_vpn_auth=True, pipe_ssh=True)
        store.save_settings(settings)
        connection = self.start(settings=settings)
        connection.add_ssh()
        self.wait_until(lambda: len(self.numbers()) == 2)
        self.assertEqual(len(FakePipe.made), 4)  # profile, login, 2 x password
        connection.close_session(1)
        self.wait_until(lambda: self.numbers() == [2])
        self.assertEqual(sum(pipe.closed for pipe in FakePipe.made), 1)
        self.disconnect()
        self.assertTrue(all(pipe.closed for pipe in FakePipe.made))
        self.assert_no_secret_files()


if __name__ == "__main__":
    unittest.main()
