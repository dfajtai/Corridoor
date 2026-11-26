"""HopToDesk facts: default port, minimum version and the remote probe."""

import re

# Default port of the "Direct IP access" option of HopToDesk.
DEFAULT_PORT: str = "21118"

# Oldest version seen working; 1.44.4 on the remote side kept reconnecting.
MIN_VERSION: str = "1.46.90"

# Read-only shell command for a Linux remote host; prints "key=value" lines.
# Only single quotes are used, so the text survives Windows argument quoting.
PROBE_COMMAND: str = (
    r"dpkg-query -W -f='package=${db:Status-Status} ${Version}\n' hoptodesk 2>/dev/null; "
    r"rpm -q --qf 'package=installed %{VERSION}\n' hoptodesk 2>/dev/null; "
    "echo command=$(command -v hoptodesk); "
    "echo service=$(systemctl is-active hoptodesk 2>/dev/null); "
    "echo processes=$(ps -e -o comm= | grep -ci '^hoptodesk')"
)


def parse_probe(output: str) -> dict[str, str]:
    """Turn the ``key=value`` lines printed by :data:`PROBE_COMMAND` into a dict.

    Lines without ``=`` (login banners, noise) are ignored. The keys are
    ``package``, ``command``, ``service`` and ``processes``.
    """
    return dict(line.strip().split("=", 1)
                for line in output.splitlines() if "=" in line)


def version_numbers(text: str) -> list[int]:
    """Return the first three numbers of a version string, comparable as lists.

    ``"1.46.90-2ubuntu1"`` gives ``[1, 46, 90]``.
    """
    return [int(part) for part in re.findall(r"\d+", text)[:3]]
