"""Entry point of Corridoor; the logic lives in the ``corridoor`` package.

    corridoor.exe               VPN + SSH (PuTTY) with the saved settings
    corridoor.exe --hoptodesk   VPN + HopToDesk with the saved settings
    corridoor.exe --config      edit, test and diagnose the settings

Without the build: ``python main.py --config`` in an elevated PowerShell.
"""

import argparse
import sys
import tkinter as tk
from tkinter import messagebox

from corridoor import gui, store, version, winapi
from corridoor.settings import Settings

TITLE: str = version.title()  # "Corridoor 0.2.ab12cd3"


def main() -> int:
    """Check the environment and open the window of the requested mode.

    Returns:
        The process exit code: 0 after a normal run, 1 after a fatal error.
    """
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--version", action="version", version=TITLE)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--config", action="store_true", help="open the settings")
    mode.add_argument("--hoptodesk", action="store_true",
                      help="connect with HopToDesk instead of SSH")
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("This program only works on Windows.")
    root = tk.Tk()
    root.title(TITLE)

    def fail(message: str) -> int:
        """Show a fatal error (there is no console) and return the exit code."""
        root.withdraw()
        messagebox.showerror(TITLE, message)
        return 1

    if not winapi.is_admin():
        return fail("Run as administrator.")
    try:
        store.prepare_storage()
    except Exception as error:
        return fail(f"Cannot prepare the secure folder: {error}")

    store.log.info(TITLE)  # first line of every log, also of exported logs
    config_mode: bool = args.config
    try:
        settings = store.load_settings()
    except Exception as error:
        store.log.error("Saved settings are unreadable: %s", error)
        messagebox.showwarning(TITLE, f"Saved settings are unreadable: {error}")
        settings, config_mode = None, True

    # Without saved settings there is nothing to run, so configure first.
    if config_mode or settings is None:
        root.title(version.title("settings"))
        gui.ConfigWindow(root, settings or Settings())
    else:
        gui.HubWindow(root, settings, "hoptodesk" if args.hoptodesk else "ssh")
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
