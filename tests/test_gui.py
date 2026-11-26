"""GUI code paths with a stand-in for tkinter (no real widgets are created)."""

import importlib
import sys
import types
import unittest
from unittest import mock
from unittest.mock import MagicMock

from corridoor.connection import Connection
from corridoor.session import Session
from corridoor.settings import Settings


class Variable:
    """Stand-in for ``tk.StringVar`` / ``tk.BooleanVar``."""

    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Frame:
    """Stand-in for ``ttk.Frame``: layout and timer calls do nothing."""

    def __init__(self, *args, **options):
        pass

    def __getattr__(self, name):
        if name.startswith(("grid", "columnconfigure", "rowconfigure", "after")):
            return MagicMock()
        raise AttributeError(name)


def import_gui():
    """Import ``corridoor.gui`` against a stub ``tkinter`` module."""
    tk = types.ModuleType("tkinter")
    tk.StringVar = tk.BooleanVar = Variable
    tk.Variable = Variable
    tk.Tk = MagicMock
    tk.ttk = MagicMock()
    tk.ttk.Frame = Frame
    tk.filedialog = tk.messagebox = tk.scrolledtext = MagicMock()
    with mock.patch.dict(sys.modules, {"tkinter": tk}):
        sys.modules.pop("corridoor.gui", None)
        return importlib.import_module("corridoor.gui")


gui = import_gui()
sys.modules.pop("corridoor.gui", None)  # do not leak the stubbed module


class GuiTest(unittest.TestCase):
    """The settings window and the hub window, driven without a display."""

    def window(self):
        return gui.ConfigWindow(MagicMock(), Settings(profile_file="a", host="h"))

    def test_form_returns_the_settings_it_was_given(self) -> None:
        self.assertEqual(self.window().current_settings(),
                         Settings(profile_file="a", host="h"))

    def test_save_validates_before_storing(self) -> None:
        window = self.window()
        with mock.patch("corridoor.store.save_settings") as save:
            self.assertIsNotNone(window.save())
            save.assert_called_once()
            window.variables["use_ssh"].set(False)  # neither client left
            self.assertIsNone(window.save())
            self.assertEqual(save.call_count, 1)

    def hub(self, connection):
        """Create a HubWindow whose (stub) connection ends at once."""
        with mock.patch.object(Connection, "run", lambda self, client="ssh": True):
            window = gui.HubWindow(MagicMock(), Settings(), "ssh")
            window.worker.join()
        window.connection = connection
        return window

    def test_hub_window_reports_success(self) -> None:
        self.assertTrue(self.hub(None).success)

    def test_hub_buttons_follow_what_the_connection_allows(self) -> None:
        connection = MagicMock()
        connection.sessions.return_value = [(1, "SSH #1")]
        connection.can_add.side_effect = lambda kind: kind == "ssh"
        window = self.hub(connection)
        # The stubbed ttk hands out one shared button; use separate ones here.
        for name in ("add_ssh_button", "add_hop_button", "close_button"):
            setattr(window, name, MagicMock())
        window.refresh()
        window.add_ssh_button.configure.assert_called_with(state="normal")
        window.add_hop_button.configure.assert_called_with(state="disabled")
        window.close_button.configure.assert_called_with(state="normal")

    def test_hub_buttons_pass_commands_on(self) -> None:
        connection = MagicMock()
        window = self.hub(connection)
        window.add_ssh()
        window.add_hoptodesk()
        window.tree.selection.return_value = ("3",)
        window.close_selected()
        connection.add_ssh.assert_called_once()
        connection.add_hoptodesk.assert_called_once()
        connection.close_session.assert_called_once_with(3)

    def test_pipe_test_result_is_applied_and_saved(self) -> None:
        window = self.window()
        results = {"pipe_vpn_config": False, "pipe_vpn_auth": True, "pipe_ssh": True}
        with mock.patch("corridoor.store.save_settings") as save, \
                mock.patch.object(Session, "pipe_test", lambda self: results):
            window.pipe_test()
            window.worker.join()
            window.refresh()
        saved = save.call_args.args[0]
        self.assertTrue(saved.pipe_vpn_auth and saved.pipe_ssh)
        self.assertFalse(saved.pipe_vpn_config)

    def test_failed_pipe_test_changes_no_setting(self) -> None:
        window = self.window()
        with mock.patch("corridoor.store.save_settings") as save, \
                mock.patch.object(Session, "pipe_test", lambda self: None):
            window.pipe_test()  # one save for the form itself
            window.worker.join()
            window.refresh()
        self.assertEqual(save.call_count, 1)


if __name__ == "__main__":
    unittest.main()
