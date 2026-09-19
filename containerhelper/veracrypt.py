"""Finding VeraCrypt and three command-line operations on a container.

No Qt: everything here is tested without the interface. Process launching and
volume reading are replaced through fields, because a real check needs an
installed VeraCrypt, administrator rights and several minutes per container —
there is no room for that in the test suite.

Checked against the VeraCrypt 1.26.24 documentation (`docs/html/en/Command Line
Usage.html` in the distribution). Hence the oddities:

- creating and mounting are **different binaries**;
- `VeraCrypt Format.exe` has no `/pim` at all, it exists only for mounting,
  so creation cannot be sped up with a reduced iteration count;
- `/nosizecheck` is mandatory, otherwise a terabyte dynamic container refuses
  to be created where there is no terabyte of free space;
- `/dismount` is deprecated, `/unmount` is needed;
- `/hash sha512` noticeably speeds up mounting: without it VeraCrypt tries
  every PRF in turn;
- `/silent` is described as "If there is any error, the operation will fail
  silently", so nothing here relies on the exit code: the result is checked by
  the facts — whether a file of the right size appeared, whether the volume
  came up.
"""

from __future__ import annotations

import os
import string
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .formatting import plural
from .sizes import (
    IS_WINDOWS,
    cluster_size,
    mounted_drives,
    volume_filesystem,
    volume_root,
    volume_usage,
)

#: Folder name inside Program Files. The same for 32- and 64-bit installs.
INSTALL_SUBDIR = "VeraCrypt"

#: Environment variables that give both Program Files. ProgramW6432 is added
#: for 32-bit Python on 64-bit Windows: there ProgramFiles points into
#: "(x86)", and the real install would not be found otherwise.
PROGRAM_FILES_VARIABLES = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")

#: Fallback paths for an empty environment. The standard install locations are
#: named explicitly: the variables may be missing, and VeraCrypt is still
#: there.
FALLBACK_DIRS = (
    r"C:\Program Files\VeraCrypt",
    r"C:\Program Files (x86)\VeraCrypt",
)

#: Binary names. An installed build has no suffix, a portable one has an
#: architecture suffix. The order here is the order of preference.
FORMAT_NAMES = (
    "VeraCrypt Format.exe",
    "VeraCrypt Format-x64.exe",
    "VeraCrypt Format-arm64.exe",
)
MOUNT_NAMES = (
    "VeraCrypt.exe",
    "VeraCrypt-x64.exe",
    "VeraCrypt-arm64.exe",
)

#: Container parameters. There is no reason to change them: the measurement
#: must repeat how containers are created by hand, not look for an optimum.
HASH = "sha512"
ENCRYPTION = "AES"
FILESYSTEM = "NTFS"

#: Versions in which the switches we need changed. Taken from the Release
#: Notes in the VeraCrypt 1.26.24 distribution.
#:
#: 1.24 added `/nosizecheck` and `/quick` (before it, quick format could not
#: be turned off from the command line by a separate switch). Without them the
#: collection does not run at all: a terabyte container hits the free-space
#: check.
MINIMUM_VERSION = (1, 24)

#: 1.25.4: "Avoid displaying waiting dialog when /silent specified ... during
#: creating of file container ... and a filesystem other than FAT". Before it
#: the waiting window pops up for every container anyway.
QUIET_CREATE_SINCE = (1, 25, 4)

#: 1.26.20 renamed Dismount to Unmount. Versions before it do not understand
#: `/unmount`, while `/dismount` is understood by every version up to and
#: including today's — it is declared deprecated, but the 1.26.24
#: documentation still describes it and it works.
UNMOUNT_SINCE = (1, 26, 20)

#: Password of the temporary container. The container lives for minutes and
#: is deleted right after the measurement, but on a short password VeraCrypt
#: shows a warning, and a warning in silent mode is a silent refusal.
CALIBRATION_PASSWORD = "ContainerHelperCalibration2026"

#: How long to wait for the process. Creating a terabyte dynamic container
#: takes seconds, a normal one with full format takes minutes. The ceiling is
#: not about the norm, it is about a hung process not hanging forever.
CREATE_TIMEOUT = 3600
MOUNT_TIMEOUT = 300

#: The letter does not appear the instant the process exits: the driver
#: brings the volume up. So the list of mounted drives is polled, not the exit
#: code.
LETTER_TIMEOUT = 60.0
POLL_SECONDS = 0.5

#: Unmounting is repeated, not waited out for one minute. Right after data is
#: written VeraCrypt refuses to unmount the volume — with exit code 1 and
#: immediately — and half a minute later gives it up without objection. On the
#: first real copy-slack collection run four measurements of seven failed this
#: way, and only the retry in cleanup saved them: it happened a minute later
#: and went through. Here the same thing is done on purpose.
#:
#: Waiting a minute after a refusal is pointless: the volume is not "slowly
#: unmounting", it was not given up at all. Hence a short grace period for the
#: driver, then a pause and a new command.
UNMOUNT_ATTEMPTS = 4
UNMOUNT_GRACE = 5.0
UNMOUNT_RETRY_SECONDS = 15.0

#: What the temporary containers are called. The name is not for looks:
#: cleanup finds orphaned files by it — a terabyte file that survived a crash
#: would otherwise lie in the folder silently and forever.
CONTAINER_PREFIX = "containerhelper-calibration-"
CONTAINER_SUFFIX = ".hc"

#: Letters searched for a free one. A and B are taken by history, C by the
#: system; top down, so as not to take the ones the system hands out next.
LETTERS = tuple(reversed(string.ascii_uppercase[3:]))


class VeraCryptError(Exception):
    """The operation failed. The text is fit to show to the user."""


@dataclass(frozen=True)
class Install:
    """A found install: the folder and both binaries."""

    directory: Path
    format_exe: Path
    mount_exe: Path
    version: str = ""

    @property
    def title(self) -> str:
        if self.version:
            return f"{self.directory} (версия {self.version})"
        return str(self.directory)

    @property
    def number(self) -> tuple[int, ...]:
        """The version as numbers for comparison.

        Empty means it could not be read.
        """
        return parse_version(self.version)

    @property
    def knows_unmount(self) -> bool:
        """Whether this version understands `/unmount`.

        An unreadable version counts as old: `/dismount` works on new ones too,
        while `/unmount` does not work on old ones. Erring this way is cheaper.
        """
        return self.number >= UNMOUNT_SINCE


def standard_dirs() -> list[Path]:
    """Standard install locations, no repeats, in order of preference."""
    candidates: list[Path] = []
    for variable in PROGRAM_FILES_VARIABLES:
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / INSTALL_SUBDIR)
    candidates.extend(Path(item) for item in FALLBACK_DIRS)

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def install_at(directory: str | os.PathLike[str]) -> Install | None:
    """Build an install from a folder.

    The exe itself may be given too: its folder is taken.
    """
    path = Path(directory)
    if path.is_file():
        path = path.parent
    format_exe = _first_existing(path, FORMAT_NAMES)
    mount_exe = _first_existing(path, MOUNT_NAMES)
    if format_exe is None or mount_exe is None:
        return None
    return Install(path, format_exe, mount_exe, file_version(mount_exe))


def find_install(extra: Iterable[str | os.PathLike[str]] = ()) -> Install | None:
    """Find VeraCrypt: first what was set by hand, then standard locations."""
    for directory in [*extra, *standard_dirs()]:
        if not directory:
            continue
        found = install_at(directory)
        if found is not None:
            return found
    return None


def missing_report(directory: str | os.PathLike[str]) -> str:
    """What is missing in the given folder. Empty means everything is there."""
    path = Path(directory)
    if path.is_file():
        path = path.parent
    if not path.is_dir():
        return f"Папки {path} нет."

    lacking = [
        title
        for title, names in (
            ("создания контейнеров", FORMAT_NAMES),
            ("монтирования", MOUNT_NAMES),
        )
        if _first_existing(path, names) is None
    ]
    if not lacking:
        return ""
    return (
        f"В папке {path} нет бинарника для {' и '.join(lacking)}. "
        f"Нужны «{FORMAT_NAMES[0]}» и «{MOUNT_NAMES[0]}»; у портативной "
        f"сборки те же имена с суффиксом архитектуры."
    )


def _first_existing(directory: Path, names: Sequence[str]) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def file_version(path: Path) -> str:
    """The version from the exe resources. Empty means it was unreadable.

    The version is needed not by the calculation but by a person: the size of
    the metadata is decided not by NTFS in general but by the specific
    formatting code, and that is worth seeing in the report.
    """
    if not IS_WINDOWS:
        return ""

    import ctypes
    from ctypes import wintypes

    class _FixedFileInfo(ctypes.Structure):
        _fields_ = [
            ("dwSignature", wintypes.DWORD),
            ("dwStrucVersion", wintypes.DWORD),
            ("dwFileVersionMS", wintypes.DWORD),
            ("dwFileVersionLS", wintypes.DWORD),
            ("dwProductVersionMS", wintypes.DWORD),
            ("dwProductVersionLS", wintypes.DWORD),
            ("dwFileFlagsMask", wintypes.DWORD),
            ("dwFileFlags", wintypes.DWORD),
            ("dwFileOS", wintypes.DWORD),
            ("dwFileType", wintypes.DWORD),
            ("dwFileSubtype", wintypes.DWORD),
            ("dwFileDateMS", wintypes.DWORD),
            ("dwFileDateLS", wintypes.DWORD),
        ]

    try:
        version_dll = ctypes.windll.version
        name = ctypes.c_wchar_p(str(path))
        size = version_dll.GetFileVersionInfoSizeW(name, None)
        if not size:
            return ""
        buffer = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(name, 0, size, buffer):
            return ""
        block = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version_dll.VerQueryValueW(
            buffer, ctypes.c_wchar_p("\\"), ctypes.byref(block), ctypes.byref(length)
        ):
            return ""
        info = ctypes.cast(block, ctypes.POINTER(_FixedFileInfo)).contents
    except (OSError, AttributeError, ValueError):
        return ""

    parts = [
        info.dwFileVersionMS >> 16,
        info.dwFileVersionMS & 0xFFFF,
        info.dwFileVersionLS >> 16,
        info.dwFileVersionLS & 0xFFFF,
    ]
    # VeraCrypt's fourth number is always zero and never comes up.
    while len(parts) > 3 and parts[-1] == 0:
        parts.pop()
    return ".".join(str(part) for part in parts)


# --- command lines ---------------------------------------------------------
#
# Split out as separate functions because they are exactly what has to be
# tested: a real launch is out of the test suite's reach, and a mistake in one
# switch costs several hours of work and a terabyte file in someone else's
# folder.


def create_command(
    install: Install,
    path: Path,
    size_bytes: int,
    password: str = CALIBRATION_PASSWORD,
    dynamic: bool = True,
    quick: bool = True,
) -> list[str]:
    """Creating a container. The size is in exact bytes, not with a suffix.

    The `G` suffix rounds, and `container_mib × 1048576` has to be hit to the
    byte: otherwise the measurement lands outside its row in the coverage
    table.
    """
    command = [
        str(install.format_exe),
        "/create",
        str(path),
        "/size",
        str(size_bytes),
        "/password",
        password,
        "/hash",
        HASH,
        "/encryption",
        ENCRYPTION,
        "/filesystem",
        FILESYSTEM,
    ]
    if dynamic:
        command.append("/dynamic")
    if quick:
        command.append("/quick")
    command.extend(["/nosizecheck", "/force", "/silent"])
    return command


def mount_command(
    install: Install,
    path: Path,
    letter: str,
    password: str = CALIBRATION_PASSWORD,
) -> list[str]:
    """Mounting. /hash spares VeraCrypt from trying every PRF in turn."""
    return [
        str(install.mount_exe),
        "/quit",
        "/silent",
        "/volume",
        str(path),
        "/letter",
        letter,
        "/password",
        password,
        "/hash",
        HASH,
    ]


def unmount_command(
    install: Install, letter: str, force: bool = False
) -> list[str]:
    """Unmounting with the switch this version understands.

    Since 1.26.20 it is `/unmount`, before it `/dismount`. The switches are not
    synonyms across time: an old VeraCrypt complains about `/unmount`, and with
    `/silent` it does so silently, and the volume stays mounted along with the
    container file.

    `/force` is for cleanup only. It takes the volume down even when files on
    it are in use, which means unflushed cache contents may be lost. Before
    measuring left space this must never be done: a lost write looks like
    extra free space, and the measured slack comes out **underestimated** —
    that is, the error moves to the only dangerous side. In cleanup there is
    nothing to lose: the container is deleted right away.
    """
    switch = "/unmount" if install.knows_unmount else "/dismount"
    command = [str(install.mount_exe), "/quit", "/silent", switch, letter]
    if force:
        command.append("/force")
    return command


def parse_version(text: str) -> tuple[int, ...]:
    """`1.26.24` → (1, 26, 24). A non-numeric tail is dropped."""
    parts: list[int] = []
    for chunk in text.split("."):
        digits = ""
        for symbol in chunk:
            if not symbol.isdigit():
                break
            digits += symbol
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def version_notice(install: Install) -> tuple[bool, str]:
    """Whether this version is fit for collection and what to say about it.

    The first value is whether it is OK to start. The second is the text for
    the window; empty when the version is recent and there is nothing to say.
    """
    number = install.number
    if not number:
        return True, (
            "Версию VeraCrypt прочитать не удалось. Том будем снимать старым "
            "ключом /dismount — его понимают все версии."
        )
    if number < MINIMUM_VERSION:
        return False, (
            f"VeraCrypt {install.version} слишком старая: ключи /nosizecheck и "
            f"/quick появились в 1.24. Без первого контейнер на терабайт "
            f"откажется создаваться, если терабайта свободного нет. "
            f"Нужна 1.24 или новее."
        )
    if number < QUIET_CREATE_SINCE:
        return True, (
            f"VeraCrypt {install.version}: до 1.25.4 ключ /silent не убирал "
            f"окно ожидания при создании NTFS-контейнера. Сбор пойдёт, но "
            f"окно будет выскакивать на каждый контейнер."
        )
    if number < UNMOUNT_SINCE:
        return True, (
            f"VeraCrypt {install.version}: снимать том будем ключом /dismount "
            f"— /unmount появился только в 1.26.20."
        )
    return True, ""


def run_command(command: Sequence[str], timeout: int) -> int:
    """Run a process without a console window and return the exit code.

    The exit code is forgotten right away: in silent mode VeraCrypt fails
    silently. It is taken only to go into the error text.
    """
    options: dict[str, object] = {"timeout": timeout, "capture_output": True}
    if IS_WINDOWS:
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        completed = subprocess.run(list(command), **options)  # type: ignore[arg-type]
    except subprocess.TimeoutExpired as exc:
        raise VeraCryptError(
            f"{Path(command[0]).name} не ответил за {timeout} с и был снят."
        ) from exc
    except OSError as exc:
        raise VeraCryptError(f"Не удалось запустить {command[0]}: {exc}") from exc
    return completed.returncode


@dataclass(frozen=True)
class Volumes:
    """Reading a mounted volume. Replaced as a whole in tests."""

    drives: Callable[[], list[str]] = staticmethod(mounted_drives)
    usage: Callable[[str], tuple[int, int]] = staticmethod(volume_usage)
    cluster: Callable[[str], "int | None"] = staticmethod(cluster_size)
    filesystem: Callable[[str], str] = staticmethod(volume_filesystem)
    #: Where to write the file set when measuring slack. The only place where
    #: the application writes to a volume instead of reading it; in tests it
    #: is replaced with a temporary folder, and file set generation is tested
    #: without VeraCrypt.
    root: Callable[[str], Path] = staticmethod(volume_root)


@dataclass
class VeraCrypt:
    """Three operations; the result is checked by facts, not the exit code."""

    install: Install
    password: str = CALIBRATION_PASSWORD
    run: Callable[[Sequence[str], int], int] = staticmethod(run_command)
    #: Pause between polls. Replaced so that a test does not really sleep.
    pause: Callable[[float], None] = staticmethod(time.sleep)
    volumes: Volumes = field(default_factory=Volumes)

    # --- operations --------------------------------------------------------

    def create(
        self,
        path: Path,
        size_bytes: int,
        dynamic: bool = True,
        quick: bool = True,
    ) -> None:
        code = self.run(
            create_command(
                self.install, path, size_bytes, self.password, dynamic, quick
            ),
            CREATE_TIMEOUT,
        )
        actual = path.stat().st_size if path.exists() else None
        if actual is None:
            raise VeraCryptError(
                f"Контейнер {path.name} не создан (код возврата {code}). "
                f"Чаще всего это отказ в правах: форматирование NTFS требует "
                f"администратора, а в тихом режиме VeraCrypt об этом молчит."
            )
        if actual != size_bytes:
            raise VeraCryptError(
                f"Контейнер {path.name} вышел {actual} B вместо {size_bytes} B. "
                f"Замер с такого контейнера встал бы не в свою строку."
            )

    def mount(self, path: Path, letter: str) -> None:
        code = self.run(
            mount_command(self.install, path, letter, self.password), MOUNT_TIMEOUT
        )
        self._await_letter(
            letter,
            present=True,
            message=(
                f"Том {letter}: не поднялся за {LETTER_TIMEOUT:g} с "
                f"(код возврата {code}). Проверьте, что драйвер VeraCrypt "
                f"установлен и запущен."
            ),
        )

    def unmount(self, letter: str, force_last: bool = False) -> None:
        """Unmount the volume by retrying, not by waiting out one minute.

        A refusal is visible at once: VeraCrypt returns a non-zero code and the
        letter stays. There is nothing to wait for after that — the volume is
        not unmounting slowly, it was not given up at all. But half a minute
        later it is: that is how all four failed measurements of the first real
        run behaved.

        A short grace period after the command is still needed: the driver
        takes the volume down, and the letter does not disappear the instant
        the process exits.

        `force_last` allows `/force` on the last attempt — and only on it. Not
        on all of them: VeraCrypt in silent mode swallows an unknown switch
        silently, and cleanup that started with force right away might not work
        at all. Three times the gentle way first, and only then by force.
        """
        code = 0
        for attempt in range(UNMOUNT_ATTEMPTS):
            if attempt:
                self.pause(UNMOUNT_RETRY_SECONDS)
                if self._letter_gone(letter, 0.0):
                    return
            last = attempt + 1 == UNMOUNT_ATTEMPTS
            code = self.run(
                unmount_command(self.install, letter, force_last and last),
                MOUNT_TIMEOUT,
            )
            if self._letter_gone(letter, UNMOUNT_GRACE):
                return
        raise VeraCryptError(
            f"Том {letter}: не размонтировался за {UNMOUNT_ATTEMPTS} "
            f"{plural(UNMOUNT_ATTEMPTS, 'попытку', 'попытки', 'попыток')} "
            f"(последний код возврата {code}). Пока он поднят, файл "
            f"контейнера удалить нельзя."
        )

    def unmount_quietly(self, letter: str) -> bool:
        """Unmount the volume without masking an error that already happened.

        For finally. Here and only here `/force` is allowed, and even then as
        the last attempt: the container is deleted right after, there is
        nothing to lose on it, and leaving a mounted volume and a terabyte
        file behind is not an option.
        """
        try:
            if f"{letter}:" not in self._taken():
                return True
            self.unmount(letter, force_last=True)
            return True
        except VeraCryptError:
            return False

    # --- drive letters -----------------------------------------------------

    def free_letter(self) -> str:
        taken = self._taken()
        for letter in LETTERS:
            if f"{letter}:" not in taken:
                return letter
        raise VeraCryptError("Свободных букв дисков не осталось.")

    def _taken(self) -> set[str]:
        return {item.upper().rstrip("\\/") for item in self.volumes.drives()}

    def _await_letter(self, letter: str, present: bool, message: str) -> None:
        """Wait for the letter to appear or vanish. By polling, not by code."""
        waited = 0.0
        while True:
            if (f"{letter}:" in self._taken()) == present:
                return
            if waited >= LETTER_TIMEOUT:
                raise VeraCryptError(message)
            self.pause(POLL_SECONDS)
            waited += POLL_SECONDS

    def _letter_gone(self, letter: str, timeout: float) -> bool:
        """Whether the letter went away in the given time. No exception.

        The caller decides. A zero timeout is just a look at the list of
        mounted drives, without a single pause: it checks whether the volume
        was let go while we waited between attempts.
        """
        waited = 0.0
        while True:
            if f"{letter}:" not in self._taken():
                return True
            if waited >= timeout:
                return False
            self.pause(POLL_SECONDS)
            waited += POLL_SECONDS


# --- temporary containers --------------------------------------------------


def container_name(container_mib: int, key: str = "") -> str:
    """Temporary container name. The file set key tells equal sizes apart.

    Without it two file sets for which the calculation gave the same
    Container init would write to one file: the first is not deleted yet, the
    second is already being created, and VeraCrypt refuses silently.
    """
    suffix = f"-{key}" if key else ""
    return f"{CONTAINER_PREFIX}{container_mib}{suffix}{CONTAINER_SUFFIX}"


def orphans(workdir: str | os.PathLike[str]) -> list[Path]:
    """Containers left over from an interrupted collection."""
    directory = Path(workdir)
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{CONTAINER_PREFIX}*{CONTAINER_SUFFIX}"))


def remove_container(path: Path) -> bool:
    """Delete the container file.

    False means it failed, most often because the volume is still mounted.
    """
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False
