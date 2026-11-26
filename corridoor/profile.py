"""Pure text handling of OpenVPN profiles (``.ovpn``): parse, check, bundle.

Nothing here talks to Windows or the network. The only file access is reading
the profile and the files it references in :func:`bundle_profile`.
"""

import base64
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .errors import StepError

# Would reroute all traffic, load a DLL or pull in an unchecked file.
FORBIDDEN_DIRECTIVES: tuple[str, ...] = (
    "redirect-gateway", "redirect-private", "plugin", "config")
# Directives whose file argument can be embedded as an inline <block>.
INLINE_DIRECTIVES: tuple[str, ...] = (
    "ca", "cert", "key", "extra-certs", "pkcs12", "tls-auth", "tls-crypt",
    "tls-crypt-v2", "crl-verify", "secret", "dh")


@dataclass
class Profile:
    """A checked OpenVPN profile, ready to be handed to ``openvpn.exe``."""

    text: str
    path: Optional[Path]  # the user's file; None for the copy from the store


def split_directive(line: str) -> list[str]:
    """Split a config line into words; comments and empty lines give ``[]``.

    A leading ``--`` of the directive name is removed, as OpenVPN allows it.
    """
    stripped = line.strip()
    if not stripped or stripped[0] in "#;":
        return []
    words = shlex.split(stripped, comments=True)
    if words:
        words[0] = words[0].removeprefix("--")
    return words


def iter_directives(text: str) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(line, words)`` for every line of a profile.

    The lines of inline ``<block>`` sections (certificates, keys) are passed
    through with an empty word list, so their content is never mistaken for
    directives. ``<connection>`` blocks hold real directives and are parsed.
    """
    closing: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        if closing is not None:
            if stripped == closing:
                closing = None
            yield line, []
        elif (stripped.startswith("<") and stripped.endswith(">")
              and not stripped.startswith("</") and stripped != "<connection>"):
            closing = f"</{stripped[1:]}"
            yield line, []
        else:
            yield line, split_directive(line)


def check_profile(text: str) -> None:
    """Reject directives that would break the split tunnel or run foreign code.

    Raises:
        StepError: Naming the offending line.
    """
    for line, words in iter_directives(text):
        if words and words[0] in FORBIDDEN_DIRECTIVES:
            raise StepError(f"remove this line from the profile: {line.strip()}")


def bundle_profile(path: Path) -> str:
    """Turn a profile and its referenced files into one self-contained text.

    Args:
        path: The original .ovpn file; relative references start at its folder.

    Returns:
        The profile with all certificate and key files embedded inline. An
        ``auth-user-pass`` file is dropped: the credentials come from the
        settings.

    Raises:
        StepError: If a referenced file is missing or cannot be embedded.
    """
    output: list[str] = []
    for line, words in iter_directives(path.read_text("utf-8-sig", errors="replace")):
        name = words[0] if words else ""
        if name == "askpass" and len(words) > 1:
            raise StepError("'askpass <file>' cannot be imported")
        if name == "auth-user-pass":
            output.append("auth-user-pass")  # credentials come from the settings
        elif name in INLINE_DIRECTIVES and len(words) > 1 and words[1] != "[inline]":
            referenced = path.parent / words[1]  # absolute paths stay unchanged
            if not referenced.is_file():
                raise StepError(f"file referenced by '{name}' not found: {referenced}")
            if name == "pkcs12":  # binary file, stored as base64
                content = base64.encodebytes(referenced.read_bytes()).decode("ascii")
            else:
                content = referenced.read_text("utf-8", errors="replace")
            if name == "tls-auth" and len(words) > 2:
                output.append(f"key-direction {words[2]}")
            output += [f"<{name}>", content.rstrip("\n"), f"</{name}>"]
        else:
            output.append(line)
    return "\n".join(output) + "\n"


def vpn_remotes(text: str) -> list[tuple[str, int, str]]:
    """Return the ``(host, port, protocol)`` entries of the VPN servers.

    ``port`` and ``proto`` lines are the defaults for ``remote`` lines that do
    not specify them. This is a diagnostic helper, not a full config parser.
    """
    directives = [words for _, words in iter_directives(text) if len(words) > 1]
    defaults = {"port": "1194", "proto": "udp"}
    for words in directives:
        key = "port" if words[0] == "rport" else words[0]
        if key in defaults:
            defaults[key] = words[1]
    remotes: list[tuple[str, int, str]] = []
    for words in directives:
        if words[0] != "remote":
            continue
        port = words[2] if len(words) > 2 else defaults["port"]
        protocol = words[3] if len(words) > 3 else defaults["proto"]
        if port.isdecimal():
            remotes.append((words[1], int(port),
                            "tcp" if protocol.startswith("tcp") else "udp"))
    return remotes
