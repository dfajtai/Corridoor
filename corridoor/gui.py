"""Tkinter windows: the settings editor and the connection window.

Both windows derive from :class:`LogWindow`, which shows the ``corridoor`` log in
a text box and runs one :class:`~corridoor.session.Session` job at a time on a
background thread. Tk is not thread safe, so the worker only logs; the GUI
thread moves the records into the widget every 200 ms (:meth:`LogWindow.poll`).

* :class:`ConfigWindow`  tabs to edit the settings; below them three clearly
  separated actions: **store** the settings, **check** the connection, and
  **try the pipes** (a way to hand secrets to programs, see the Advanced tab)
* :class:`HubWindow`     one VPN connection with a list of client sessions
"""

import logging
import queue
import threading
import tkinter as tk
from dataclasses import asdict
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Optional

from . import store
from .connection import Connection
from .session import Session
from .settings import Settings

log = logging.getLogger("corridoor")

# Choices of the "Log detail" field, from the least to the most detailed.
LOG_CHOICES: list[str] = list(store.LOG_LEVELS)
LOG_LEVELS_BY_NAME: dict[str, int] = store.LOG_LEVELS

# One tab: (title, introduction, fields). A field is (name, label, kind) with
# kind one of: text, secret, file, check, choice, header, note. ``header`` and
# ``note`` show text only (a bold title / an explanation) and have no name.
Field = tuple[str, str, str]
TABS: list[tuple[str, str, list[Field]]] = [
    ("General", "Where the programs are, and which of them Corridoor may start. "
                "The settings of each program are on its own tab.", [
        ("", "Programs", "header"),
        ("openvpn_exe", "openvpn.exe (always needed)", "file"),
        ("putty_exe", "putty.exe", "file"),
        ("hoptodesk_exe", "hoptodesk.exe", "file"),
        ("filezilla_exe", "filezilla.exe", "file"),
        ("", "Allowed programs (OpenVPN is always used)", "header"),
        ("use_ssh", "SSH: PuTTY windows", "check"),
        ("use_hoptodesk", "HopToDesk: remote desktop", "check"),
        ("use_filezilla", "FileZilla: file transfer over SFTP", "check"),
        ("", "A program that is not allowed cannot be started and is not tested. "
             "Allow at least one.", "note"),
    ]),
    ("VPN", "The OpenVPN connection. Only the remote host below is sent through "
            "it; everything else keeps its normal route.", [
        ("profile_file", "OpenVPN profile", "file"),
        ("copy_profile", "Keep an encrypted copy of the profile (with keys)", "check"),
        ("vpn_user", "VPN user", "text"),
        ("vpn_password", "VPN password", "secret"),
        ("", "Leave the user empty if the profile logs in with a certificate only.",
         "note"),
        ("host", "Remote host (inside the VPN)", "text"),
    ]),
    ("SSH", "PuTTY windows on the remote host; you can open several at once. "
            "Allow SSH on the General tab.", [
        ("port", "SSH port", "text"),
        ("max_ssh", "Max. PuTTY windows at once (1-10)", "text"),
        ("ssh_user", "SSH user", "text"),
        ("ssh_password", "SSH password", "secret"),
        ("ssh_key", "SSH key .ppk (optional)", "file"),
    ]),
    ("HopToDesk", "Remote desktop through HopToDesk (direct IP), one session at a "
                  "time; the password is typed into HopToDesk. Allow HopToDesk on "
                  "the General tab.", [
        ("hoptodesk_port", "Direct IP port", "text"),
    ]),
    ("FileZilla", "File transfer over SFTP, through the SSH port. The user and "
                  "port come from the SSH tab, and FileZilla asks for the password "
                  "itself: Corridoor never passes it. Allow FileZilla on the "
                  "General tab.", [
        ("", "Nothing to set here. Make sure the SSH port and user are right. "
             "The tests also start the server's SFTP subsystem with plink.exe "
             "(from the PuTTY folder).", "note"),
    ]),
    ("Advanced", "You rarely need this tab.", [
        ("", "Programs Corridoor opens", "header"),
        ("clients_without_admin",
         "Start PuTTY, HopToDesk and FileZilla without administrator rights", "check"),
        ("", "Corridoor needs administrator rights for the VPN, but the programs it "
             "opens for you do not: they run as you, with normal rights. Untick this "
             "only if one of them cannot be started that way.", "note"),
        ("", "Handing secrets to programs", "header"),
        ("", "By default, OpenVPN and PuTTY read the VPN profile and the passwords "
             "from a short-lived temporary file in the protected folder. A named "
             "pipe hands over the same data from memory instead, so nothing is "
             "written to disk. Whether your versions accept a pipe has to be "
             "tried, so use the Pipe test below the tabs: it switches on only the "
             "pipes that work. You do not need to tick these by hand.", "note"),
        ("pipe_vpn_config", "VPN profile via pipe (copied profile only)", "check"),
        ("pipe_vpn_auth", "VPN credentials via pipe", "check"),
        ("pipe_ssh", "SSH password via pipe", "check"),
        ("", "Log", "header"),
        ("log_level", "Log detail", "choice"),
        ("", "info: steps and results. debug: nearly everything (never "
             "passwords). Takes effect when you save.", "note"),
    ]),
]

# Width in pixels after which explanations wrap.
WRAP: int = 560


# Lines of the log kept in the window, so the filter can be changed afterwards.
LOG_BUFFER: int = 5000


class QueueHandler(logging.Handler):
    """Hand log messages over to the GUI thread (Tk is not thread safe)."""

    def __init__(self) -> None:
        """Create the handler with its message queue of ``(level, text)``."""
        super().__init__()
        self.messages: "queue.Queue[tuple[int, str]]" = queue.Queue()
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))

    def format(self, record: logging.LogRecord) -> str:
        """Format a record; debug lines get a ``[debug]`` marker after the time."""
        text = super().format(record)
        if record.levelno <= logging.DEBUG:
            time_part, _, rest = text.partition(" ")
            text = f"{time_part} [debug] {rest}"
        return text

    def emit(self, record: logging.LogRecord) -> None:
        """Queue the formatted record together with its level."""
        self.messages.put((record.levelno, self.format(record)))


class LogWindow(ttk.Frame):
    """Base window: a log view plus the handling of one background session."""

    def __init__(self, root: tk.Tk, log_row: int, log_height: int = 14,
                 side: bool = False) -> None:
        """Create the frame and the log box.

        Args:
            root: The window.
            log_row: Grid row of the log (below the other parts).
            log_height: Height of the log in text lines.
            side: Put the log to the right of everything else (column 1,
                full height) instead of below it.
        """
        super().__init__(root, padding=12)
        self.root = root
        try:  # the native Windows look; other themes keep working without it
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        self.stop = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.success = False

        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(log_row, weight=1)
        # The log: a small bar with the live controls above the text.
        self.lines: list[tuple[int, str]] = []   # everything received (bounded)
        self.level_variable = tk.StringVar(value=self.logger_level())
        self.filter_variable = tk.StringVar(value="")
        holder = ttk.Frame(self)
        if side:
            holder.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        else:
            holder.grid(row=log_row, column=0, columnspan=3, sticky="nsew")
        holder.columnconfigure(3, weight=1)
        holder.rowconfigure(1, weight=1)
        ttk.Label(holder, text="Detail").grid(row=0, column=0, padx=(0, 4))
        chooser = ttk.Combobox(holder, textvariable=self.level_variable,
                               values=LOG_CHOICES, state="readonly", width=7)
        chooser.grid(row=0, column=1)
        chooser.bind("<<ComboboxSelected>>", lambda event: self.change_level())
        ttk.Label(holder, text="Filter").grid(row=0, column=2, padx=(12, 4))
        finder = ttk.Entry(holder, textvariable=self.filter_variable)
        finder.grid(row=0, column=3, sticky="ew")
        finder.bind("<KeyRelease>", lambda event: self.redraw())
        self.text = scrolledtext.ScrolledText(
            holder, width=62 if side else 90, height=log_height, state="disabled")
        self.text.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=(6, 0))

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

    @staticmethod
    def logger_level() -> str:
        """Return the current detail of the log as ``info`` or ``debug``."""
        return "debug" if log.getEffectiveLevel() <= logging.DEBUG else "info"

    def visible(self, level: int, text: str) -> bool:
        """Return True if a line passes the detail and the text filter."""
        if level < LOG_LEVELS_BY_NAME.get(self.level_variable.get(), logging.INFO):
            return False
        wanted = self.filter_variable.get().strip().lower()
        return not wanted or wanted in text.lower()

    def change_level(self) -> None:
        """Apply the detail chosen in the window to the running program, at once.

        Lines that were not recorded before (debug lines of the time when the
        detail was ``info``) cannot be shown afterwards.
        """
        store.apply_log_level(self.level_variable.get())
        self.redraw()

    def redraw(self) -> None:
        """Show again the kept lines that pass the current detail and filter."""
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for level, text in self.lines:
            if self.visible(level, text):
                self.text.insert("end", text + "\n")
        self.text.see("end")
        self.text.configure(state="disabled")

    def poll(self) -> None:
        """Move queued messages into the text box and refresh the buttons."""
        current = self.logger_level()
        if current != self.level_variable.get():  # changed by saving the settings
            self.level_variable.set(current)
            self.redraw()
        while not self.handler.messages.empty():
            item = self.handler.messages.get()
            level, text = item if isinstance(item, tuple) else (logging.INFO, item)
            self.lines.append((level, text))
            del self.lines[:-LOG_BUFFER]
            if self.visible(level, text):
                self.text.configure(state="normal")
                self.text.insert("end", text + "\n")
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
    """Edit the settings (tabs), store them, check them, try the pipes.

    The window has two columns. On the left, top to bottom:

    1. **Tabs** with the settings, one tab per topic.
    2. **Actions**, in three boxes: *Settings* (save), *Checks* (quick test and
       diagnostics: do the settings work?) and *Pipes* (the pipe test: which way
       of handing secrets to programs works here?).

    On the right, over the full height, the **log** of whatever is running.
    """

    def __init__(self, root: tk.Tk, settings: Settings) -> None:
        """Build the form and fill it with the given settings."""
        super().__init__(root, log_row=0, log_height=28, side=True)
        values = asdict(settings)
        self.variables: dict[str, tk.Variable] = {}

        self.left = ttk.Frame(self)  # tabs and actions; the log is on the right
        self.left.grid(row=0, column=0, sticky="ns")
        self.left.rowconfigure(0, weight=1)
        notebook = self.notebook = ttk.Notebook(self.left)
        notebook.grid(row=0, column=0, sticky="nsew")
        for title, intro, fields in TABS:
            tab = ttk.Frame(notebook, padding=14)
            tab.columnconfigure(1, weight=1)
            notebook.add(tab, text=title)
            ttk.Label(tab, text=intro, wraplength=WRAP, foreground="#555555"
                      ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
            for row, (name, label, kind) in enumerate(fields, start=1):
                self.add_field(tab, row, values, name, label, kind)

        self.build_actions()
        # Filled by the worker thread, applied by the GUI thread.
        self.pipe_results: Optional[dict[str, bool]] = None
        # The button that started the running job; it turns into "Cancel".
        self.active_button: Optional[ttk.Button] = None

    def add_field(self, tab: ttk.Frame, row: int, values: dict, name: str,
                  label: str, kind: str) -> None:
        """Place one field of a tab and remember its variable by name."""
        if kind == "header":
            ttk.Label(tab, text=label, font=("TkDefaultFont", 10, "bold")
                      ).grid(row=row, column=0, columnspan=3, sticky="w",
                             pady=(10, 2))
            return
        if kind == "note":
            ttk.Label(tab, text=label, wraplength=WRAP, foreground="#555555"
                      ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
            return
        if kind == "check":
            variable: tk.Variable = tk.BooleanVar(value=values[name])
            ttk.Checkbutton(tab, text=label, variable=variable
                            ).grid(row=row, column=0, columnspan=3, sticky="w",
                                   pady=2)
        else:
            variable = tk.StringVar(value=values[name])
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w",
                                            padx=(0, 10), pady=3)
            if kind == "choice":
                ttk.Combobox(tab, textvariable=variable, values=LOG_CHOICES,
                             state="readonly", width=10
                             ).grid(row=row, column=1, sticky="w", pady=3)
            else:
                ttk.Entry(tab, textvariable=variable,
                          show="*" if kind == "secret" else ""
                          ).grid(row=row, column=1, sticky="ew", pady=3)
            if kind == "file":
                ttk.Button(tab, text="...", width=3,
                           command=lambda v=variable: self.browse(v)
                           ).grid(row=row, column=2, padx=(6, 0))
        self.variables[name] = variable

    def build_actions(self) -> None:
        """Build the three boxes under the tabs: settings, checks, pipes."""
        bar = ttk.Frame(self.left)
        bar.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        for column in range(3):
            bar.columnconfigure(column, weight=1, uniform="actions")

        def box(column: int, title: str, explanation: str) -> ttk.Frame:
            frame = ttk.LabelFrame(bar, text=title, padding=8)
            frame.grid(row=0, column=column, sticky="nsew",
                       padx=(0 if column == 0 else 6, 0))
            frame.columnconfigure(0, weight=1)
            ttk.Label(frame, text=explanation, wraplength=170,
                      foreground="#555555").grid(row=1, column=0, columnspan=3,
                                                 sticky="w", pady=(6, 0))
            return frame

        settings_box = box(0, "Settings", "Stores the settings, encrypted.")
        self.save_button = ttk.Button(settings_box, text="Save", command=self.save)
        self.save_button.grid(row=0, column=0, sticky="w")
        ttk.Button(settings_box, text="Export log...", command=self.export_log
                   ).grid(row=0, column=1, padx=(6, 0))

        checks_box = box(1, "Checks", "Quick test: connect once and check every "
                                      "enabled client. Diagnostics: run every "
                                      "check and summarise.")
        self.test_button = ttk.Button(checks_box, text="Quick test",
                                      command=self.test)
        self.test_button.grid(row=0, column=0, sticky="w")
        self.diagnose_button = ttk.Button(checks_box, text="Diagnostics",
                                          command=self.diagnose)
        self.diagnose_button.grid(row=0, column=1, padx=(6, 0))

        pipes_box = box(2, "Pipes", "Tries the pipes of the Advanced tab for "
                                    "real and switches on those that work.")
        self.pipe_button = ttk.Button(pipes_box, text="Pipe test",
                                      command=self.pipe_test)
        self.pipe_button.grid(row=0, column=0, sticky="w")

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
        elif not (settings.use_ssh or settings.use_hoptodesk
                  or settings.use_filezilla):
            problem = "Allow at least one of SSH, HopToDesk and FileZilla (General tab)."
        elif (settings.use_ssh or settings.use_filezilla) and not valid(settings.port):
            problem = "The SSH port is invalid."
        elif not (settings.max_ssh.isdecimal() and 1 <= int(settings.max_ssh) <= 10):
            problem = "The maximum number of PuTTY windows must be 1 to 10."
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
        """Save, then run the quick test (stops at the first failure)."""
        self.launch(self.test_button, lambda session: session.run_test())

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
                stream.write(self.text.get("1.0", "end"))  # what is shown now
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
        for button, label in ((self.test_button, "Quick test"),
                              (self.diagnose_button, "Diagnostics"),
                              (self.pipe_button, "Pipe test")):
            active = running and button is self.active_button
            button.configure(
                text="Cancel" if active else label,
                state="disabled" if running and not active else "normal")


class HubWindow(LogWindow):
    """Normal mode: one VPN connection and a list of client sessions.

    The VPN is brought up at once and the first session of the requested kind
    is added. More SSH sessions (up to the ``max_ssh`` setting), one HopToDesk
    session and one
    FileZilla session can be added; each can be closed alone. ``Disconnect``
    (or the window's X) closes all sessions and the VPN. After a failure the window stays open with the log.
    """

    def __init__(self, root: tk.Tk, settings: Settings, client: str) -> None:
        """Create the window and connect; ``client`` is the first session."""
        super().__init__(root, log_row=2, log_height=10)
        self.connection: Optional[Connection] = None
        self.shown: list[tuple[int, str]] = []

        # No column heading: a heading row looks clickable and is only noise.
        self.tree = ttk.Treeview(self, show="tree", height=4, selectmode="browse")
        self.tree.column("#0", stretch=True)
        self.tree.grid(row=0, column=0, columnspan=3, sticky="ew")

        buttons = ttk.Frame(self)
        buttons.grid(row=1, column=0, columnspan=3, pady=8, sticky="e")
        self.add_ssh_button = ttk.Button(buttons, text="Add SSH session",
                                         command=self.add_ssh)
        self.add_hop_button = ttk.Button(buttons, text="Add HopToDesk session",
                                         command=self.add_hoptodesk)
        self.add_files_button = ttk.Button(buttons, text="Add FileZilla session",
                                           command=self.add_filezilla)
        self.close_button = ttk.Button(buttons, text="Close session",
                                       command=self.close_selected)
        self.disconnect_button = ttk.Button(buttons, text="Disconnect",
                                            command=self.close)
        for button in (self.add_ssh_button, self.add_hop_button,
                       self.add_files_button, self.close_button,
                       self.disconnect_button):
            button.pack(side="left", padx=2)

        def job(session: Session) -> bool:
            self.connection = Connection(session)
            return self.connection.run(client)

        self.start_session(settings, job)

    def add_ssh(self) -> None:
        """Add another PuTTY window to the connection."""
        if self.connection:
            self.connection.add_ssh()

    def add_hoptodesk(self) -> None:
        """Add the HopToDesk session to the connection."""
        if self.connection:
            self.connection.add_hoptodesk()

    def add_filezilla(self) -> None:
        """Add the FileZilla session to the connection."""
        if self.connection:
            self.connection.add_filezilla()

    def close_selected(self) -> None:
        """Close the selected session; the VPN stays up."""
        selection = self.tree.selection()
        if self.connection and selection:
            self.connection.close_session(int(selection[0]))

    def refresh(self) -> None:
        """Show the sessions, enable the buttons, close after a clean exit."""
        if self.worker is None:
            return
        connection = self.connection
        if connection is not None:
            sessions = connection.sessions()
            if sessions != self.shown:
                self.tree.delete(*self.tree.get_children())
                for number, title in sessions:
                    self.tree.insert("", "end", iid=str(number), text=title)
                self.shown = sessions
            for button, kind in ((self.add_ssh_button, "ssh"),
                                 (self.add_hop_button, "hoptodesk"),
                                 (self.add_files_button, "filezilla")):
                button.configure(
                    state="normal" if connection.can_add(kind) else "disabled")
            self.close_button.configure(
                state="normal" if sessions else "disabled")
        if not self.running():
            if self.success:
                self.root.destroy()  # last: nothing may touch the window after
            else:
                self.disconnect_button.configure(text="Close")
