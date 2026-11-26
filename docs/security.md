# Security

All data is kept in `C:\Program Files\Corridoor\`, accessible only to
Administrators and SYSTEM.

| File | Content |
|---|---|
| `settings.bin` | settings and passwords, DPAPI-encrypted |
| `profile.bin` | profile copy, DPAPI-encrypted |
| `corridoor.log` | **unencrypted** log; the launcher does not write passwords to it |
| `tmp_*.txt` | temporary plain-text profile and passwords (not used for data sent through a pipe), deleted when the session ends |

Leftover temporary files from a killed session are removed at the next start.

## Considerations

- Pipes accept local connections from Administrators and SYSTEM only (plus your own
  user for PuTTY, see below), use random names, and send data only to the process the
  launcher started.
- Pipes keep secrets off the disk. They remain in plain text in the memory of the
  launcher and the client programs. A profile that is not copied stays your own file.
- DPAPI binds the data to the Windows account that saved it.
- Local storage cannot protect secrets from an attacker with administrator rights.
- Only the launcher and OpenVPN run elevated. PuTTY, HopToDesk and FileZilla are
  **started without administrator rights** (setting *Start PuTTY, HopToDesk and
  FileZilla without administrator rights*, on by default): as the same user, with the
  limited token that Windows keeps next to the elevated one, or, if there is none, a
  restricted copy of the token (Administrators group switched off, no privileges,
  medium integrity level). If that fails, the program is **not** started elevated
  instead; the error says how to switch the setting off.
- For such a PuTTY window the password file (or pipe) is also readable by your own
  user, and only by it, because a program without admin rights could not read it
  otherwise. The file is deleted after a few seconds. Another program running as you
  could read it in that moment; that is the price of a PuTTY that is not elevated.
  Every PuTTY window gets its own password secret, released when that window closes.
- A program started without admin rights belongs to you, not to an administrator: a
  flaw in it can no longer change the system, the routes or the VPN.
- FileZilla is started without a password (`-l interactive`); it asks for it itself,
  so the password never appears on a command line.
- OpenVPN and all PuTTY windows are in a job object that Windows ends if the
  launcher crashes.
- Profiles containing `redirect-gateway`, `redirect-private`, `plugin` or `config`
  are rejected. User scripts are disabled (`--script-security 1`).
- Keep the client programs, the key file, a non-copied profile and the build folder
  in locations that untrusted users or programs cannot modify.
- Review exported logs before sharing them; they contain addresses and server names.
  The `debug` level also lists command lines, file paths and user names. Passwords
  are never written to the log, at any level.

## Limitations

- One VPN connection and one target host at a time; on it up to 4 SSH sessions (setting, at most 10) and
  one HopToDesk session and one FileZilla session.
- Only the target's IPv4 address is routed. Server-pushed routes and DNS settings
  are ignored.
- Encrypted VPN private keys and challenge-based two-factor authentication are not
  supported.
