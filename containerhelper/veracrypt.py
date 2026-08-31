"""Поиск VeraCrypt и три операции над контейнером через командную строку.

Без Qt: всё, что здесь есть, проверяется без интерфейса. Запуск процессов и
чтение томов подменяются полями, потому что настоящая проверка требует
установленного VeraCrypt, прав администратора и нескольких минут на каждый
контейнер — в наборе тестов такому места нет.

Сверено с документацией VeraCrypt 1.26.24 (`docs/html/en/Command Line
Usage.html` в поставке). Отсюда и странности:

- создание и монтирование — **разные бинарники**;
- `/pim` у `VeraCrypt Format.exe` нет вовсе, он только при монтировании,
  поэтому ускорить создание уменьшенным числом итераций нельзя;
- `/nosizecheck` обязателен, иначе динамический контейнер на терабайт
  откажется создаваться там, где терабайта свободного нет;
- `/dismount` устарел, нужен `/unmount`;
- `/hash sha512` при монтировании заметно ускоряет: без него VeraCrypt
  перебирает все PRF подряд;
- `/silent` описан как «If there is any error, the operation will fail
  silently», поэтому на код возврата здесь не полагается ничего: результат
  проверяется по факту — появился ли файл нужного размера, поднялся ли том.
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

#: Имя папки внутри Program Files. Одно и то же у 32- и 64-битной установки.
INSTALL_SUBDIR = "VeraCrypt"

#: Переменные окружения, из которых берутся оба Program Files. ProgramW6432
#: добавлен ради 32-битного Python на 64-битной Windows: там ProgramFiles
#: указывает в «(x86)», и настоящая установка иначе не нашлась бы.
PROGRAM_FILES_VARIABLES = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")

#: Запасные пути на случай пустого окружения. Стандартные места установки
#: названы явно: переменных может не быть, а VeraCrypt всё равно там.
FALLBACK_DIRS = (
    r"C:\Program Files\VeraCrypt",
    r"C:\Program Files (x86)\VeraCrypt",
)

#: Имена бинарников. У установленной сборки без суффикса, у портативной — с
#: суффиксом архитектуры. Порядок здесь и есть порядок предпочтения.
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

#: Параметры контейнера. Меняться им незачем: замер должен повторять то, как
#: контейнеры создают руками, а не искать оптимум.
HASH = "sha512"
ENCRYPTION = "AES"
FILESYSTEM = "NTFS"

#: Версии, в которых менялись нужные нам ключи. Взято из Release Notes в
#: поставке VeraCrypt 1.26.24.
#:
#: 1.24 добавила `/nosizecheck` и `/quick` (до неё быстрое форматирование из
#: командной строки не выключалось отдельным ключом). Без них сбор не идёт
#: вовсе: терабайтный контейнер упрётся в проверку свободного места.
MINIMUM_VERSION = (1, 24)

#: 1.25.4: «Avoid displaying waiting dialog when /silent specified ... during
#: creating of file container ... and a filesystem other than FAT». До неё окно
#: ожидания всё равно выскакивает на каждый контейнер.
QUIET_CREATE_SINCE = (1, 25, 4)

#: 1.26.20 переименовала Dismount в Unmount. `/unmount` до неё не понимают, а
#: `/dismount` понимают все версии до сегодняшней включительно — он объявлен
#: устаревшим, но в документации 1.26.24 по-прежнему описан и работает.
UNMOUNT_SINCE = (1, 26, 20)

#: Пароль временного контейнера. Контейнер живёт минуты и удаляется сразу
#: после замера, но на коротком пароле VeraCrypt показывает предупреждение,
#: а предупреждение в тихом режиме — это молчаливый отказ.
CALIBRATION_PASSWORD = "ContainerHelperCalibration2026"

#: Сколько ждать процесс. Создание терабайтного динамического контейнера —
#: секунды, обычного с полным форматированием — минуты. Потолок не про норму,
#: а про то, чтобы зависший процесс не остался висеть навсегда.
CREATE_TIMEOUT = 3600
MOUNT_TIMEOUT = 300

#: Буква появляется не в тот же миг, когда вышел процесс: том поднимает
#: драйвер. Поэтому опрашивается список смонтированных, а не код возврата.
LETTER_TIMEOUT = 60.0
POLL_SECONDS = 0.5

#: Размонтирование повторяется, а не ждётся одной минутой. Сразу после записи
#: данных VeraCrypt отказывает размонтировать том — кодом возврата 1 и
#: немедленно, — а через полминуты отдаёт его без возражений. На первом
#: настоящем прогоне сбора запаса так сорвались четыре замера из семи, и
#: спасала их только повторная попытка в уборке: она случалась минутой позже
#: и проходила. Здесь то же самое делается намеренно.
#:
#: Ждать после отказа минуту бессмысленно: том не «медленно размонтируется»,
#: его не отдали вовсе. Поэтому короткая отсрочка на драйвер, потом пауза и
#: новая команда.
UNMOUNT_ATTEMPTS = 4
UNMOUNT_GRACE = 5.0
UNMOUNT_RETRY_SECONDS = 15.0

#: Как называются временные контейнеры. Имя не для красоты: по нему уборка
#: находит осиротевшие файлы — терабайтный файл, переживший падение, иначе
#: лежал бы в папке молча и вечно.
CONTAINER_PREFIX = "containerhelper-calibration-"
CONTAINER_SUFFIX = ".hc"

#: Буквы, среди которых ищется свободная. A и B заняты историей, C — системой;
#: сверху вниз, чтобы не занимать те, что система раздаёт следующими.
LETTERS = tuple(reversed(string.ascii_uppercase[3:]))


class VeraCryptError(Exception):
    """Операция не удалась. Текст пригоден для показа пользователю."""


@dataclass(frozen=True)
class Install:
    """Найденная установка: папка и оба бинарника."""

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
        """Версия числами для сравнения. Пустая — прочитать не удалось."""
        return parse_version(self.version)

    @property
    def knows_unmount(self) -> bool:
        """Понимает ли эта версия `/unmount`.

        Непрочитанная версия считается старой: `/dismount` работает и на
        новых, а `/unmount` на старых — нет. Ошибиться в эту сторону дешевле.
        """
        return self.number >= UNMOUNT_SINCE


def standard_dirs() -> list[Path]:
    """Стандартные места установки, без повторов и в порядке предпочтения."""
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
    """Собрать установку из папки. Указать можно и сам exe — возьмётся папка."""
    path = Path(directory)
    if path.is_file():
        path = path.parent
    format_exe = _first_existing(path, FORMAT_NAMES)
    mount_exe = _first_existing(path, MOUNT_NAMES)
    if format_exe is None or mount_exe is None:
        return None
    return Install(path, format_exe, mount_exe, file_version(mount_exe))


def find_install(extra: Iterable[str | os.PathLike[str]] = ()) -> Install | None:
    """Найти VeraCrypt: сначала указанное руками, потом стандартные места."""
    for directory in [*extra, *standard_dirs()]:
        if not directory:
            continue
        found = install_at(directory)
        if found is not None:
            return found
    return None


def missing_report(directory: str | os.PathLike[str]) -> str:
    """Чего не хватает в указанной папке. Пусто — всё на месте."""
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
    """Версия из ресурсов exe. Пусто — прочитать не удалось.

    Версия нужна не расчёту, а человеку: размер метаданных решает не NTFS
    вообще, а конкретный код форматирования, и в отчёте это стоит видеть.
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
    # Четвёртое число у VeraCrypt всегда ноль и в разговоре не участвует.
    while len(parts) > 3 and parts[-1] == 0:
        parts.pop()
    return ".".join(str(part) for part in parts)


# --- командные строки ------------------------------------------------------
#
# Вынесены отдельными функциями, потому что проверять надо именно их: настоящий
# запуск набору тестов недоступен, а ошибка в одном ключе стоит нескольких
# часов работы и терабайтного файла в чужой папке.


def create_command(
    install: Install,
    path: Path,
    size_bytes: int,
    password: str = CALIBRATION_PASSWORD,
    dynamic: bool = True,
    quick: bool = True,
) -> list[str]:
    """Создание контейнера. Размер — точными байтами, а не суффиксом.

    Суффикс `G` округляет, а попасть надо в `container_mib × 1048576` до
    байта: иначе замер встанет мимо своей строки в таблице покрытия.
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
    """Монтирование. /hash избавляет VeraCrypt от перебора всех PRF подряд."""
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
    """Размонтирование тем ключом, который эта версия понимает.

    С 1.26.20 это `/unmount`, до неё — `/dismount`. Ключи не синонимы во
    времени: старая VeraCrypt на `/unmount` ругнётся, а с `/silent` — молча,
    и том останется поднятым вместе с файлом контейнера.

    `/force` — только для уборки. Он снимает том, даже когда файлы на нём
    заняты, а это значит, что несброшенное содержимое кэша может пропасть.
    Перед замером остатка так делать нельзя ни в коем случае: потерянная
    запись покажется лишним свободным местом, и измеренный запас выйдет
    **заниженным** — то есть ошибка уедет в единственную опасную сторону.
    В уборке терять нечего: контейнер тут же удаляется.
    """
    switch = "/unmount" if install.knows_unmount else "/dismount"
    command = [str(install.mount_exe), "/quit", "/silent", switch, letter]
    if force:
        command.append("/force")
    return command


def parse_version(text: str) -> tuple[int, ...]:
    """«1.26.24» → (1, 26, 24). Нечисловой хвост отбрасывается."""
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
    """Годится ли эта версия для сбора и что о ней стоит сказать вслух.

    Первое значение — можно ли начинать. Второе — текст для окна; пусто,
    когда версия свежая и говорить нечего.
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
    """Запустить процесс без консольного окна и вернуть код возврата.

    Код возврата тут же и забывается: в тихом режиме VeraCrypt падает молча.
    Он берётся только затем, чтобы попасть в текст ошибки.
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
    """Чтение смонтированного тома. Подменяется целиком в тестах."""

    drives: Callable[[], list[str]] = staticmethod(mounted_drives)
    usage: Callable[[str], tuple[int, int]] = staticmethod(volume_usage)
    cluster: Callable[[str], "int | None"] = staticmethod(cluster_size)
    filesystem: Callable[[str], str] = staticmethod(volume_filesystem)
    #: Куда писать набор файлов при замере запаса. Единственное место, где
    #: приложение пишет на том, а не читает его; в тестах подменяется
    #: временной папкой, и генерация набора проверяется без VeraCrypt.
    root: Callable[[str], Path] = staticmethod(volume_root)


@dataclass
class VeraCrypt:
    """Три операции с проверкой результата по факту, а не по коду возврата."""

    install: Install
    password: str = CALIBRATION_PASSWORD
    run: Callable[[Sequence[str], int], int] = staticmethod(run_command)
    #: Пауза между опросами. Подменяется, чтобы тест не спал по-настоящему.
    pause: Callable[[float], None] = staticmethod(time.sleep)
    volumes: Volumes = field(default_factory=Volumes)

    # --- операции ----------------------------------------------------------

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
        """Снять том, повторяя попытку, а не выжидая одну минуту.

        Отказ виден сразу: VeraCrypt возвращает ненулевой код и буква
        остаётся. Ждать после этого нечего — том не размонтируется медленно,
        его не отдали вовсе. Зато через полминуты отдают: так вели себя все
        четыре сорвавшихся замера первого настоящего прогона.

        Короткая отсрочка после команды всё же нужна: снимает том драйвер, и
        буква исчезает не в тот же миг, что вышел процесс.

        `force_last` разрешает применить `/force` на последней попытке — и
        только на ней. Не на всех: незнакомый ключ VeraCrypt в тихом режиме
        проглотит молча, и уборка, начнись она сразу с силы, могла бы не
        сработать вовсе. Сначала три раза по-хорошему, и лишь потом силой.
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
        """Снять том, не заслоняя уже случившуюся ошибку. Для finally.

        Здесь и только здесь разрешается `/force`, и то последней попыткой:
        контейнер сразу после этого удаляется, терять на нём нечего, а
        оставить поднятым том и файл на терабайт нельзя.
        """
        try:
            if f"{letter}:" not in self._taken():
                return True
            self.unmount(letter, force_last=True)
            return True
        except VeraCryptError:
            return False

    # --- буквы дисков ------------------------------------------------------

    def free_letter(self) -> str:
        taken = self._taken()
        for letter in LETTERS:
            if f"{letter}:" not in taken:
                return letter
        raise VeraCryptError("Свободных букв дисков не осталось.")

    def _taken(self) -> set[str]:
        return {item.upper().rstrip("\\/") for item in self.volumes.drives()}

    def _await_letter(self, letter: str, present: bool, message: str) -> None:
        """Дождаться появления или исчезновения буквы. Опросом, а не по коду."""
        waited = 0.0
        while True:
            if (f"{letter}:" in self._taken()) == present:
                return
            if waited >= LETTER_TIMEOUT:
                raise VeraCryptError(message)
            self.pause(POLL_SECONDS)
            waited += POLL_SECONDS

    def _letter_gone(self, letter: str, timeout: float) -> bool:
        """Ушла ли буква за отведённое время. Без исключения: решает вызывающий.

        Нулевой срок — просто взгляд на список смонтированных, без единой
        паузы: им проверяют, не отпустили ли том, пока мы ждали между
        попытками.
        """
        waited = 0.0
        while True:
            if f"{letter}:" not in self._taken():
                return True
            if waited >= timeout:
                return False
            self.pause(POLL_SECONDS)
            waited += POLL_SECONDS


# --- временные контейнеры --------------------------------------------------


def container_name(container_mib: int, key: str = "") -> str:
    """Имя временного контейнера. Ключ набора различает одинаковые размеры.

    Без него два набора файлов, которым расчёт выдал один и тот же
    Container init, писали бы в один файл: первый ещё не удалён, второй уже
    создаётся, и VeraCrypt отказывает молча.
    """
    suffix = f"-{key}" if key else ""
    return f"{CONTAINER_PREFIX}{container_mib}{suffix}{CONTAINER_SUFFIX}"


def orphans(workdir: str | os.PathLike[str]) -> list[Path]:
    """Контейнеры, оставшиеся от прерванного сбора."""
    directory = Path(workdir)
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{CONTAINER_PREFIX}*{CONTAINER_SUFFIX}"))


def remove_container(path: Path) -> bool:
    """Удалить файл контейнера. False — не вышло, чаще всего том ещё поднят."""
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False
