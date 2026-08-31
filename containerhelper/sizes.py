"""Измерение исходных данных и смонтированных томов."""

from __future__ import annotations

import os
import shutil
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .model import DEFAULT_CLUSTER_BYTES, Payload

IS_WINDOWS = sys.platform == "win32"


@dataclass
class SourceStat:
    """Один выбранный источник: файл или папка целиком.

    Размеры файлов держатся списком, а не суммой: смена размера кластера
    пересчитывает объём по кластерам, и обходить дерево заново ради этого не
    нужно. По той же причине источники не сливаются в одну кучу — в таблице
    видно, сколько принёс каждый.
    """

    path: str
    is_dir: bool
    sizes: list[int] = field(default_factory=list)
    #: Вложенные каталоги. Места они почти не занимают, но объясняют, откуда
    #: берётся разница между числом файлов и числом выбранных элементов.
    dir_count: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.sizes)

    @property
    def logical_bytes(self) -> int:
        return sum(self.sizes)

    def alloc_bytes(self, cluster_bytes: int = DEFAULT_CLUSTER_BYTES) -> int:
        return Payload.for_files(self.sizes, cluster_bytes).alloc_bytes

    @property
    def name(self) -> str:
        """Короткое имя для таблицы. Полный путь уходит в подсказку."""
        return os.path.basename(self.path.rstrip("\\/")) or self.path


@dataclass
class SourceStats:
    """Сводка по всему выбранному — то, из чего складывается расчёт.

    Считается здесь, а не в интерфейсе: сумма по нескольким источникам это
    арифметика, а не отображение, и проверять её надо без Qt.
    """

    sources: int
    folders: int
    files: int
    file_count: int
    dir_count: int
    logical_bytes: int
    alloc_bytes: int
    largest_bytes: int
    smallest_bytes: int
    average_bytes: int
    empty_files: int
    unreadable: int

    @property
    def cluster_tail(self) -> int:
        """Что доплачивается за округление каждого файла до кластера."""
        return self.alloc_bytes - self.logical_bytes


@dataclass
class ScanResult:
    payload: Payload
    errors: list[str]
    #: Размеры отдельных файлов. Нужны, чтобы пересчитать payload при смене
    #: размера кластера, не обходя дерево заново.
    sizes: list[int] = field(default_factory=list)
    #: Разбивка по выбранным элементам. Один элемент — обычный случай, но
    #: выбрать можно и несколько файлов вперемешку с папками.
    sources: list[SourceStat] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def file_count(self) -> int:
        return len(self.sizes)

    @property
    def logical_bytes(self) -> int:
        return sum(self.sizes)

    @property
    def dir_count(self) -> int:
        return sum(source.dir_count for source in self.sources)

    @property
    def paths(self) -> list[str]:
        return [source.path for source in self.sources]

    def with_cluster(self, cluster_bytes: int) -> Payload:
        return Payload.for_files(self.sizes, cluster_bytes)

    def stats(self, cluster_bytes: int = DEFAULT_CLUSTER_BYTES) -> SourceStats:
        sizes = self.sizes
        count = len(sizes)
        logical = sum(sizes)
        return SourceStats(
            sources=len(self.sources),
            folders=sum(1 for source in self.sources if source.is_dir),
            files=sum(1 for source in self.sources if not source.is_dir),
            file_count=count,
            dir_count=self.dir_count,
            logical_bytes=logical,
            alloc_bytes=self.with_cluster(cluster_bytes).alloc_bytes,
            largest_bytes=max(sizes) if sizes else 0,
            smallest_bytes=min(sizes) if sizes else 0,
            # Целочисленно, как и всё остальное: дробный «средний байт» смысла
            # не имеет, а округлять вниз честнее.
            average_bytes=logical // count if count else 0,
            empty_files=sum(1 for size in sizes if size == 0),
            unreadable=len(self.errors),
        )


def scan_paths(
    paths: Iterable[str | os.PathLike[str]],
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> ScanResult:
    """Обойти несколько выбранных файлов и папок разом.

    Вложенные пути отбрасываются: выбранная папка и лежащий в ней файл дали бы
    этот файл дважды, а от двойного счёта расчёт завышается молча.

    Недоступные элементы не прерывают обход: они собираются в errors, а расчёт
    остаётся возможным по тому, что удалось прочитать.
    """
    sources = [_scan_one(Path(path)) for path in unique_roots(paths)]
    sizes: list[int] = []
    errors: list[str] = []
    for source in sources:
        sizes.extend(source.sizes)
        errors.extend(source.errors)
    return ScanResult(Payload.for_files(sizes, cluster_bytes), errors, sizes, sources)


def scan_path(
    path: str | os.PathLike[str],
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> ScanResult:
    """Посчитать кластерный размер и число файлов для файла или папки."""
    return scan_paths([path], cluster_bytes)


def unique_roots(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    """Убрать повторы и пути, лежащие внутри других выбранных папок.

    Порядок выбора сохраняется: в таблице источники стоят так, как их назвал
    пользователь, а не так, как их удобнее было сравнивать между собой.
    """
    ordered: list[str] = []
    for raw in paths:
        text = os.path.abspath(os.fspath(raw))
        if text not in ordered:
            ordered.append(text)

    parents: list[str] = []
    accepted: set[str] = set()
    # Родитель всегда короче вложенного и потому встаёт раньше него: строковый
    # порядок здесь и есть порядок вложенности.
    for text in sorted(ordered, key=os.path.normcase):
        key = os.path.normcase(text).rstrip("\\/")
        if any(key.startswith(parent) for parent in parents):
            continue
        parents.append(key + os.sep)
        accepted.add(key)
    return [text for text in ordered if os.path.normcase(text).rstrip("\\/") in accepted]


def _scan_one(target: Path) -> SourceStat:
    if not target.exists():
        return SourceStat(str(target), False, [], 0, [f"Путь не найден: {target}"])

    if target.is_file():
        try:
            size = target.stat().st_size
        except OSError as exc:
            return SourceStat(
                str(target), False, [], 0, [f"Нет доступа к {target}: {exc}"]
            )
        return SourceStat(str(target), False, [size])

    sizes: list[int] = []
    errors: list[str] = []
    dirs = _walk(target, sizes, errors)
    return SourceStat(str(target), True, sizes, dirs, errors)


def _walk(directory: Path, sizes: list[int], errors: list[str]) -> int:
    """Собрать размеры файлов; вернуть число пройденных вложенных каталогов."""
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        errors.append(f"Нет доступа к {directory}: {exc}")
        return 0

    dirs = 0
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                dirs += 1 + _walk(Path(entry.path), sizes, errors)
            elif entry.is_file():
                sizes.append(entry.stat().st_size)
        except OSError as exc:
            errors.append(f"Нет доступа к {entry.path}: {exc}")
    return dirs


def volume_usage(drive: str) -> tuple[int, int]:
    """Ёмкость и свободное место тома в байтах."""
    root = _root_path(drive)
    usage = shutil.disk_usage(root)
    return usage.total, usage.free


def cluster_size(drive: str) -> int | None:
    """Размер кластера тома. None, если определить не удалось."""
    if not IS_WINDOWS:
        return None

    import ctypes
    from ctypes import wintypes

    sectors_per_cluster = wintypes.DWORD()
    bytes_per_sector = wintypes.DWORD()
    free_clusters = wintypes.DWORD()
    total_clusters = wintypes.DWORD()

    ok = ctypes.windll.kernel32.GetDiskFreeSpaceW(
        ctypes.c_wchar_p(_root_path(drive)),
        ctypes.byref(sectors_per_cluster),
        ctypes.byref(bytes_per_sector),
        ctypes.byref(free_clusters),
        ctypes.byref(total_clusters),
    )
    if not ok:
        return None
    return sectors_per_cluster.value * bytes_per_sector.value


def mounted_drives() -> list[str]:
    """Буквы смонтированных томов, например ['C:', 'E:']."""
    if not IS_WINDOWS:
        return []

    import ctypes

    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [
        f"{letter}:"
        for index, letter in enumerate(string.ascii_uppercase)
        if mask & (1 << index)
    ]


def _root_path(drive: str) -> str:
    """Привести 'E', 'E:' или 'E:\\' к корневому пути тома."""
    cleaned = drive.strip().rstrip("\\/")
    if not cleaned.endswith(":"):
        cleaned = f"{cleaned[:1]}:"
    return f"{cleaned}\\" if IS_WINDOWS else cleaned


def volume_root(drive: str) -> Path:
    """Корень тома как путь — туда пишется набор при замере запаса.

    Отдельной функцией, потому что это единственное место, где приложение
    пишет на том, а не читает его: в тестах она подменяется временной папкой,
    и генерация набора проверяется без VeraCrypt.
    """
    return Path(_root_path(drive))


#: Каталоги, которые NTFS заводит на томе сама. Место они занимают настоящее и
#: попадают в «занято», но полезными данными не являются, поэтому в сверке
#: показываются отдельной строкой, а не считаются расхождением.
SERVICE_DIRS = ("System Volume Information", "$RECYCLE.BIN", "found.000")


@dataclass
class VolumeScan:
    """Что лежит на смонтированном томе — для сверки с тем, что копировали."""

    payload: Payload
    service_dirs: list[str]
    errors: list[str]

    @property
    def empty(self) -> bool:
        return self.payload.file_count == 0


def scan_volume(
    drive: str,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> VolumeScan:
    """Обойти корень тома, отделив служебные каталоги NTFS от полезных данных.

    Служебные каталоги обычно ещё и недоступны на чтение, поэтому их
    пропускают до обхода: иначе они наполнили бы errors шумом.
    """
    root = Path(_root_path(drive))
    sizes: list[int] = []
    errors: list[str] = []
    service: list[str] = []

    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        return VolumeScan(
            Payload.for_files((), cluster_bytes), [], [f"Нет доступа к {root}: {exc}"]
        )

    for entry in entries:
        if entry.name in SERVICE_DIRS:
            service.append(entry.name)
            continue
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                _walk(Path(entry.path), sizes, errors)
            elif entry.is_file():
                sizes.append(entry.stat().st_size)
        except OSError as exc:
            errors.append(f"Нет доступа к {entry.path}: {exc}")

    return VolumeScan(Payload.for_files(sizes, cluster_bytes), service, errors)


#: Единственная файловая система, под которую сняты калибровочные записи.
#: У exFAT и FAT32 накладные расходы устроены иначе, и модель метаданных на
#: них не просто неточна — она про другое.
SUPPORTED_FS = "NTFS"


def volume_filesystem(drive: str) -> str:
    """Имя файловой системы тома: 'NTFS', 'exFAT', 'FAT32'. Пусто — не удалось."""
    if not IS_WINDOWS:
        return ""

    import ctypes
    from ctypes import wintypes

    name = ctypes.create_unicode_buffer(261)
    filesystem = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_component = wintypes.DWORD()
    flags = wintypes.DWORD()

    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(_root_path(drive)),
        name,
        ctypes.sizeof(name) // ctypes.sizeof(ctypes.c_wchar),
        ctypes.byref(serial),
        ctypes.byref(max_component),
        ctypes.byref(flags),
        filesystem,
        ctypes.sizeof(filesystem) // ctypes.sizeof(ctypes.c_wchar),
    )
    return filesystem.value if ok else ""
