# Architecture

Corridoor is a small layered program. Each module has one job, and imports only
from modules listed above it.

| Module | Responsibility |
|---|---|
| `main.py` | entry point: checks the environment, opens the window of the requested mode |
| `corridoor/errors.py` | `StepError` (a step failed) and `Cancelled` (the user stopped) |
| `corridoor/winapi.py` | **the only code that talks to Windows**: DPAPI, mutex, exit event, job object |
| `corridoor/version.py` | the version `<VERSION>.<commit id>` and the window title |
| `corridoor/hoptodesk.py` | HopToDesk port, minimum version, the remote probe and its parser |
| `corridoor/settings.py` | the `Settings` dataclass |
| `corridoor/profile.py` | pure `.ovpn` text handling: parse, check, bundle, list the servers |
| `corridoor/net.py` | `ping` and the routing table lookup |
| `corridoor/pipes.py` | `SecretPipe`: a one-way named pipe for exactly one process |
| `corridoor/store.py` | the administrator-only folder: settings, profile copy, log |
| `corridoor/session.py` | `Session`: the steps, the OpenVPN process, one-shot checks, launching PuTTY and HopToDesk |
| `corridoor/connection.py` | `Connection`: the tunnel plus the list of client sessions |
| `corridoor/diagnose.py` | the full diagnostics run, built on `Session` |
| `corridoor/pipetest.py` | the named pipe self-test, built on `Session` |
| `corridoor/gui.py` | Tkinter windows: `ConfigWindow` (tabs, General first; actions in three boxes: settings, checks, pipes) and `HubWindow` |

## Data flow of a connection

```
main.py ──► gui.HubWindow ──(thread)──► Connection.run
                                          │
   store.load_profile ◄───────────────────┤  1. programs, profile, name
   Session.vpn_tunnel ────────────────────┤  2. OpenVPN in a job object
   Session.launch_putty / launch_hoptodesk┤  3. first session, then the list
   command queue (add / close) ◄── GUI    ┤  4. serve until disconnect
                                          └  5. close sessions, VPN, secrets
```

`Connection.run` is the only code that starts, watches and stops client
processes. The GUI thread sends commands (`add_ssh`, `add_hoptodesk`,
`close_session`) through a queue and reads copies of the session list, so no lock
is held while a process is started or stopped.

## Sessions on one connection

- Up to `MAX_SSH` (10) SSH sessions, at most one HopToDesk session and at most one
  FileZilla session (`sftp://user@host:port`, started with
  `-l interactive`, so it asks for the password itself).
- Every PuTTY window has its own password secret (file or pipe), released when
  that window closes. A password *file* is also deleted after 10 seconds.
- Closing a session never touches the VPN. **Disconnect** closes every session,
  then asks OpenVPN to exit through the exit event.
- PuTTY windows join the same kill-on-close **job object** as OpenVPN, so a crash
  of the launcher ends them too. This is crash protection only; the normal close
  is an explicit `terminate()`. HopToDesk is not in the job, because it may hand
  the request over to an already running instance, so it is only listed and
  removed from the list, never killed. FileZilla is treated the same way.

The GUI never calls the logic directly from the Tk thread. A job runs on a
background thread and reports only through the `corridoor` logger; the GUI moves
the log records into its text box with a queue.

## Version

`VERSION` (tracked) holds the base, for example `0.2`; it changes only on request.
The commit id cannot be part of the commit it names, so `githooks/post-commit`
writes `corridoor/_version.py` (`__version__ = "0.2.ab12cd3"`) *after* each commit,
and `build.ps1` writes it before a build. That file is listed in `.gitignore`, so the
working tree stays clean. Without it, `version.get_version()` returns `0.2.dev`.
`githooks/install.ps1` enables the hook (`core.hooksPath`), because `.git/hooks`
is not cloned.

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
- the exit event is a file that the fake OpenVPN waits for,
- OpenVPN, plink, PuTTY and HopToDesk are small Python scripts that
  `subprocess.Popen` starts with the interpreter,
- a local socket greets like an SSH server.

| File | Covers |
|---|---|
| `test_version.py` | version module and the post-commit hook (run in a throw-away repository) |
| `test_profile.py` | profile parsing, checking, bundling; HopToDesk helpers |
| `test_store.py` | settings round trip, profile copy, leftovers |
| `test_session.py` | connection test, HopToDesk checks, diagnostics, remote probe |
| `test_connection.py` | session list: add, close, limits, disconnect, secrets, a dying VPN |
| `test_pipes.py` | pipe variants and fallbacks of the pipe test |
| `test_gui.py` | window logic with a stub for Tkinter |

The real Windows parts (DPAPI, the job object, the named pipe server, the
permissions of the folder) and the real programs are **not** covered; test them
on a Windows machine.
