"""Connection test, diagnostics and the remote probe."""

import threading
import unittest
from dataclasses import replace

from corridoor import store
from corridoor.errors import StepError
from tests.support import GOOD_PROBE, SessionTestCase


class RunTest(SessionTestCase):
    """:meth:`Session.run_test`, the step-by-step connection test."""

    def test_connection_test_passes(self) -> None:
        self.assertTrue(self.new_session().run_test())
        self.assert_no_secret_files()

    def test_rejected_vpn_login_fails_with_a_hint(self) -> None:
        self.fake_openvpn(mode="auth_failed")
        self.assertFalse(self.new_session().run_test())
        self.assertIn("wrong VPN user name or password", self.log_text())
        self.assert_no_secret_files()

    def test_missing_ping_reply_is_only_a_warning(self) -> None:
        self.ping_reply = False
        self.assertTrue(self.new_session().run_test())
        self.assertIn("does not answer to ping", self.log_text())

    def test_closed_tcp_port_of_the_vpn_server_fails(self) -> None:
        self.write_profile("client\nproto tcp\nremote 127.0.0.1 1\n")
        store.save_settings(self.settings)
        self.assertFalse(self.new_session().run_test())

    def test_missing_program_fails_before_connecting(self) -> None:
        settings = replace(self.settings, openvpn_exe="/nonexistent/openvpn")
        self.assertFalse(self.new_session(settings).run_test())

    def test_key_option(self) -> None:
        self.assertEqual(self.new_session(replace(
            self.settings, ssh_key="k.ppk")).key_option(), ["-i", "k.ppk"])
        self.assertEqual(self.new_session().key_option(), [])


class FileTransferCheckTest(SessionTestCase):
    """FileZilla needs the SSH port, but neither PuTTY nor an SSH login."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = replace(self.settings, use_ssh=False, use_filezilla=True,
                                putty_exe="/nonexistent")

    def test_quick_test_checks_the_sftp_port(self) -> None:
        self.assertTrue(self.new_session().run_test())
        self.assertIn("Reaching SSH port", self.log_text())

    def test_closed_port_fails(self) -> None:
        self.assertFalse(self.new_session(
            replace(self.settings, port="1")).run_test())

    def test_missing_program_fails(self) -> None:
        self.assertFalse(self.new_session(
            replace(self.settings, filezilla_exe="/nonexistent")).run_test())

    def test_diagnostics_check_the_port(self) -> None:
        self.fake_openvpn(sleep=3)
        self.assertTrue(self.new_session().diagnose())
        self.assertIn("OK       SSH port", self.log_text())
        self.assertNotIn("SSH login", self.log_text())


class HopToDeskTest(SessionTestCase):
    """The HopToDesk client and its checks."""

    def setUp(self) -> None:
        super().setUp()
        self.fake_openvpn(sleep=3)
        self.settings = replace(self.settings, use_hoptodesk=True)
        store.save_settings(self.settings)

    def test_both_clients_in_one_test(self) -> None:
        self.assertTrue(self.new_session().run_test())

    def test_hoptodesk_only_does_not_need_putty(self) -> None:
        settings = replace(self.settings, use_ssh=False, putty_exe="/nonexistent")
        self.assertTrue(self.new_session(settings).run_test())

    def test_unreachable_port_fails(self) -> None:
        settings = replace(self.settings, hoptodesk_port="1")
        self.assertFalse(self.new_session(settings).run_test())
        self.assert_no_secret_files()


class DiagnoseTest(SessionTestCase):
    """:meth:`Session.diagnose` runs everything and summarises."""

    def setUp(self) -> None:
        super().setUp()
        self.fake_openvpn(sleep=3)

    def test_all_checks_pass(self) -> None:
        self.assertTrue(self.new_session().diagnose())
        self.assertIn("=== Summary ===", self.log_text())
        self.assert_no_secret_files()

    def test_failed_vpn_skips_dependent_checks(self) -> None:
        self.fake_openvpn(mode="auth_failed")
        self.assertFalse(self.new_session().diagnose())
        self.assertIn("Ping through VPN: SKIPPED", self.log_text())
        self.assert_no_secret_files()

    def test_remote_hoptodesk_is_checked_over_ssh(self) -> None:
        settings = replace(self.settings, use_hoptodesk=True)
        self.fake_probe("package=installed 1.46.90", *GOOD_PROBE)
        self.assertTrue(self.new_session(settings).diagnose())
        self.assertIn("OK       Remote HopToDesk (via SSH)", self.log_text())

    def test_remote_check_is_skipped_after_a_failed_login(self) -> None:
        settings = replace(self.settings, use_hoptodesk=True)
        self.fake_plink(err="denied\n", code=1)
        self.assertFalse(self.new_session(settings).diagnose())
        self.assertIn("Remote HopToDesk (via SSH): SKIPPED", self.log_text())


class RemoteProbeTest(SessionTestCase):
    """:meth:`Session.check_remote_hoptodesk` against canned plink answers."""

    def probe(self, *lines: str, code: int = 0, err: str = "") -> str:
        """Run the check and return ``ok``, ``warn`` or the error message."""
        self.fake_plink(out="".join(f"{line}\n" for line in lines), err=err,
                        code=code)
        session = self.new_session()
        try:
            session.check_remote_hoptodesk("127.0.0.1")
        except StepError as error:
            return str(error)
        finally:
            session.cleanup()
        return "warn" if session.warned else "ok"

    def test_current_version_is_ok(self) -> None:
        self.assertEqual(self.probe("package=installed 1.46.90", *GOOD_PROBE), "ok")
        self.assertEqual(self.probe("package=installed 1.100.0", *GOOD_PROBE), "ok")

    def test_old_version_warns(self) -> None:
        self.assertEqual(self.probe("package=installed 1.44.4", *GOOD_PROBE), "warn")

    def test_missing_package_information_warns(self) -> None:
        self.assertEqual(self.probe(*GOOD_PROBE), "warn")

    def test_not_installed_fails(self) -> None:
        self.assertIn("not installed", self.probe(
            "command=", "service=inactive", "processes=0"))
        self.assertIn("not installed", self.probe(
            "package=config-files 1.44.4", "command=", "service=", "processes=0"))

    def test_not_running_fails(self) -> None:
        self.assertIn("not running", self.probe(
            "package=installed 1.46.90", "command=x", "service=inactive",
            "processes=0"))

    def test_unusable_answer_needs_linux(self) -> None:
        self.assertIn("Linux", self.probe("garbage"))

    def test_ssh_error_is_passed_on(self) -> None:
        self.assertIn("boom", self.probe(err="boom\n", code=1))


if __name__ == "__main__":
    unittest.main()
