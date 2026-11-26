"""Log detail: the level setting, the debug output and the secrets in it."""

import logging
import unittest
from dataclasses import replace

from corridoor import store
from corridoor.settings import Settings
from tests.support import FakePipe, SessionTestCase

VPN_PASSWORD = "VPNsecret123"
SSH_PASSWORD = "SSHsecret456"


class SettingsTest(unittest.TestCase):
    """Masking of the settings for the log."""

    def test_passwords_are_masked(self) -> None:
        masked = Settings(vpn_password=VPN_PASSWORD, ssh_password=SSH_PASSWORD,
                          vpn_user="demo").masked()
        self.assertEqual(masked["vpn_password"], "***")
        self.assertEqual(masked["ssh_password"], "***")
        self.assertEqual(masked["vpn_user"], "demo")  # not a secret

    def test_unset_passwords_stay_empty(self) -> None:
        self.assertEqual(Settings().masked()["vpn_password"], "")

    def test_default_level_is_info(self) -> None:
        self.assertEqual(Settings().log_level, "info")


class LevelTest(SessionTestCase):
    """``log_level`` in the settings controls the detail of the log."""

    def logger_level(self) -> int:
        return logging.getLogger("corridoor").level

    def test_saving_applies_the_level(self) -> None:
        store.save_settings(replace(self.settings, log_level="debug"))
        self.assertEqual(self.logger_level(), logging.DEBUG)
        store.save_settings(replace(self.settings, log_level="info"))
        self.assertEqual(self.logger_level(), logging.INFO)

    def test_loading_applies_the_level(self) -> None:
        store.save_settings(replace(self.settings, log_level="debug"))
        logging.getLogger("corridoor").setLevel(logging.INFO)
        store.load_settings()
        self.assertEqual(self.logger_level(), logging.DEBUG)

    def test_unknown_level_means_info(self) -> None:
        store.apply_log_level("verbose")
        self.assertEqual(self.logger_level(), logging.INFO)
        store.apply_log_level("DEBUG")  # the case does not matter
        self.assertEqual(self.logger_level(), logging.DEBUG)

    def test_openvpn_chatter_is_debug_only(self) -> None:
        """At info only the OpenVPN lines that matter are shown."""
        self.fake_openvpn(mode="noisy")
        self.assertTrue(self.new_session().run_test())
        self.assertIn("openvpn: TLS: Initial packet", self.log_text())
        self.assertIn("openvpn: Initialization Sequence Completed", self.log_text())
        self.assertNotIn("library versions", self.log_text())
        self.assertNotIn("OpenVPN 2.7 fake build", self.log_text())

    def test_openvpn_chatter_appears_at_debug(self) -> None:
        self.fake_openvpn(mode="noisy")
        logging.getLogger("corridoor").setLevel(logging.DEBUG)
        self.assertTrue(self.new_session().run_test())
        self.assertIn("library versions", self.log_text())

    def test_info_hides_the_details(self) -> None:
        self.assertTrue(self.new_session().run_test())
        self.assertNotIn("starting OpenVPN in", self.log_text())
        self.assertNotIn("settings {", self.log_text())

    def test_debug_shows_the_details(self) -> None:
        settings = replace(self.settings, log_level="debug")
        store.save_settings(settings)
        self.assertTrue(self.new_session(settings).run_test())
        text = self.log_text()
        for expected in ("starting OpenVPN in", "OpenVPN process id",
                         "tunnel is up after", "settings {", "plink:",
                         "checking ", "cleanup:", "secret ("):
            self.assertIn(expected, text)


class SecretsInLogTest(SessionTestCase):
    """The most detailed log must still never contain a password."""

    def setUp(self) -> None:
        super().setUp()
        self.settings = replace(self.settings, vpn_password=VPN_PASSWORD,
                                ssh_password=SSH_PASSWORD, log_level="debug")
        store.save_settings(self.settings)

    def assert_clean(self) -> None:
        text = self.log_text()
        self.assertIn("starting OpenVPN in", text)  # really at debug level
        self.assertNotIn(VPN_PASSWORD, text)
        self.assertNotIn(SSH_PASSWORD, text)

    def test_connection_test(self) -> None:
        self.assertTrue(self.new_session().run_test())
        self.assert_clean()

    def test_diagnostics(self) -> None:
        self.fake_openvpn(sleep=3)
        self.assertTrue(self.new_session().diagnose())
        self.assert_clean()

    def test_pipe_test(self) -> None:
        self.use_fake_pipes()
        self.new_session().pipe_test()
        self.assert_clean()
        self.assertTrue(FakePipe.made)  # pipes were used, their data not logged


if __name__ == "__main__":
    unittest.main()
