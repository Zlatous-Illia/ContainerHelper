"""Where the data and the settings live.

The program is built around a portable data folder: a `data` directory next
to the executable, holding the records, the backups and the settings. The
registry is not touched at all — otherwise the utility stops being portable,
and on another machine it silently forgets everything.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .i18n import tr

#: Name of the subfolder next to the executable. The folder root stays clean:
#: .bak and .tmp files do not get underfoot.
DATA_DIR_NAME = "data"

RECORDS_NAME = "Records.json"

#: Empty-volume measurements live in a file of their own. They describe not the
#: work with data but the machine: the Windows build and the VeraCrypt version.
#: Copy records travel with the user; measurements from another machine are
#: meaningless, and keeping both in one file would mean carrying someone
#: else's calibration around as your own.
CALIBRATION_NAME = "Calibration.json"

SETTINGS_NAME = "settings.ini"

#: Command-line switch. It goes into the shortcut and so needs no saved state
#: at all — which is exactly what makes it good where the program folder is not
#: writable.
DATA_ARGUMENT = "--data"

#: The only trace outside the portable data folder. It appears only after the
#: user has been told the folder is not writable and has chosen a place
#: themselves. The key is the path to the program folder, so that two copies
#: do not overwrite each other's choice.
LOCATION_ORGANISATION = "ContainerHelper"
LOCATION_NAME = "location.ini"


class DataDirError(Exception):
    """The data folder is unavailable and could not be chosen automatically."""


def program_dir() -> Path:
    """Program folder: beside the exe in a build, project root in sources."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    return program_dir() / DATA_DIR_NAME


def data_dir_from_arguments(argv: list[str] | None = None) -> Path | None:
    """Parse `--data <path>` or `--data=<path>`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for index, item in enumerate(argv):
        if item == DATA_ARGUMENT and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser()
        if item.startswith(DATA_ARGUMENT + "="):
            return Path(item.split("=", 1)[1]).expanduser()
    return None


def is_writable(directory: Path) -> bool:
    """Probe by writing, not by permissions: on network drives they lie."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _location_store() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / LOCATION_ORGANISATION / LOCATION_NAME


def remembered_data_dir() -> Path | None:
    """The folder chosen by hand for this copy of the program."""
    from PySide6.QtCore import QSettings

    store = _location_store()
    if not store.exists():
        return None
    settings = QSettings(str(store), QSettings.IniFormat)
    value = settings.value(_location_key(), "", type=str)
    return Path(value) if value else None


def remember_data_dir(directory: Path) -> None:
    from PySide6.QtCore import QSettings

    store = _location_store()
    store.parent.mkdir(parents=True, exist_ok=True)
    settings = QSettings(str(store), QSettings.IniFormat)
    settings.setValue(_location_key(), str(directory))
    settings.sync()


def _location_key() -> str:
    """Key by the program folder: two copies must not collide."""
    return str(program_dir()).replace("\\", "/").replace("/", "|")


def resolve_data_dir(argv: list[str] | None = None) -> tuple[Path | None, str]:
    """Find the data folder. Returns the path and where it came from.

    Order: the command-line argument, the folder next to the program, the
    choice remembered earlier. If nothing fits — None, and the caller must ask:
    the program must not quietly move off into some other folder.
    """
    chosen = data_dir_from_arguments(argv)
    if chosen is not None:
        if is_writable(chosen):
            return chosen, DATA_ARGUMENT
        raise DataDirError(
            tr("paths.argument.unwritable", argument=DATA_ARGUMENT, path=chosen)
        )

    beside = default_data_dir()
    if is_writable(beside):
        return beside, tr("paths.origin.beside")

    remembered = remembered_data_dir()
    if remembered is not None and is_writable(remembered):
        return remembered, tr("paths.origin.remembered")

    return None, tr("paths.origin.unwritable")


def records_path(data_dir: Path) -> Path:
    return data_dir / RECORDS_NAME


def calibration_path(data_dir: Path) -> Path:
    return data_dir / CALIBRATION_NAME


def settings_path(data_dir: Path) -> Path:
    return data_dir / SETTINGS_NAME
