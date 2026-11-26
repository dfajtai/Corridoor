# Configuration, tests and diagnostics

## VPN

| Setting | Description |
|---|---|
| OpenVPN profile | path of the `.ovpn` file |
| Copy the profile | stores the profile, with certificates and keys embedded, in the encrypted store (default: on). Save again after changing the original. |
| VPN user / password | leave the user empty for certificate-only authentication |
| openvpn.exe | path of the OpenVPN executable |
| Remote host | IPv4 address of the target inside the VPN. A host name must resolve **without** the VPN. |

## SSH (PuTTY)

| Setting | Description |
|---|---|
| Use SSH | enables SSH in tests and diagnostics and permits SSH connections |
| SSH port / user / password | login data; the password may stay empty when a key is used |
| SSH key `.ppk` | optional private key. Only its path is stored. |
| putty.exe | path of PuTTY (`plink.exe` must be in the same folder) |

With a passphrase-protected key, the non-interactive login test fails.

## HopToDesk

| Setting | Description |
|---|---|
| Use HopToDesk | disabled by default |
| Direct IP port | default `21118` |
| hoptodesk.exe | path of HopToDesk |

On the remote machine, enable **Settings > Network > Direct IP access** and allow the
port in its firewall. The password is entered in HopToDesk. Passing an IP address to
`--connect` should be verified with your HopToDesk version.

## Test features: named pipes

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
   enabled, a user name, a password and an already accepted host key.
3. Exactly the pipes that worked are switched on and saved. Untested ones stay off.

A rejected VPN login stops the test immediately to avoid locking the account. A
cancelled or aborted test changes nothing. There is no automatic fallback later: run
the test again after updating OpenVPN or PuTTY. HopToDesk receives no secrets from
the launcher.

## Logging

| Setting | Description |
|---|---|
| Log detail | `info` (default): steps, results, warnings and the OpenVPN output. `debug`: nearly everything. |

`debug` adds, marked `[debug]` in the window: the settings (passwords shown as `***`),
Python and Windows versions, every command line that is started (OpenVPN, PuTTY,
plink, HopToDesk), process ids, job object and mutex events, pipe connections, ping
output, routing lookups, the SSH banner, the answer of the remote HopToDesk probe,
the commands to the session list, and the cleanup. Secrets are logged only as a
length and the path of their file or pipe, never as text.

The level applies as soon as you select **Save**, also to a session that is already
running. The file `corridoor.log` gets the same detail. Switch back to `info` when
the problem is solved: the debug log is long, and it contains paths, addresses and
user names (see [Security](security.md)).

## Buttons

| Button | Behaviour |
|---|---|
| Save | stores the settings |
| Save & test | checks programs, profile, name resolution, VPN server, VPN connection, ping, client ports and SSH login; stops at the first failure |
| Diagnostics | runs all checks, including the route change, skips only dependent ones and prints a summary |
| Export log... | saves the log window to a text file |
| Pipe test | tries the named pipes and saves those that work |

![Diagnostics](images/diagnostics.png)

A missing ping reply is only a warning, because ICMP is often blocked. A UDP VPN port
cannot be probed in advance; the handshake is the actual test. The HopToDesk check
covers port reachability, not authentication.

If SSH is enabled with a user name, **Diagnostics** also logs in to the target and
checks HopToDesk there (Linux only, nothing is changed): installed version, service
state and running processes. It fails if HopToDesk is missing or not running, and warns
if the package is older than 1.46.90. The version shown is the installed package;
after an upgrade, restart HopToDesk so that old processes are replaced.
