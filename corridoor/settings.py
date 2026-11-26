"""The user's settings as one plain dataclass."""

from dataclasses import asdict, dataclass

from .hoptodesk import DEFAULT_PORT


@dataclass
class Settings:
    """Everything the user can configure; saved encrypted by :mod:`store`.

    All values are plain strings and booleans, so the dataclass can be turned
    into JSON and back. Ports are kept as text exactly as typed in the form.
    """

    # --- VPN ---
    profile_file: str = ""
    copy_profile: bool = True     # keep an encrypted copy of the profile
    vpn_user: str = ""            # empty: certificate-only authentication
    vpn_password: str = ""
    host: str = ""                # the only host that is routed through the VPN
    openvpn_exe: str = r"C:\Program Files\OpenVPN\bin\openvpn.exe"

    # --- SSH (PuTTY) ---
    use_ssh: bool = True
    port: str = "22"
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_key: str = ""             # optional .ppk; only the path is stored
    putty_exe: str = r"C:\Program Files\PuTTY\putty.exe"

    # --- HopToDesk ---
    use_hoptodesk: bool = False
    hoptodesk_port: str = DEFAULT_PORT
    hoptodesk_exe: str = r"C:\Program Files\HopToDesk\hoptodesk.exe"

    # --- File transfer (FileZilla over SFTP, through the SSH port) ---
    use_filezilla: bool = False
    filezilla_exe: str = r"C:\Program Files\FileZilla FTP Client\filezilla.exe"

    # --- Test features: named pipes instead of temporary files ---
    pipe_vpn_config: bool = False  # only used together with copy_profile
    pipe_vpn_auth: bool = False
    pipe_ssh: bool = False

    # --- Logging ---
    log_level: str = "info"        # "info" (default) or "debug" (nearly everything)

    def masked(self) -> dict:
        """Return the settings as a dict that is safe to write into a log.

        Passwords are replaced by ``***`` (or empty if not set). Nothing else is
        secret: paths, user names and ports are logged as they are.
        """
        values = asdict(self)
        for name in ("vpn_password", "ssh_password"):
            values[name] = "***" if values[name] else ""
        return values
