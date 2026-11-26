"""The named pipe self-test."""

import unittest
from dataclasses import replace

from corridoor.pipetest import vpn_variants
from tests.support import FakePipe, SessionTestCase


class VariantsTest(unittest.TestCase):
    """Which combinations the pipe test tries, in which order."""

    def test_all_four_with_a_copy_and_a_user(self) -> None:
        self.assertEqual(vpn_variants(True, True),
                         [(True, True), (False, True), (True, False), (False, False)])

    def test_no_copy_means_no_profile_pipe(self) -> None:
        self.assertEqual(vpn_variants(False, True), [(False, True), (False, False)])

    def test_no_user_means_no_credentials_pipe(self) -> None:
        self.assertEqual(vpn_variants(True, False), [(True, False), (False, False)])

    def test_nothing_to_pipe(self) -> None:
        self.assertEqual(vpn_variants(False, False), [(False, False)])


class PipeTestTest(SessionTestCase):
    """:meth:`Session.pipe_test` with a fake OpenVPN that can refuse pipes."""

    def setUp(self) -> None:
        super().setUp()
        self.use_fake_pipes()

    def pipes(self, settings=None, stop=None):
        """Run the test; return the three results, or None if it gave up."""
        settings = settings or self.settings
        session = self.new_session(settings, stop)
        result = session.pipe_test()
        self.assertIs(session.settings, settings, "settings were not restored")
        self.assertTrue(all(pipe.closed for pipe in FakePipe.made))
        self.assert_no_secret_files()
        return None if result is None else tuple(result.values())

    def test_everything_works_with_pipes(self) -> None:
        self.fake_openvpn(count_calls=True)
        self.assertEqual(self.pipes(), (True, True, True))
        self.assertEqual(self.launches(), 1)

    def test_falls_back_when_the_profile_pipe_is_refused(self) -> None:
        self.fake_openvpn(reject=("--config",), count_calls=True)
        self.assertEqual(self.pipes(), (False, True, True))
        self.assertEqual(self.launches(), 2)

    def test_falls_back_when_the_credentials_pipe_is_refused(self) -> None:
        self.fake_openvpn(reject=("--auth-user-pass",), count_calls=True)
        self.assertEqual(self.pipes(), (True, False, True))
        self.assertEqual(self.launches(), 3)

    def test_falls_back_to_files_only(self) -> None:
        self.fake_openvpn(reject="any", count_calls=True)
        self.assertEqual(self.pipes(), (False, False, True))
        self.assertEqual(self.launches(), 4)

    def test_unread_ssh_pipe_is_not_enabled(self) -> None:
        FakePipe.unread = True
        self.assertEqual(self.pipes(), (True, True, False))

    def test_failed_ssh_login_does_not_enable_the_pipe(self) -> None:
        self.fake_plink(err="denied\n", code=1)
        self.assertEqual(self.pipes(), (True, True, False))

    def test_not_copied_profile_is_never_piped(self) -> None:
        own = replace(self.settings, copy_profile=False)
        self.assertEqual(self.pipes(own), (False, True, True))

    def test_without_ssh_password_the_ssh_pipe_is_untested(self) -> None:
        self.assertEqual(self.pipes(replace(self.settings, ssh_password="")),
                         (True, True, False))

    def test_rejected_login_stops_at_once(self) -> None:
        self.fake_openvpn(mode="auth_failed", count_calls=True)
        self.assertIsNone(self.pipes())
        self.assertEqual(self.launches(), 1)  # no retry: it could lock the account

    def test_rejected_login_of_a_running_server_stops_too(self) -> None:
        self.fake_openvpn(mode="auth_failed_hang", sleep=2, count_calls=True)
        self.assertIsNone(self.pipes())
        self.assertEqual(self.launches(), 1)

    def test_never_connecting_tries_every_variant(self) -> None:
        self.fake_openvpn(mode="exit", count_calls=True)
        self.assertIsNone(self.pipes())
        self.assertEqual(self.launches(), 4)

    def test_cancel_changes_nothing(self) -> None:
        self.fake_openvpn(mode="hang", sleep=3)
        self.assertIsNone(self.pipes(stop=self.stop_after(0.5)))


if __name__ == "__main__":
    unittest.main()
