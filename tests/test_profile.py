"""Profile parsing, checking and bundling, plus the small pure helpers."""

import tempfile
import unittest
from pathlib import Path

from corridoor import hoptodesk
from corridoor.errors import StepError
from corridoor.profile import (bundle_profile, check_profile, split_directive,
                               vpn_remotes)


class ProfileTest(unittest.TestCase):
    """Text handling of ``.ovpn`` profiles."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_split_directive_ignores_comments_and_dashes(self) -> None:
        self.assertEqual(split_directive("# comment"), [])
        self.assertEqual(split_directive("  ; comment"), [])
        self.assertEqual(split_directive(""), [])
        self.assertEqual(split_directive('--remote "a b" 1'), ["remote", "a b", "1"])

    def test_bundle_embeds_files_and_drops_credential_file(self) -> None:
        (self.folder / "ca.crt").write_text("CA\n")
        (self.folder / "ta.key").write_text("TA\n")
        profile = self.folder / "a.ovpn"
        profile.write_text(
            'client\nca ca.crt\ntls-auth "ta.key" 1\nauth-user-pass pw.txt\n'
            "<cert>\nplugin-not-a-directive\n</cert>\n")
        bundle = bundle_profile(profile)
        check_profile(bundle)  # the text inside <cert> is no directive
        self.assertIn("<ca>\nCA\n</ca>", bundle)
        self.assertIn("key-direction 1\n<tls-auth>", bundle)
        self.assertNotIn("pw.txt", bundle)

    def test_bundle_reports_a_missing_file(self) -> None:
        profile = self.folder / "a.ovpn"
        profile.write_text("ca missing.crt\n")
        with self.assertRaises(StepError):
            bundle_profile(profile)

    def test_bundle_refuses_askpass_file(self) -> None:
        profile = self.folder / "a.ovpn"
        profile.write_text("askpass secret.txt\n")
        with self.assertRaises(StepError):
            bundle_profile(profile)

    def test_check_rejects_forbidden_directives(self) -> None:
        for bad in ("plugin x.dll", "--redirect-gateway def1", "config other.ovpn"):
            with self.subTest(bad), self.assertRaises(StepError):
                check_profile(bad)

    def test_vpn_remotes_uses_defaults_and_connection_blocks(self) -> None:
        text = ("proto tcp-client\nport 443\nremote a\n"
                "<connection>\nremote b 1200 udp\n</connection>\n")
        self.assertEqual(vpn_remotes(text), [("a", 443, "tcp"), ("b", 1200, "udp")])

    def test_vpn_remotes_builtin_defaults(self) -> None:
        self.assertEqual(vpn_remotes("remote vpn.example.com\n"),
                         [("vpn.example.com", 1194, "udp")])


class HopToDeskHelpersTest(unittest.TestCase):
    """Parsing of the remote probe and of versions."""

    def test_parse_probe_ignores_noise(self) -> None:
        output = "Welcome\npackage=installed 1.46.90\nservice=active\n"
        self.assertEqual(hoptodesk.parse_probe(output),
                         {"package": "installed 1.46.90", "service": "active"})

    def test_version_numbers_compare_numerically(self) -> None:
        self.assertEqual(hoptodesk.version_numbers("1.46.90-2ubuntu1"), [1, 46, 90])
        self.assertGreater(hoptodesk.version_numbers("1.100.0"),
                           hoptodesk.version_numbers(hoptodesk.MIN_VERSION))
        self.assertLess(hoptodesk.version_numbers("1.44.4"),
                        hoptodesk.version_numbers(hoptodesk.MIN_VERSION))


if __name__ == "__main__":
    unittest.main()
