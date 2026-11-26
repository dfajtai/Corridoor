# Architecture

Corridoor is a small layered program. Each module has one job, and imports only
from modules listed above it.

| Module | Responsibility |
|---|---|
| `main.py` | entry point: checks the environment, opens the window of the requested mode |
| `corridoor/errors.py` | `StepError` (a step failed) and `Cancelled` (the user stopped) |
| `corridoor/winapi.py` | **the only code that talks to Windows**: DPAPI, mutex, exit event, job object |
| `corridoor/hoptodesk.py` | HopToDesk port, minimum version, the remote probe and its parser |
| `corridoor/settings.py` | the `Settings` dataclass |
| `corridoor/profile.py` | pure `.ovpn` text handling: parse, check, bundle, list the servers |
| `corridoor/net.py` | `ping` and the routing table lookup |
| `corridoor/pipes.py` | `SecretPipe`: a one-way named pipe for exactly one process |
| `corridoor/store.py` | the administrator-only folder: settings, profile copy, log |
| `corridoor/session.py` | `Session`: one job, its steps and its child processes |
| `corridoor/diagnose.py` | the full diagnostics run, built on `Session` |
| `corridoor/pipetest.py` | the named pipe self-test, built on `Session` |
| `corridoor/gui.py` | Tkinter windows: `ConfigWindow` and `RunWindow` |

## Data flow of a connection

```
main.py ──► gui.RunWindow ──(thread)──► Session.run
                                          │
   store.load_profile ◄───────────────────┤  1. programs, profile, name
   Session.vpn_tunnel ────────────────────┤  2. OpenVPN in a job object
   Session.run_putty / run_hoptodesk ─────┘  3. the client, until disconnect
                                             4. cleanup: pipes, temp files, VPN
```

The GUI never calls the logic directly from the Tk thread. A job runs on a
background thread and reports only through the `corridoor` logger; the GUI moves
the log records into its text box with a queue.

## Design rules

- **Windows code stays in `winapi.py`.** Other modules call `winapi.name(...)`
  through the module, so tests can replace it. Pure logic (`profile`, `hoptodesk`)
  never imports it.
- **Steps either pass, warn or raise.** A step raises `StepError` with a message
  that tells the user what to check. `Session.warn` reports a problem that must
  not stop the session. `Cancelled` is a normal exit.
- **Secrets have one owner.** `Session.secret` returns a path and an announcer. The
  session removes every temporary file and closes every pipe in `cleanup`, which
  runs in a `finally` of every job.
- **The tunnel is always shut down.** `Session.vpn_tunnel` is a context manager
  that signals OpenVPN to exit and, as a last resort, kills it.

## Tests

```powershell
python -m unittest discover -s tests -t .
```

They run on any operating system. `tests/support.py` builds the environment:

- the Windows functions are replaced (XOR instead of DPAPI, no mutex, no job object),
- `store` points to a temporary folder,
- OpenVPN, plink, PuTTY and HopToDesk are small Python scripts that
  `subprocess.Popen` starts with the interpreter,
- a local socket greets like an SSH server.

| File | Covers |
|---|---|
| `test_profile.py` | profile parsing, checking, bundling; HopToDesk helpers |
| `test_store.py` | settings round trip, profile copy, leftovers |
| `test_session.py` | connection test, real session, HopToDesk, diagnostics, remote probe |
| `test_pipes.py` | pipe variants and fallbacks, sessions with pipes |
| `test_gui.py` | window logic with a stub for Tkinter |

The real Windows parts (DPAPI, the job object, the named pipe server, the
permissions of the folder) and the real programs are **not** covered; test them
on a Windows machine.
