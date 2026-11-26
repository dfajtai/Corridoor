"""The Windows parts that can be tried for real: starting a program with fewer rights."""

import subprocess
import sys
import unittest

from corridoor import pipes

ON_WINDOWS = sys.platform == "win32"


class PipeAccessTest(unittest.TestCase):
    """The access rules of a secret pipe."""

    def test_only_administrators_and_system_by_default(self) -> None:
        self.assertEqual(pipes.descriptor_text(), "D:P(A;;GA;;;BA)(A;;GA;;;SY)")

    def test_a_reader_gets_read_access_only(self) -> None:
        text = pipes.descriptor_text("S-1-5-21-1-2-3-1001")
        self.assertTrue(text.endswith("(A;;GR;;;S-1-5-21-1-2-3-1001)"))
        self.assertEqual(text.count("GA"), 2)  # still only two with full access


@unittest.skipUnless(ON_WINDOWS, "needs Windows")
class StartLimitedTest(unittest.TestCase):
    """Real processes; works elevated (linked or restricted token) and not."""

    def setUp(self) -> None:
        from corridoor import winapi
        self.winapi = winapi

    def start(self, code: str, *arguments: str):
        process = self.winapi.start_limited([sys.executable, "-c", code, *arguments])
        self.addCleanup(process.terminate)
        return process

    def test_program_runs_and_its_exit_code_is_reported(self) -> None:
        process = self.start("import sys; sys.exit(3)")
        self.assertEqual(process.wait(timeout=10), 3)
        self.assertEqual(process.poll(), 3)

    def test_poll_wait_and_terminate_like_popen(self) -> None:
        process = self.start("import time; time.sleep(30)")
        self.assertIsNone(process.poll())
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(timeout=0.3)
        process.terminate()
        process.wait(timeout=10)
        self.assertIsNotNone(process.poll())

    def test_arguments_with_spaces_arrive_intact(self) -> None:
        process = self.start(
            "import sys; sys.exit(0 if sys.argv[1:] == ['a b', 'c'] else 9)",
            "a b", "c")
        self.assertEqual(process.wait(timeout=10), 0)

    def test_a_missing_program_raises_oserror(self) -> None:
        with self.assertRaises(OSError):
            self.winapi.start_limited([r"C:\does\not\exist.exe"])

    def test_user_sid_looks_like_a_sid(self) -> None:
        self.assertRegex(self.winapi.user_sid(), r"^S-1-5-21(-\d+)+$")


if __name__ == "__main__":
    unittest.main()
