"""The version: base from ``VERSION``, commit id from the post-commit hook."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corridoor import version

ROOT = Path(__file__).resolve().parents[1]


def find_sh():
    """Return a POSIX ``sh`` (Git for Windows ships one) or None."""
    found = shutil.which("sh")
    if found:
        return found
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parent.parent / "usr" / "bin" / "sh.exe"
        if candidate.exists():
            return str(candidate)
    return None


class VersionModuleTest(unittest.TestCase):
    """:mod:`corridoor.version`."""

    def test_version_file_holds_major_and_minor(self) -> None:
        text = (ROOT / "VERSION").read_text().splitlines()[0]
        self.assertRegex(text, r"^\d+\.\d+$")
        self.assertEqual(version.base_version(), text)

    def test_generated_version_is_used(self) -> None:
        with mock.patch.dict("sys.modules", {"corridoor._version": mock.Mock(
                __version__="0.2.ab12cd3")}):
            self.assertEqual(version.get_version(), "0.2.ab12cd3")

    def test_without_generated_file_the_version_is_dev(self) -> None:
        with mock.patch.dict("sys.modules", {"corridoor._version": None}):
            self.assertEqual(version.get_version(), f"{version.base_version()}.dev")

    def test_title(self) -> None:
        with mock.patch.object(version, "get_version", lambda: "0.2.ab12cd3"):
            self.assertEqual(version.title(), "Corridoor 0.2.ab12cd3")
            self.assertEqual(version.title("settings"),
                             "Corridoor 0.2.ab12cd3 - settings")

    def test_real_version_has_the_expected_shape(self) -> None:
        self.assertRegex(version.get_version(), r"^\d+\.\d+\.\w+$")


@unittest.skipUnless(find_sh() and shutil.which("git"), "needs sh and git")
class HookTest(unittest.TestCase):
    """Run the real hook in a throw-away repository."""

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name)
        (self.repo / "corridoor").mkdir()
        shutil.copy(ROOT / "githooks" / "post-commit", self.repo / "post-commit")
        self.git("init", "-q")

    def run_hook(self) -> str:
        subprocess.run([find_sh(), "post-commit"], cwd=self.repo, check=True)
        return (self.repo / "corridoor" / "_version.py").read_text()

    def commit(self, base: str) -> str:
        (self.repo / "VERSION").write_text(base + "\n")
        self.git("add", "VERSION")
        self.git("commit", "-q", "-m", "x")
        return self.git("rev-parse", "--short", "HEAD")

    def test_hook_writes_base_and_commit_id(self) -> None:
        commit = self.commit("0.2")
        self.assertIn(f'__version__ = "0.2.{commit}"', self.run_hook())

    def test_new_commit_gives_new_id_and_base_follows_the_file(self) -> None:
        self.commit("0.2")
        commit = self.commit("0.3")
        self.assertIn(f'__version__ = "0.3.{commit}"', self.run_hook())

    def test_hook_script_has_unix_line_endings(self) -> None:
        text = (ROOT / "githooks" / "post-commit").read_bytes()
        self.assertNotIn(b"\r", text)
        self.assertTrue(text.startswith(b"#!/bin/sh"))

    def test_generated_file_is_not_tracked(self) -> None:
        ignored = (ROOT / ".gitignore").read_text()
        self.assertTrue(re.search(r"^corridoor/_version\.py$", ignored, re.M))


if __name__ == "__main__":
    unittest.main()
