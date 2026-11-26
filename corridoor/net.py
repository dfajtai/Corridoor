"""Small network probes that do not need a session."""

import socket
import subprocess

from . import winapi


def ping(address: str) -> bool:
    """Send two ICMP echo requests with the Windows ping tool (IPv4).

    The exit code is not reliable ("destination unreachable" counts as a reply),
    therefore a ``TTL=`` field is required. It is the same in every language.
    """
    try:
        result = subprocess.run(
            [winapi.system32("ping.exe"), "-4", "-n", "2", "-w", "2000", address],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=15, creationflags=winapi.NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return False
    return "TTL=" in result.stdout.upper()


def source_address(target_ip: str, port: int) -> str:
    """Return the local address the system would use to reach the target.

    Connecting a UDP socket only consults the routing table, nothing is sent.
    A changed address after the VPN is up shows that the route uses the tunnel.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((target_ip, port))
        return probe.getsockname()[0]
