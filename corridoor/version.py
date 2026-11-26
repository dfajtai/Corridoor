"""The program version: ``<base>.<commit id>``, for example ``0.2.ab12cd3``.

* The *base* (``0.2``) is the first line of the ``VERSION`` file. It changes only
  when the maintainer asks for a new version.
* The *commit id* is added by the git post-commit hook (``githooks/post-commit``)
  and by ``build.ps1``, which write ``corridoor/_version.py``. That file is
  generated and not tracked by git, because a commit cannot contain its own id.

Without the generated file (a fresh clone without the hook) the version is
``<base>.dev``, so the program still starts.
"""

from pathlib import Path

NAME: str = "Corridoor"


def base_version() -> str:
    """Return the base from the ``VERSION`` file, or ``0.0`` if it is missing."""
    path = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        return path.read_text("utf-8").splitlines()[0].strip() or "0.0"
    except (OSError, IndexError):
        return "0.0"


def get_version() -> str:
    """Return the full version, such as ``0.2.ab12cd3``."""
    try:
        from ._version import __version__
        return __version__
    except ImportError:
        return f"{base_version()}.dev"


def title(suffix: str = "") -> str:
    """Return the window title: ``Corridoor 0.2.ab12cd3`` plus an optional suffix."""
    text = f"{NAME} {get_version()}"
    return f"{text} - {suffix}" if suffix else text
