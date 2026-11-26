"""GUI code paths with a stand-in for tkinter (no real widgets are created)."""

import importlib
import logging
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
        for name in ("add_ssh_button", "add_hop_button", "add_files_button",
                     "close_button"):
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

    def test_every_setting_is_on_exactly_one_tab(self) -> None:
        """A setting missing from the tabs could never be changed by the user."""
        from dataclasses import fields
        shown = [name for _, _, tab in gui.TABS for name, _, kind in tab
                 if kind not in ("header", "note")]
        self.assertEqual(sorted(shown), sorted(f.name for f in fields(Settings)))

    def test_tabs_are_in_the_expected_order(self) -> None:
        self.assertEqual([title for title, _, _ in gui.TABS],
                         ["General", "VPN", "SSH", "HopToDesk", "FileZilla", "Advanced"])

    def test_programs_and_their_switches_are_on_the_general_tab(self) -> None:
        """Paths and the allow switches live together on the first tab."""
        general = {name for name, _, kind in gui.TABS[0][2]
                   if kind not in ("header", "note")}
        self.assertEqual(general, {
            "openvpn_exe", "putty_exe", "hoptodesk_exe", "filezilla_exe",
            "use_ssh", "use_hoptodesk", "use_filezilla"})

    def test_other_tabs_have_no_program_path_or_switch(self) -> None:
        for title, _, fields in gui.TABS[1:]:
            for name, _, _ in fields:
                self.assertFalse(name.endswith("_exe") or name.startswith("use_"),
                                 f"{name} belongs on the General tab, not {title}")

    def test_clients_without_admin_is_a_switch_on_the_advanced_tab(self) -> None:
        advanced = dict((name, kind) for name, _, kind in gui.TABS[-1][2])
        self.assertEqual(advanced["clients_without_admin"], "check")
        self.assertTrue(Settings().clients_without_admin)

    def test_every_tab_explains_itself(self) -> None:
        for title, intro, _ in gui.TABS:
            self.assertTrue(intro.strip(), title)

    def test_filezilla_alone_is_a_valid_client_choice(self) -> None:
        window = self.window()
        window.variables["use_ssh"].set(False)
        window.variables["use_filezilla"].set(True)
        with mock.patch("corridoor.store.save_settings") as save:
            self.assertIsNotNone(window.save())
            save.assert_called_once()

    def test_filezilla_needs_a_valid_ssh_port(self) -> None:
        window = self.window()
        window.variables["use_ssh"].set(False)
        window.variables["use_filezilla"].set(True)
        window.variables["port"].set("nope")
        with mock.patch("corridoor.store.save_settings") as save:
            self.assertIsNone(window.save())
            save.assert_not_called()

    def test_hub_filezilla_button_passes_the_command_on(self) -> None:
        connection = MagicMock()
        self.hub(connection).add_filezilla()
        connection.add_filezilla.assert_called_once()

    def test_debug_lines_are_marked_in_the_window(self) -> None:
        handler = gui.QueueHandler()
        for level, marker in ((logging.DEBUG, "[debug] "), (logging.INFO, "")):
            record = logging.LogRecord("corridoor", level, "", 0, "hello", (), None)
            text = handler.format(record)
            self.assertTrue(text.endswith(f"{marker}hello"), text)
            self.assertEqual("[debug]" in text, bool(marker))

    def test_max_ssh_is_a_validated_field_of_the_ssh_tab(self) -> None:
        window = self.window()
        self.assertEqual(window.variables["max_ssh"].get(), "4")
        with mock.patch("corridoor.store.save_settings") as save:
            for bad in ("0", "11", "x", ""):
                window.variables["max_ssh"].set(bad)
                self.assertIsNone(window.save(), bad)
            window.variables["max_ssh"].set("10")
            self.assertIsNotNone(window.save())
            self.assertEqual(save.call_count, 1)

    def test_log_lines_are_kept_with_their_level(self) -> None:
        window = self.window()
        window.handler.messages.put((logging.DEBUG, "12:00:00 [debug] detail"))
        window.handler.messages.put((logging.INFO, "12:00:01 headline"))
        window.poll()
        self.assertEqual([level for level, _ in window.lines],
                         [logging.DEBUG, logging.INFO])

    def test_detail_filter_hides_debug_lines_afterwards(self) -> None:
        window = self.window()
        window.level_variable.set("debug")
        self.assertTrue(window.visible(logging.DEBUG, "x"))
        window.level_variable.set("info")
        self.assertFalse(window.visible(logging.DEBUG, "x"))
        self.assertTrue(window.visible(logging.INFO, "x"))
        self.assertTrue(window.visible(logging.WARNING, "x"))

    def test_text_filter_is_live_and_ignores_the_case(self) -> None:
        window = self.window()
        window.level_variable.set("debug")
        window.filter_variable.set("  SSH ")
        self.assertTrue(window.visible(logging.INFO, "SSH #1 started."))
        self.assertTrue(window.visible(logging.INFO, "plink ssh login"))
        self.assertFalse(window.visible(logging.INFO, "HopToDesk started."))
        window.filter_variable.set("")
        self.assertTrue(window.visible(logging.INFO, "HopToDesk started."))

    def test_redraw_shows_only_the_matching_lines(self) -> None:
        window = self.window()
        window.lines = [(logging.INFO, "one"), (logging.DEBUG, "two"),
                        (logging.INFO, "three")]
        window.level_variable.set("info")
        window.filter_variable.set("t")
        window.text = MagicMock()
        window.redraw()
        shown = [call.args[1] for call in window.text.insert.call_args_list]
        self.assertEqual(shown, ["three\n"])  # "two" is debug, "one" has no "t"

    def test_changing_the_detail_applies_it_to_the_running_program(self) -> None:
        window = self.window()
        window.level_variable.set("debug")
        with mock.patch("corridoor.store.apply_log_level") as apply:
            window.change_level()
        apply.assert_called_once_with("debug")

    def test_the_buffer_is_bounded(self) -> None:
        window = self.window()
        for number in range(gui.LOG_BUFFER + 50):
            window.handler.messages.put((logging.INFO, f"line {number}"))
        window.poll()
        self.assertEqual(len(window.lines), gui.LOG_BUFFER)
        self.assertEqual(window.lines[-1][1], f"line {gui.LOG_BUFFER + 49}")

    def test_log_detail_is_a_field_of_the_form(self) -> None:
        window = self.window()
        self.assertEqual(window.variables["log_level"].get(), "info")
        self.assertEqual(window.current_settings().log_level, "info")

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
