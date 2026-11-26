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


class FileZillaCheckTest(SessionTestCase):
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


class SftpCheckTest(SessionTestCase):
    """FileZilla: the SFTP subsystem is started with plink and closed at once."""

    def setUp(self) -> None:
        super().setUp()
        self.arguments = self.work_dir / "plink_args"
        self.fake("plink.exe", f"""
            import json, sys
            with open({str(self.arguments)!r}, "w") as stream:
                json.dump(sys.argv[1:], stream)
            """)
        self.settings = replace(self.settings, use_ssh=False, use_filezilla=True)

    def test_quick_test_starts_the_sftp_subsystem(self) -> None:
        self.assertTrue(self.new_session().run_test())
        import json
        arguments = json.loads(self.arguments.read_text())
        self.assertIn("-s", arguments)
        self.assertEqual(arguments[-2:], ["x@127.0.0.1", "sftp"])
        self.assertIn("Testing SFTP for FileZilla", self.log_text())
        self.assertIn("[8/8]", self.log_text())  # the step is counted

    def test_failure_names_the_likely_causes(self) -> None:
        self.fake_plink(err="Server refused to start a subsystem", code=1)
        self.assertFalse(self.new_session().run_test())
        self.assertIn("SFTP subsystem", self.log_text())
        self.assertIn("Server refused", self.log_text())

    def test_without_a_user_name_it_is_skipped(self) -> None:
        self.assertTrue(self.new_session(
            replace(self.settings, ssh_user="")).run_test())
        self.assertIn("SFTP test skipped", self.log_text())
        self.assertFalse(self.arguments.exists())

    def test_without_plink_it_only_warns(self) -> None:
        self.assertTrue(self.new_session(
            replace(self.settings, putty_exe="/nonexistent")).run_test())
        self.assertIn("SFTP not tested", self.log_text())

    def test_diagnostics_list_the_check(self) -> None:
        self.fake_openvpn(sleep=3)
        self.assertTrue(self.new_session().diagnose())
        self.assertIn("OK       SFTP for FileZilla", self.log_text())

    def test_diagnostics_without_a_user_name(self) -> None:
        self.fake_openvpn(sleep=3)
        self.assertTrue(self.new_session(
            replace(self.settings, ssh_user="")).diagnose())
        self.assertIn("SFTP for FileZilla: not checked", self.log_text())


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
