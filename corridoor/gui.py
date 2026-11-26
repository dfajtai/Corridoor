"""Tkinter windows: the settings editor and the small status window.

Both windows derive from :class:`LogWindow`, which shows the ``corridoor`` log in
a text box and runs one :class:`~corridoor.session.Session` job at a time on a
background thread. Tk is not thread safe, so the worker only logs; the GUI
thread moves the records into the widget every 200 ms (:meth:`LogWindow.poll`).

* :class:`ConfigWindow`  edit the settings, save, test, diagnose, pipe test
* :class:`RunWindow`     connect at once and close after a successful session
"""

import logging
import queue
import threading
import tkinter as tk
from dataclasses import asdict
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

from . import store
from .session import Session
from .settings import Settings

log = logging.getLogger("corridoor")

# (field name, label, kind); kinds: text, secret, file, check, header
FIELDS: list[tuple[str, str, str]] = [
    ("", "VPN", "header"),
    ("profile_file", "OpenVPN profile", "file"),
    ("copy_profile", "Copy the profile (with keys) into the encrypted store", "check"),
    ("vpn_user", "VPN user", "text"),
    ("vpn_password", "VPN password", "secret"),
    ("openvpn_exe", "openvpn.exe", "file"),
    ("host", "Remote host (inside VPN)", "text"),
    ("", "SSH (PuTTY)", "header"),
    ("use_ssh", "Use SSH", "check"),
    ("port", "SSH port", "text"),
    ("ssh_user", "SSH user", "text"),
    ("ssh_password", "SSH password", "secret"),
    ("ssh_key", "SSH key .ppk (optional)", "file"),
    ("putty_exe", "putty.exe", "file"),
    ("", "HopToDesk (direct IP)", "header"),
    ("use_hoptodesk", "Use HopToDesk", "check"),
    ("hoptodesk_port", "Direct IP port", "text"),
    ("hoptodesk_exe", "hoptodesk.exe", "file"),
    ("", "Test features (named pipes instead of temporary files)", "header"),
    ("pipe_vpn_config", "VPN profile via pipe (copied profile only)", "check"),
    ("pipe_vpn_auth", "VPN credentials via pipe", "check"),
    ("pipe_ssh", "SSH password via pipe", "check"),
]


class QueueHandler(logging.Handler):
    """Hand log messages over to the GUI thread (Tk is not thread safe)."""

    def __init__(self) -> None:
        """Create the handler with its message queue."""
        super().__init__()
        self.messages: "queue.Queue[str]" = queue.Queue()
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        """Queue the formatted record."""
        self.messages.put(self.format(record))


class LogWindow(ttk.Frame):
    """Base window: a log view plus the handling of one background session."""

    def __init__(self, root: tk.Tk, log_row: int, log_height: int = 14) -> None:
        """Create the frame and the log box in the given grid row."""
        super().__init__(root, padding=10)
        self.root = root
        self.stop = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.success = False

        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(log_row, weight=1)
        self.text = scrolledtext.ScrolledText(self, width=90, height=log_height,
                                              state="disabled")
        self.text.grid(row=log_row, column=0, columnspan=3, sticky="nsew")

        self.handler = QueueHandler()
        log.addHandler(self.handler)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()

    def running(self) -> bool:
        """Return True while a session thread is alive."""
        return self.worker is not None and self.worker.is_alive()

    def start_session(self, settings: Settings,
                      job: Callable[[Session], bool]) -> None:
        """Run ``job`` with a new session in a background thread."""
        self.stop.clear()

        def work() -> None:
            self.success = job(Session(settings, self.stop))

        self.worker = threading.Thread(target=work)
        self.worker.start()

    def poll(self) -> None:
        """Move queued messages into the text box and refresh the buttons."""
        while not self.handler.messages.empty():
            self.text.configure(state="normal")
            self.text.insert("end", self.handler.messages.get() + "\n")
            self.text.see("end")
            self.text.configure(state="disabled")
        self.after(200, self.poll)
        self.refresh()  # last: it may destroy the window

    def refresh(self) -> None:
        """Update widgets according to the session state (overridden)."""

    def close(self) -> None:
        """Stop the session, wait for its cleanup, then close the window."""
        self.stop.set()
        if self.running():
            self.after(200, self.close)
            return
        self.root.destroy()


class ConfigWindow(LogWindow):
    """Edit, save and test the settings."""

    def __init__(self, root: tk.Tk, settings: Settings) -> None:
        """Build the form and fill it with the given settings."""
        super().__init__(root, log_row=len(FIELDS) + 1, log_height=8)
        values = asdict(settings)
        self.variables: dict[str, tk.Variable] = {}
        for row, (name, label, kind) in enumerate(FIELDS):
            if kind == "header":
                ttk.Label(self, text=label, font=("TkDefaultFont", 9, "bold")
                          ).grid(row=row, column=0, columnspan=2, sticky="w",
                                 pady=(6, 0))
                continue
            if kind == "check":
                variable: tk.Variable = tk.BooleanVar(value=values[name])
                ttk.Checkbutton(self, text=label, variable=variable
                                ).grid(row=row, column=1, sticky="w")
            else:
                variable = tk.StringVar(value=values[name])
                ttk.Label(self, text=label).grid(row=row, column=0, sticky="w",
                                                 padx=(0, 8))
                ttk.Entry(self, textvariable=variable,
                          show="*" if kind == "secret" else ""
                          ).grid(row=row, column=1, sticky="ew", pady=1)
            if kind == "file":
                ttk.Button(self, text="...", width=3,
                           command=lambda v=variable: self.browse(v)
                           ).grid(row=row, column=2, padx=(4, 0))
            self.variables[name] = variable

        buttons = ttk.Frame(self)
        buttons.grid(row=len(FIELDS), column=0, columnspan=3, pady=8, sticky="e")
        self.save_button = ttk.Button(buttons, text="Save", command=self.save)
        self.save_button.pack(side="left", padx=4)
        self.test_button = ttk.Button(buttons, text="Save & test", command=self.test)
        self.test_button.pack(side="left")
        self.diagnose_button = ttk.Button(buttons, text="Diagnostics",
                                          command=self.diagnose)
        self.diagnose_button.pack(side="left", padx=4)
        ttk.Button(buttons, text="Export log...", command=self.export_log
                   ).pack(side="left")
        self.pipe_button = ttk.Button(buttons, text="Pipe test",
                                      command=self.pipe_test)
        self.pipe_button.pack(side="left", padx=4)
        # Filled by the worker thread, applied by the GUI thread.
        self.pipe_results: Optional[dict[str, bool]] = None
        # The button that started the running job; it turns into "Cancel".
        self.active_button: Optional[ttk.Button] = None

    def browse(self, variable: tk.StringVar) -> None:
        """Pick a file and store its path in the given variable."""
        path = filedialog.askopenfilename()
        if path:
            variable.set(path)

    def current_settings(self) -> Settings:
        """Collect the content of the form."""
        return Settings(
            **{name: var.get() for name, var in self.variables.items()})

    def save(self) -> Optional[Settings]:
        """Validate and store the form; return the settings on success."""
        settings = self.current_settings()
        def valid(port: str) -> bool:
            return port.isdecimal() and 0 < int(port) < 65536

        problem = ""
        if not (settings.profile_file and settings.host):
            problem = "The profile and the remote host are required."
        elif not (settings.use_ssh or settings.use_hoptodesk):
            problem = "Switch on SSH, HopToDesk, or both."
        elif settings.use_ssh and not valid(settings.port):
            problem = "The SSH port is invalid."
        elif settings.use_hoptodesk and not valid(settings.hoptodesk_port):
            problem = "The HopToDesk port is invalid."
        if problem:
            messagebox.showwarning("Check the settings", problem)
            return None
        try:
            store.save_settings(settings)
        except Exception as error:  # show every failure, there is no console
            log.error("Saving failed: %s", error)
            messagebox.showerror("Saving failed", str(error))
            return None
        return settings

    def test(self) -> None:
        """Save, then run the connection test (stops at the first failure)."""
        self.launch(self.test_button, lambda session: session.run(test_only=True))

    def diagnose(self) -> None:
        """Save, then run all diagnostic checks and print a summary."""
        self.launch(self.diagnose_button, Session.diagnose)

    def pipe_test(self) -> None:
        """Save, then test the named pipes; working ones get switched on."""
        def job(session: Session) -> bool:
            self.pipe_results = session.pipe_test()
            return self.pipe_results is not None

        self.launch(self.pipe_button, job)

    def apply_pipe_results(self) -> None:
        """Store the outcome of a finished pipe test."""
        results, self.pipe_results = self.pipe_results, None
        if results is None:
            return
        for name, works in results.items():
            self.variables[name].set(works)
        if self.save() is not None:
            log.info("The working pipes are now switched on and saved.")

    def launch(self, button: ttk.Button,
               job: Callable[[Session], bool]) -> None:
        """Start a job, or cancel the running one."""
        if self.running():
            self.stop.set()
            return
        settings = self.save()
        if settings is not None:
            self.active_button = button
            self.start_session(settings, job)

    def export_log(self) -> None:
        """Write the visible log into a file chosen by the user."""
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", initialfile="corridoor_diagnostics.txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(self.text.get("1.0", "end"))
        except OSError as error:
            messagebox.showerror("Export failed", str(error))

    def refresh(self) -> None:
        """Lock the other buttons while a job runs and switch the labels."""
        if not hasattr(self, "active_button"):  # first poll, form not built yet
            return
        running = self.running()
        if not running:
            self.apply_pipe_results()
        self.save_button.configure(state="disabled" if running else "normal")
        for button, label in ((self.test_button, "Save & test"),
                              (self.diagnose_button, "Diagnostics"),
                              (self.pipe_button, "Pipe test")):
            active = running and button is self.active_button
            button.configure(
                text="Cancel" if active else label,
                state="disabled" if running and not active else "normal")


class RunWindow(LogWindow):
    """Normal mode: connect immediately and close after a successful session."""

    def __init__(self, root: tk.Tk, settings: Settings, client: str) -> None:
        """Create the window and connect with the given client."""
        super().__init__(root, log_row=0)
        self.button = ttk.Button(self, text="Disconnect", command=self.close)
        self.button.grid(row=1, column=2, pady=(8, 0), sticky="e")
        self.start_session(
            settings, lambda session: session.run(test_only=False, client=client))

    def refresh(self) -> None:
        """Close on success; keep the log visible after a failure."""
        if self.worker is None:
            return
        if not self.running():
            if self.success:
                self.root.destroy()
            else:
                self.button.configure(text="Close")
