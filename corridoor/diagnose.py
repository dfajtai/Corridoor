"""The full diagnostics run: every check, a summary at the end.

Unlike :meth:`Session.run`, a failed check does not abort the run. Only the
checks that depend on the failed one are skipped. Dependencies are expressed
as names of values that an earlier check has to produce.
"""

import logging
import socket
import subprocess
from typing import TYPE_CHECKING, Callable

from . import net, store, winapi
from .errors import Cancelled, StepError

if TYPE_CHECKING:  # session imports this module lazily, so no import cycle
    from .session import Session

log: logging.Logger = logging.getLogger("corridoor")

# The routing table lookup needs some port; nothing is sent to it.
ROUTE_PROBE_PORT: int = 9


def run(session: "Session") -> bool:
    """Run every possible check and finish with a summary.

    Args:
        session: The session whose settings and helpers are used.

    Returns:
        True if no check failed (warnings and skipped checks do not count).
    """
    settings = session.settings
    results: list[tuple[str, str]] = []  # (title, outcome)
    values: dict[str, object] = {}       # data passed from check to check

    def check(title: str, action: Callable[[], None],
              needs: tuple[str, ...] = ()) -> bool:
        """Run one check unless required data is missing; record the outcome."""
        if session.stop.is_set():
            raise Cancelled
        if any(key not in values for key in needs):
            log.info("--- %s: SKIPPED (an earlier check failed)", title)
            results.append((title, "SKIPPED"))
            return False
        log.info("--- %s", title)
        session.warned = False
        try:
            action()
        except Cancelled:
            raise
        except Exception as error:
            log.error("  FAILED: %s", error)
            results.append((title, "FAILED"))
            return False
        results.append((title, "WARNING" if session.warned else "OK"))
        return True

    # --- the individual checks; each stores what later checks need ---

    def openvpn_version() -> None:
        output = subprocess.run(
            [settings.openvpn_exe, "--version"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", timeout=10,
            creationflags=winapi.NO_WINDOW,
        ).stdout.strip()
        if not output:
            raise StepError("openvpn did not report a version")
        log.info("  %s", output.splitlines()[0])

    def profile() -> None:
        values["profile"] = store.load_profile(settings)

    def resolve() -> None:
        values["target"] = socket.gethostbyname(settings.host)
        log.info("  %s -> %s", settings.host, values["target"])

    def route_before() -> None:
        values["source"] = net.source_address(values["target"], ROUTE_PROBE_PORT)
        log.info("  local address without VPN: %s", values["source"])

    def route_after() -> None:
        source = net.source_address(values["target"], ROUTE_PROBE_PORT)
        log.info("  local address with VPN: %s", source)
        if source == values.get("source"):
            session.warn("the route did not change, the traffic may bypass the VPN")

    def login() -> None:
        session.check_login(values["target"])
        values["login"] = True

    log.info("=== Diagnostics started ===")
    cancelled = False
    try:
        with winapi.single_instance():
            store.remove_temp_files()
            clients = session.enabled_clients()
            check("Programs", lambda: session.check_programs(clients))
            check("OpenVPN version", openvpn_version)
            check("OpenVPN profile", profile)
            check("Target name", resolve)
            check("VPN server reachability",
                  lambda: session.check_vpn_server(values["profile"]), ("profile",))
            # Informational, a failure here does not block anything.
            check("Route without VPN", route_before, ("target",))
            with session.vpn_tunnel() as connect:
                def tunnel() -> None:
                    connect(values["profile"], values["target"])
                    values["vpn"] = True

                check("VPN connection", tunnel, ("profile", "target"))
                check("Route through VPN", route_after, ("vpn",))
                check("Ping through VPN",
                      lambda: session.ping_target(values["target"]), ("vpn",))
                if "ssh" in clients:
                    check("SSH port",
                          lambda: session.check_ssh_port(values["target"]), ("vpn",))
                    if settings.ssh_user:
                        check("SSH login", login, ("vpn",))
                if "hoptodesk" in clients:
                    check("HopToDesk port",
                          lambda: session.check_hoptodesk_port(values["target"]),
                          ("vpn",))
                    if "ssh" in clients and settings.ssh_user:
                        check("Remote HopToDesk (via SSH)",
                              lambda: session.check_remote_hoptodesk(values["target"]),
                              ("login",))
                    else:
                        log.info("--- Remote HopToDesk: not checked, it needs "
                                 "SSH to be enabled with a user name")
    except Cancelled:
        cancelled = True
        log.info("Stopped by the user.")
    except Exception as error:
        log.error("Diagnostics aborted: %s", error)
        return False
    finally:
        session.cleanup()

    log.info("=== Summary%s ===", " (incomplete)" if cancelled else "")
    for title, outcome in results:
        log.info("  %-8s %s", outcome, title)
    return all(outcome != "FAILED" for _, outcome in results)
