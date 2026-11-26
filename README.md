# Corridoor

*One corridor, one door.*

A lightweight Windows launcher that brings up an OpenVPN **split tunnel** to a
single host and then opens an SSH (PuTTY) or HopToDesk session to it. Only the
traffic addressed to that host uses the VPN; everything else keeps its normal route.

> **Project status:** VPN split tunnel, SSH, the named pipes and HopToDesk have been
> used successfully on Windows (see [Tested versions](#tested-versions)). Folder
> permissions, crash cleanup through the Job Object and the build and install scripts
> have not been verified separately.

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Tested versions](#tested-versions)
- [Remote computer setup (Ubuntu)](#remote-computer-setup-ubuntu)
- [Local computer setup (Windows)](#local-computer-setup-windows)
- [Daily use](#daily-use)
- [Configuration](#configuration)
- [Testing and diagnostics](#testing-and-diagnostics)
- [Security](#security)
- [Limitations](#limitations)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Development](#development)
- [License](#license)

## Features

- **Single-host split tunnel:** routes pushed by the server are ignored.
- **Two clients:** SSH through PuTTY, or remote desktop through HopToDesk (direct IP).
- **Settings window** with a step-by-step connection test and full diagnostics.
- **Encrypted storage:** settings, passwords and an optional profile copy are
  protected with Windows DPAPI in an administrator-only folder.
- **Named pipes (test feature):** the profile and passwords can be handed to
  OpenVPN and PuTTY without temporary files.
- **Clean shutdown:** OpenVPN is stopped when the session ends, and a Windows Job
  Object terminates it if the launcher crashes.
- **Few dependencies:** the program uses only the Python standard library.

## How it works

1. OpenVPN is started with `--route-nopull` and a host route (`/32`) to the target.
2. The launcher waits for `Initialization Sequence Completed`.
3. It starts PuTTY or HopToDesk against the target address.
4. It stops OpenVPN when the session is over.

## Requirements

You also need an existing OpenVPN server that can reach the remote computer, its
`.ovpn` profile, and (if it asks for them) a VPN user name and password.

### Downloads: local computer (Windows)

| Software | Download | Needed for |
|---|---|---|
| OpenVPN **Community** 2.7 | [openvpn.net/community-downloads](https://openvpn.net/community-downloads/) | always; provides `openvpn.exe`. OpenVPN Connect is not supported. |
| PuTTY 0.77 or newer | [PuTTY latest release](https://www.chiark.greenend.org.uk/~sgtatham/putty/latest.html) | SSH mode; `putty.exe` and `plink.exe` in the same folder |
| HopToDesk **1.46.90** or newer | [HopToDesk64.exe](https://dl.hoptodesk.com/HopToDesk64.exe), [all downloads](https://www.hoptodesk.com/download) | HopToDesk mode |
| Python **3.10 to 3.14**, 64-bit | [python.org for Windows](https://www.python.org/downloads/windows/) | build, or running from source |
| Visual Studio Build Tools | [C++ build tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) | build; select **Desktop development with C++** |
| Poetry 2.2 or newer | [installation guide](https://python-poetry.org/docs/#installation) | build; installs Nuitka from `pyproject.toml` |

Nuitka needs a C compiler. Its automatically downloaded MinGW64 does not support
Python 3.13 or newer, so install the Build Tools for those versions.

### Downloads: remote computer (Ubuntu, 64-bit Intel/AMD)

| Software | Source | Needed for |
|---|---|---|
| OpenSSH server | Ubuntu package `openssh-server` | SSH mode and the remote HopToDesk check |
| HopToDesk **1.46.90** or newer | [hoptodesk.deb](https://dl.hoptodesk.com/hoptodesk.deb), [all downloads](https://www.hoptodesk.com/download) | HopToDesk mode |

HopToDesk 1.44.4 does not work; keep **both** computers up to date.

## Tested versions

| Component | Version | Result |
|---|---|---|
| Windows | build 10.0.26300, 64-bit | works |
| OpenVPN Community | 2.7.9 | works, including profile and credentials through named pipes |
| PuTTY | 0.83 | SSH works; the password pipe was tested with `plink` |
| HopToDesk | 1.46.90 | works |
| HopToDesk | 1.44.4 | **does not work:** the session keeps reconnecting |

The remote computer in the HopToDesk test ran Ubuntu Linux.

## Remote computer setup (Ubuntu)

Do this first, while you can reach the machine directly or over an existing connection.

### 1. SSH server

```bash
sudo apt update
sudo apt install openssh-server
sudo systemctl enable --now ssh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub   # note the fingerprint
ip -4 -brief address                               # note the internal IP address
```

### 2. HopToDesk

```bash
mkdir -p ~/hoptodesk-install && cd ~/hoptodesk-install
wget -O hoptodesk.deb https://dl.hoptodesk.com/hoptodesk.deb
sudo apt install ./hoptodesk.deb
sudo systemctl enable --now hoptodesk
dpkg-query -W hoptodesk                            # 1.46.90 or newer
```

If `apt` reports unmet dependencies of an unrelated package, repair that first. Run
`sudo apt-get -s ...` to preview what a repair would remove.

In the HopToDesk window on this computer:

1. Set your own permanent password and enable unattended access.
2. Open **Settings > Network** and enable **Direct IP access** (default TCP `21118`).
3. On Wayland, enable **Settings > Security > Unattended Wayland Access**.
   If the login screen is still unreachable, switch GDM to X11: set
   `WaylandEnable=false` under `[daemon]` in `/etc/gdm3/custom.conf`, then reboot.

After an upgrade, restart it with `sudo systemctl restart hoptodesk` and check that
Direct IP access is still enabled.

### 3. Firewall

Needed only if `sudo ufw status` shows `active`. Log in the way you will use the tool
and run `echo $SSH_CLIENT`; the first value is the address this computer sees.

```bash
sudo ufw allow from <address-or-vpn-subnet> to any port 22 proto tcp
sudo ufw allow from <address-or-vpn-subnet> to any port 21118 proto tcp
sudo ufw status numbered
```

Add the SSH rule before enabling or tightening the firewall, so that you do not lock
yourself out.

### 4. Check

```bash
systemctl is-active ssh hoptodesk
ss -ltn | grep -E ':(22|21118) '
```

## Local computer setup (Windows)

### 1. Install the software

1. Install OpenVPN Community, PuTTY and HopToDesk from the links above.
2. Install Python with **Add python.exe to PATH** and **tcl/tk** selected.
3. Install the Build Tools with **Desktop development with C++**.
4. Install Poetry.
5. Check in a new PowerShell window: `python --version` and `poetry --version`

### 2. Build and install Corridoor

Clone the repository (or extract the source archive into an empty folder). In a **regular** PowerShell window in
that folder:

```powershell
poetry install
poetry run powershell -NoProfile -ExecutionPolicy Bypass -File .\build.ps1
```

Immediately afterwards, in an **elevated** PowerShell window in the same folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The program is installed to `C:\Program Files\Corridoor_app\`. Shortcuts are placed
on the desktop and in the Start menu for all users.

| Shortcut | Command | Purpose |
|---|---|---|
| Corridoor SSH | `corridoor.exe` | connect with PuTTY |
| Corridoor HopToDesk | `corridoor.exe --hoptodesk` | connect with HopToDesk |
| Corridoor Settings | `corridoor.exe --config` | configure, test, diagnose |

If no settings have been saved yet, every shortcut opens the settings window.

Without building, run it from an elevated PowerShell window:
`python .\corridoor.py --config`

**Updating:** close Corridoor, update the sources, and repeat the build and install steps. Saved
settings are kept.

### 3. Configure

1. Open **Corridoor Settings**.
2. **VPN:** select the `.ovpn` profile, keep **Copy the profile** on, enter the VPN
   user and password, check the `openvpn.exe` path.
3. **Remote host:** the internal IP only, without user name, port or leading zeros.
4. **SSH:** enable it, enter port, user and password (or key), check `putty.exe`.
5. **HopToDesk:** enable it, keep port `21118`, check the `hoptodesk.exe` path
   (`Program Files` or `Program Files (x86)`).
6. Select **Save & test**. The first SSH login may fail because the host key is unknown:
   close the settings, start **Corridoor SSH**, compare the fingerprint with the one
   noted on the remote computer, and accept it only if they match.
7. Run **Pipe test**, then **Diagnostics**.

## Daily use

Start **Corridoor SSH** or **Corridoor HopToDesk**. To disconnect, close PuTTY or
select **Disconnect**. In HopToDesk mode, only **Disconnect** closes the VPN; closing
the HopToDesk window does not.

## Configuration

### VPN

| Setting | Description |
|---|---|
| OpenVPN profile | path of the `.ovpn` file |
| Copy the profile | stores the profile, with certificates and keys embedded, in the encrypted store (default: on). Save again after changing the original. |
| VPN user / password | leave the user empty for certificate-only authentication |
| openvpn.exe | path of the OpenVPN executable |
| Remote host | IPv4 address of the target inside the VPN. A host name must resolve **without** the VPN. |

### SSH (PuTTY)

| Setting | Description |
|---|---|
| Use SSH | enables SSH in tests and diagnostics and permits SSH connections |
| SSH port / user / password | login data; the password may stay empty when a key is used |
| SSH key `.ppk` | optional private key. Only its path is stored. |
| putty.exe | path of PuTTY |

With a passphrase-protected key, the non-interactive login test fails.

### HopToDesk

| Setting | Description |
|---|---|
| Use HopToDesk | disabled by default |
| Direct IP port | default `21118` |
| hoptodesk.exe | path of HopToDesk |

On the remote machine, enable **Settings > Network > Direct IP access** and allow the
port in its firewall. The password is entered in HopToDesk. Passing an IP address to
`--connect` should be verified with your HopToDesk version.

### Test features: named pipes

By default, OpenVPN and PuTTY read the profile and passwords from short-lived
temporary files. A named pipe passes the same data from memory instead, so it is not
written to disk. Whether your program versions accept a pipe in place of a file has
to be tried on your machine.

| Setting | Replaces the temporary file of |
|---|---|
| VPN profile via pipe | the decrypted profile (only with **Copy the profile**) |
| VPN credentials via pipe | the VPN user name and password |
| SSH password via pipe | the password read by PuTTY and plink |

All three are off by default. Select **Pipe test** to set them:

1. The VPN is connected for real, with pipes for profile and credentials. If that
   fails, the test falls back step by step to temporary files.
2. Through the working tunnel, plink logs in with the password from a pipe. The key
   file is left out so that the password is actually used. This step needs SSH
   enabled, a user name and a password, and an already accepted host key.
3. Exactly the pipes that worked are switched on and saved. Untested ones stay off.

A rejected VPN login stops the test immediately to avoid locking the account. A
cancelled or aborted test changes nothing. There is no automatic fallback later: run
the test again after updating OpenVPN or PuTTY. HopToDesk receives no secrets from
the launcher.

## Testing and diagnostics

| Button | Behaviour |
|---|---|
| Save | stores the settings |
| Save & test | checks programs, profile, name resolution, VPN server, VPN connection, ping, client ports and SSH login; stops at the first failure |
| Diagnostics | runs all checks, including the route change, skips only dependent ones and prints a summary |
| Export log... | saves the log window to a text file |
| Pipe test | tries the named pipes and saves those that work |

A missing ping reply is only a warning, because ICMP is often blocked. A UDP VPN port
cannot be probed in advance; the handshake is the actual test. The HopToDesk check
covers port reachability, not authentication.

If SSH is enabled with a user name, **Diagnostics** also logs in to the target and
checks HopToDesk there (Linux only, nothing is changed): installed version, service
state and running processes. It fails if HopToDesk is missing or not running, and warns
if the package is older than 1.46.90. The version shown is the installed package;
after an upgrade, restart HopToDesk so that old processes are replaced.

## Security

All data is kept in `C:\Program Files\Corridoor\`, accessible only to
Administrators and SYSTEM.

| File | Content |
|---|---|
| `settings.bin` | settings and passwords, DPAPI-encrypted |
| `profile.bin` | profile copy, DPAPI-encrypted |
| `corridoor.log` | **unencrypted** log; the launcher does not write passwords to it |
| `tmp_*.txt` | temporary plain-text profile and passwords (not used for data sent through a pipe), deleted when the session ends |

Leftover temporary files from a killed session are removed at the next start.

**Considerations**

- Pipes accept local connections from Administrators and SYSTEM only, use random
  names, and send data only to the process the launcher started.
- Pipes keep secrets off the disk. They remain in plain text in the memory of the
  launcher and the client programs. A profile that is not copied stays your own file.
- DPAPI binds the data to the Windows account that saved it.
- Local storage cannot protect secrets from an attacker with administrator rights.
- PuTTY and HopToDesk **run elevated**, because the launcher starts them.
- Profiles containing `redirect-gateway`, `redirect-private`, `plugin` or `config`
  are rejected. User scripts are disabled (`--script-security 1`).
- Keep the client programs, the key file, a non-copied profile and the build folder
  in locations that untrusted users or programs cannot modify.
- Review exported logs before sharing them; they contain addresses and server names.

## Limitations

- Only one session at a time (SSH **or** HopToDesk).
- Only the target's IPv4 address is routed. Server-pushed routes and DNS settings
  are ignored.
- Encrypted VPN private keys and challenge-based two-factor authentication are not
  supported.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `file not found` | paths in the settings window |
| `AUTH_FAILED` | VPN user name and password |
| HopToDesk keeps reconnecting | HopToDesk version on both computers (1.46.90 or newer) |
| `getaddrinfo failed` for an IP address | no leading zeros: `10.1.0.5`, not `10.01.0.5` |
| `cannot stat file '\\.\pipe\...'` | warning only; it did not prevent the connection in testing |
| `TLS key negotiation failed` | server address, port, firewall, `tls-auth` / `tls-crypt` key |
| SSH port not reachable | SSH service, firewall, VPN routing |
| HopToDesk port not reachable | Direct IP access and firewall on the remote machine |
| `another session is already running` | close the other session |

## Project layout

| File | Responsibility |
|---|---|
| `corridoor.py` | entry point and mode selection |
| `corridoor_core.py` | storage, profile handling, connection and diagnostics |
| `corridoor_pipes.py` | one-way named pipes for secrets |
| `corridoor_gui.py` | Tkinter windows |
| `make_icons.py` | icon generation |
| `build.ps1` | Nuitka build and shortcut creation |
| `install.ps1` | installation into Program Files |
| `tests/` | session and GUI checks with stand-ins (see below) |
| `pyproject.toml` | Python version and build dependencies (Poetry) |
| `LICENSE` | MIT license |

## Development

The checks run on **Linux**: Windows APIs, `openvpn`, PuTTY and HopToDesk are replaced
by stand-ins, and the GUI check uses a stub instead of Tkinter. They do not replace a
test on a real Windows machine.

```bash
python3 tests/run_session_tests.py    # ends with: ALL TESTS PASSED
python3 tests/run_gui_smoke.py        # ends with: GUI code paths OK
```

## License

[MIT](LICENSE). OpenVPN, PuTTY and HopToDesk are separate programs with their own
licenses; they are not included.
