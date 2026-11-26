# Corridoor

*One corridor, one door.*

A lightweight Windows launcher that brings up an OpenVPN **split tunnel** to a
single host and then opens sessions to it: **SSH** (PuTTY), **remote desktop**
(HopToDesk) and **file transfer** (FileZilla over SFTP). Only the traffic addressed to
that host uses the VPN; everything else keeps its normal route. One corridor, one
door, as many rooms behind it as you need: several sessions share the one VPN
connection.

![Settings window](docs/images/settings.png)

> **Status:** VPN split tunnel, SSH, named pipes and HopToDesk have been used
> successfully on Windows. FileZilla (SFTP), starting the programs without
> administrator rights, folder permissions, crash cleanup through the Job Object
> and the build and install scripts have not been verified separately.
> See [tested versions](docs/windows-setup.md#tested-versions).

## Install in 5 minutes

### Checklist: what must be on the computer

- [ ] Windows 10/11, 64-bit, and an administrator account
- [ ] [OpenVPN **Community**](https://openvpn.net/community-downloads/) 2.7 (not OpenVPN Connect)
- [ ] [PuTTY](https://www.chiark.greenend.org.uk/~sgtatham/putty/latest.html) (`putty.exe` and `plink.exe`), [HopToDesk](https://www.hoptodesk.com/download) 1.46.90+ and/or [FileZilla](https://filezilla-project.org/) (as many as you want to use)
- [ ] [Python](https://www.python.org/downloads/windows/) 3.10 to 3.14, 64-bit, with **Add to PATH** and **tcl/tk**
- [ ] [Poetry](https://python-poetry.org/docs/#installation) 2.2+
- [ ] [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) with **Desktop development with C++**
- [ ] The `.ovpn` profile (and VPN user/password if it asks) and the remote computer's internal IP
- [ ] The remote computer is ready: [remote setup](docs/remote-setup.md)

### Install

In a **regular** PowerShell window in the repository folder:

```powershell
poetry install
poetry run powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

Then, in an **elevated** PowerShell window in the same folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Then open **Corridoor Settings** (desktop shortcut), fill in the profile, VPN login
and remote host, select **Quick test**, and start **Corridoor SSH**,
**Corridoor HopToDesk** or **Corridoor FileZilla**. Details: [Windows setup](docs/windows-setup.md).

## Shortcuts

It handles three kinds of sessions on the one VPN connection: **SSH terminals**
(several PuTTY windows), **remote desktop** (one HopToDesk session) and **file
transfer** (one FileZilla session over SFTP). Each has its own shortcut:

| | Shortcut | Opens the VPN and |
|---|---|---|
| <img src="docs/images/icons/run.png" width="40" alt="SSH"> | **Corridoor SSH** | a PuTTY window |
| <img src="docs/images/icons/hop.png" width="40" alt="HopToDesk"> | **Corridoor HopToDesk** | a HopToDesk remote desktop |
| <img src="docs/images/icons/files.png" width="40" alt="FileZilla"> | **Corridoor FileZilla** | FileZilla over SFTP |
| <img src="docs/images/icons/config.png" width="40" alt="Settings"> | **Corridoor Settings** | the settings, tests and diagnostics (no VPN) |

More sessions can be added from the window of any of them.

## Features

- **Single-host split tunnel:** routes pushed by the server are ignored.
- **Several sessions on one VPN:** add SSH (PuTTY) windows (4 by default, up to 10),
  one HopToDesk session and one FileZilla session (SFTP file transfer) to a list, and
  close each on its own. Disconnecting closes all.
- **Programs without administrator rights:** only the launcher and OpenVPN are
  elevated; PuTTY, HopToDesk and FileZilla run as you, with normal rights.
- **Settings window** with tabs, a step-by-step connection test (including the SFTP
  subsystem for FileZilla) and full diagnostics.
- **Live log:** next to the settings, with detail (info/debug) and text filter that
  work while it runs.
- **Encrypted storage:** settings, passwords and the profile copy are protected
  with Windows DPAPI in an administrator-only folder.
- **Named pipes (test feature):** secrets reach OpenVPN and PuTTY without temporary files.
- **Clean shutdown:** closing the main window stops every session and the VPN, and
  a Windows Job Object ends OpenVPN and all PuTTY windows if the launcher crashes.
- **Standard library only** at runtime.

## How it works

1. OpenVPN starts with `--route-nopull` and a `/32` route to the target.
2. The launcher waits for `Initialization Sequence Completed`.
3. It opens the first session (PuTTY, HopToDesk or FileZilla) against the target
   address; more can be added from the list.
4. It stops all sessions and OpenVPN when you disconnect.

## Documentation

| Document | Content |
|---|---|
| [Remote setup](docs/remote-setup.md) | Ubuntu: SSH server, HopToDesk, firewall |
| [Windows setup](docs/windows-setup.md) | downloads, tested versions, build, install, first configuration |
| [Configuration](docs/configuration.md) | every setting, named pipes, tests and diagnostics |
| [Security](docs/security.md) | what is stored where, protections, limitations |
| [Troubleshooting](docs/troubleshooting.md) | symptoms and what to check |
| [Architecture](docs/architecture.md) | modules, data flow, development and tests |

## Development

```powershell
python -m unittest discover -s tests -t .
githooks\install.ps1
```

The second command (once per clone) enables the git hook that keeps the version
current. The version is `0.2.<commit id>`: the base `0.2` is the first line of the
`VERSION` file and changes only on request, the commit id is written by the
post-commit hook (and by `build.ps1`) into the untracked file
`corridoor/_version.py`. It is shown in every window title, in the first log line
and by `corridoor.exe --version`.

The tests run on any system: Windows APIs and the external programs are replaced
by stand-ins, so they do not replace a test on a real Windows machine.

## License

[MIT](LICENSE). OpenVPN, PuTTY and HopToDesk are separate programs with their own
licenses; they are not included.
