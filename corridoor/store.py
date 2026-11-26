"""The encrypted, administrator-only folder where Corridoor keeps its data.

Files in ``SECRET_DIR``:

* ``settings.bin``  DPAPI-encrypted settings including the passwords
* ``profile.bin``   optional DPAPI-encrypted copy of the .ovpn profile
* ``corridoor.log`` plain-text log (never contains passwords)
* ``tmp_*.txt``     short-lived plain-text secrets of a running session

The paths are module variables on purpose: the tests point them to a
temporary folder.
"""

import json
import logging
import os
import subprocess
from dataclasses import asdict, fields
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from . import winapi
from .errors import StepError
from .profile import Profile, bundle_profile, check_profile
from .settings import Settings

SECRET_DIR: Path = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Corridoor"
SETTINGS_FILE: Path = SECRET_DIR / "settings.bin"
PROFILE_FILE: Path = SECRET_DIR / "profile.bin"
LOG_FILE: Path = SECRET_DIR / "corridoor.log"

# Names the user can choose; "debug" prints nearly everything (never passwords).
LOG_LEVELS: dict[str, int] = {"info": logging.INFO, "debug": logging.DEBUG}

log: logging.Logger = logging.getLogger("corridoor")


def apply_log_level(name: str) -> None:
    """Set the detail of the log (window and file); unknown names mean ``info``.

    Takes effect at once, also for a session that is already running.
    """
    log.setLevel(LOG_LEVELS.get(str(name).lower(), logging.INFO))


def prepare_storage() -> None:
    """Create the admin-only folder and attach the file logger to it.

    Ownership goes to the Administrators group and inheritance is removed, so
    only Administrators and SYSTEM keep access.

    Raises:
        OSError, subprocess.CalledProcessError: If the folder cannot be secured.
    """
    SECRET_DIR.mkdir(exist_ok=True)
    log.debug("securing %s", SECRET_DIR)
    icacls = winapi.system32("icacls.exe")
    admins, system = "*S-1-5-32-544", "*S-1-5-18"  # language independent SIDs
    for options in (
        ["/setowner", admins],
        ["/inheritance:r", "/grant:r", f"{admins}:(OI)(CI)F", f"{system}:(OI)(CI)F"],
    ):
        subprocess.run([icacls, str(SECRET_DIR), *options], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=winapi.NO_WINDOW)

    handler = RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=2,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    apply_log_level("info")  # the saved level is applied after the settings load


def remove_temp_files() -> None:
    """Delete leftover ``tmp_*.txt`` files of a session that was killed."""
    for leftover in SECRET_DIR.glob("tmp_*.txt"):
        leftover.unlink(missing_ok=True)


def load_settings() -> Optional[Settings]:
    """Return the saved settings, or None if nothing has been saved yet.

    Options that a different version of the program has saved, and that this
    version does not know, are ignored.
    """
    if not SETTINGS_FILE.is_file():
        return None
    stored = json.loads(winapi.dpapi(SETTINGS_FILE.read_bytes(), encrypt=False))
    known = {field.name for field in fields(Settings)}
    settings = Settings(**{key: value for key, value in stored.items() if key in known})
    apply_log_level(settings.log_level)
    return settings


def save_settings(settings: Settings) -> None:
    """Encrypt and store the settings and, if requested, the profile copy.

    With ``copy_profile`` the profile is bundled (certificates embedded), checked
    and stored encrypted. If the original file is gone, an existing copy of the
    same profile is kept.

    Raises:
        StepError: If the profile cannot be imported.
        OSError: If writing or encryption fails.
    """
    if settings.copy_profile:
        source = Path(settings.profile_file)
        previous = load_settings()
        if source.is_file():
            bundle = bundle_profile(source)
            check_profile(bundle)
            PROFILE_FILE.write_bytes(winapi.dpapi(bundle.encode("utf-8"), encrypt=True))
            log.info("Profile copied into the encrypted store.")
        elif not (PROFILE_FILE.is_file() and previous
                  and previous.profile_file == settings.profile_file):
            raise StepError(f"profile not found: {source}")
    else:
        PROFILE_FILE.unlink(missing_ok=True)
    raw = json.dumps(asdict(settings)).encode("utf-8")
    SETTINGS_FILE.write_bytes(winapi.dpapi(raw, encrypt=True))
    apply_log_level(settings.log_level)
    log.info("Settings saved.")
    log.debug("Settings: %s", settings.masked())


def load_profile(settings: Settings) -> Profile:
    """Load the profile the settings point to and check it.

    Returns:
        The decrypted store copy, or the user's own file if it is not copied.

    Raises:
        StepError: If the profile contains forbidden directives.
        OSError: If the profile cannot be read.
    """
    path: Optional[Path] = None
    log.debug("loading the profile from %s",
              "the encrypted store" if settings.copy_profile else settings.profile_file)
    if settings.copy_profile:
        text = winapi.dpapi(PROFILE_FILE.read_bytes(), encrypt=False).decode("utf-8")
    else:
        path = Path(settings.profile_file).resolve()
        text = path.read_text("utf-8-sig", errors="replace")
    check_profile(text)
    return Profile(text, path)
