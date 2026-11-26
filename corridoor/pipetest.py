"""The named pipe self-test.

Whether OpenVPN and PuTTY accept a pipe instead of a file depends on their
versions, so it is tried with real connections. The result tells the GUI which
``pipe_*`` settings are safe to switch on.
"""

import logging
import socket
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING, Optional

from . import store, winapi
from .errors import Cancelled, StepError

if TYPE_CHECKING:  # session imports this module lazily, so no import cycle
    from .session import Session

log: logging.Logger = logging.getLogger("corridoor")


def vpn_variants(copy_profile: bool, has_user: bool) -> list[tuple[bool, bool]]:
    """Return the ``(profile via pipe, credentials via pipe)`` combinations to try.

    The best variant (both pipes) comes first and the last one uses files only.
    Pipes that cannot apply (no copied profile, no VPN user) are dropped from
    every variant, and duplicates are removed.
    """
    variants: list[tuple[bool, bool]] = []
    for config, auth in ((True, True), (False, True), (True, False), (False, False)):
        variant = (config and copy_profile, auth and has_user)
        if variant not in variants:
            variants.append(variant)
    return variants


def run(session: "Session") -> Optional[dict[str, bool]]:
    """Try the named pipes with real connections.

    The VPN is connected with pipes first, falling back step by step to
    temporary files. Through the working tunnel an SSH login is tested with
    the password coming from a pipe. ``session.settings`` is restored at the end.

    Args:
        session: The session to run on.

    Returns:
        New values for the ``pipe_*`` settings, or None if the test could not
        be completed (nothing should be changed then).
    """
    original = session.settings
    result = {"pipe_vpn_config": False, "pipe_vpn_auth": False, "pipe_ssh": False}
    variants = vpn_variants(original.copy_profile, bool(original.vpn_user))
    test_ssh = bool(original.use_ssh and original.ssh_user and original.ssh_password)
    log.info("=== Pipe test started ===")
    try:
        with winapi.single_instance():
            session.check_programs(("ssh",) if test_ssh else ())
            profile = store.load_profile(original)
            target_ip = socket.gethostbyname(original.host)
            connected = False
            for config, auth in variants:
                session.settings = replace(original, pipe_vpn_config=config,
                                           pipe_vpn_auth=auth)
                log.info("--- VPN with profile via %s, credentials via %s",
                         "pipe" if config else "file", "pipe" if auth else "file")
                try:
                    with session.vpn_tunnel() as connect:
                        connect(profile, target_ip)
                        connected = True
                        result.update(pipe_vpn_config=config, pipe_vpn_auth=auth)
                        log.info("  VPN connected")
                        if test_ssh:
                            result["pipe_ssh"] = try_ssh_pipe(session, target_ip)
                        else:
                            log.info("  SSH pipe not tested (needs SSH, user "
                                     "and password)")
                except (StepError, OSError) as error:
                    if connected:
                        raise
                    log.warning("  did not work: %s", error)
                    if session.auth_failed:  # retrying could lock the account
                        raise StepError("VPN login rejected, test stopped")
                finally:
                    session.cleanup()
                if connected:
                    break
            if not connected:
                raise StepError("the VPN did not connect in any variant")
    except Cancelled:
        log.info("Stopped by the user, settings unchanged.")
        return None
    except Exception as error:
        log.error("Pipe test aborted: %s", error)
        return None
    finally:
        session.cleanup()
        session.settings = original
    log.info("=== Pipe test result ===")
    for name, works in result.items():
        log.info("  %-16s %s", name, "pipe" if works else "file (not enabled)")
    return result


def try_ssh_pipe(session: "Session", target_ip: str) -> bool:
    """Log in with the password from a pipe; True if that really worked.

    The key is left out so that only the password can make the login succeed.
    The pipe must also have delivered its data, not just any login happened.
    """
    log.info("--- SSH login with the password via pipe")
    session.settings = replace(session.settings, pipe_ssh=True, ssh_key="")
    count = len(session.pipes)
    try:
        session.check_login(target_ip)
    except (StepError, OSError, subprocess.TimeoutExpired) as error:
        log.warning("  did not work: %s", error)
        return False
    works = all(pipe.delivered for pipe in session.pipes[count:])
    log.info("  %s", "login OK" if works else "the pipe was not read")
    return works
