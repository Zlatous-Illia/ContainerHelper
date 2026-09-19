"""Administrator rights: detect them and ask for them again.

They are needed because of one switch: the documentation of
`/filesystem NTFS` says that "a UAC prompt will be displayed unless the process
is run with full administrative privileges". On twenty-two containers that is
twenty-two UAC prompts in a row, and the collection stops being automatic.

Rights cannot be dropped again — a process that has them keeps them to the
end, so the restart is offered, not done on its own, and the window after it
must say so out loud.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .paths import DATA_ARGUMENT, program_dir
from .sizes import IS_WINDOWS

#: The ShellExecuteW verb that raises the UAC prompt.
RUNAS = "runas"

#: Anything ShellExecuteW returns that is not greater than this is an error.
#: That is how the function's own documentation puts it: success returns a
#: "pseudo-HINSTANCE" greater than 32.
SHELL_EXECUTE_OK = 32


def is_admin() -> bool:
    """Whether we run with full administrator rights."""
    if not IS_WINDOWS:
        return False
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


def relaunch_arguments(data_dir: Path | None) -> tuple[str, list[str]]:
    """What to relaunch: the exe itself in a build, python -m from source.

    The data folder is passed as an explicit `--data`: the elevated process
    has a different environment, and relying on it to find the same folder by
    itself is not an option — portability would end right there.
    """
    arguments: list[str] = []
    if not getattr(sys, "frozen", False):
        arguments += ["-m", "containerhelper"]
    if data_dir is not None:
        arguments += [DATA_ARGUMENT, str(data_dir)]
    return sys.executable, arguments


def quote(arguments: list[str]) -> str:
    """Join the arguments into one string: ShellExecuteW accepts only that."""
    return " ".join(
        f'"{item}"' if " " in item or not item else item for item in arguments
    )


def relaunch_as_admin(data_dir: Path | None = None) -> bool:
    """Request a restart with administrator rights.

    True means the request was accepted and the new copy is starting up; the
    caller must close the current one, otherwise two copies will write to the
    same data folder.
    """
    if not IS_WINDOWS:
        return False
    import ctypes

    executable, arguments = relaunch_arguments(data_dir)
    try:
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            RUNAS,
            executable,
            quote(arguments),
            str(program_dir()),
            1,  # SW_SHOWNORMAL
        )
    except (OSError, AttributeError):
        return False
    return int(result) > SHELL_EXECUTE_OK
