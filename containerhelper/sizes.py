"""Measuring the source data and mounted volumes."""

from __future__ import annotations

import os
import shutil
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .i18n import tr
from .model import DEFAULT_CLUSTER_BYTES, Payload

IS_WINDOWS = sys.platform == "win32"


@dataclass
class SourceStat:
    """One selected source: a file or a whole folder.

    File sizes are kept as a list, not a sum: a change of cluster size
    recalculates the cluster-rounded size, and there is no need to walk the
    tree again for that. For the same reason the sources are not merged into
    one heap — the table shows how much each one brought.
    """

    path: str
    is_dir: bool
    sizes: list[int] = field(default_factory=list)
    #: Nested folders. They take almost no space, but they explain where the
    #: difference between the file count and the number of selected items
    #: comes from.
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
        """Short name for the table. The full path goes into the tooltip."""
        return os.path.basename(self.path.rstrip("\\/")) or self.path


@dataclass
class SourceStats:
    """Summary of everything selected — what the calculation is made of.

    Computed here, not in the interface: a sum over several sources is
    arithmetic, not display, and it has to be tested without Qt.
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
        """What is paid extra for rounding each file up to a cluster."""
        return self.alloc_bytes - self.logical_bytes


@dataclass
class ScanResult:
    payload: Payload
    errors: list[str]
    #: Sizes of the individual files. Needed to recalculate the payload when
    #: the cluster size changes, without walking the tree again.
    sizes: list[int] = field(default_factory=list)
    #: Split by selected items. One item is the usual case, but several files
    #: mixed with folders can be selected too.
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
            # Integer, like everything else: a fractional "average byte" makes
            # no sense, and rounding down is more honest.
            average_bytes=logical // count if count else 0,
            empty_files=sum(1 for size in sizes if size == 0),
            unreadable=len(self.errors),
        )


def scan_paths(
    paths: Iterable[str | os.PathLike[str]],
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> ScanResult:
    """Walk several selected files and folders at once.

    Nested paths are dropped: a selected folder and a file inside it would
    give that file twice, and double counting silently inflates the
    calculation.

    Inaccessible items do not interrupt the walk: they are collected in
    errors, and the calculation stays possible from what could be read.
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
    """Count cluster-rounded size and file count for a file or folder."""
    return scan_paths([path], cluster_bytes)


def unique_roots(paths: Iterable[str | os.PathLike[str]]) -> list[str]:
    """Remove repeats and paths lying inside other selected folders.

    The selection order is kept: in the table the sources stand the way the
    user named them, not the way it was more convenient to compare them.
    """
    ordered: list[str] = []
    for raw in paths:
        text = os.path.abspath(os.fspath(raw))
        if text not in ordered:
            ordered.append(text)

    parents: list[str] = []
    accepted: set[str] = set()
    # A parent is always shorter than a nested path and so sorts before it:
    # string order here is exactly nesting order.
    for text in sorted(ordered, key=os.path.normcase):
        key = os.path.normcase(text).rstrip("\\/")
        if any(key.startswith(parent) for parent in parents):
            continue
        parents.append(key + os.sep)
        accepted.add(key)
    return [text for text in ordered if os.path.normcase(text).rstrip("\\/") in accepted]


def _scan_one(target: Path) -> SourceStat:
    if not target.exists():
        return SourceStat(
            str(target), False, [], 0, [tr("sizes.path.missing", path=target)]
        )

    if target.is_file():
        try:
            size = target.stat().st_size
        except OSError as exc:
            error = tr("sizes.path.denied", path=target, error=exc)
            return SourceStat(str(target), False, [], 0, [error])
        return SourceStat(str(target), False, [size])

    sizes: list[int] = []
    errors: list[str] = []
    dirs = _walk(target, sizes, errors)
    return SourceStat(str(target), True, sizes, dirs, errors)


def _walk(directory: Path, sizes: list[int], errors: list[str]) -> int:
    """Collect file sizes; return the number of nested folders walked."""
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        errors.append(tr("sizes.path.denied", path=directory, error=exc))
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
            errors.append(tr("sizes.path.denied", path=entry.path, error=exc))
    return dirs


def volume_usage(drive: str) -> tuple[int, int]:
    """Volume capacity and free space in bytes."""
    root = _root_path(drive)
    usage = shutil.disk_usage(root)
    return usage.total, usage.free


def cluster_size(drive: str) -> int | None:
    """Cluster size of the volume. None if it could not be determined."""
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
    """Letters of the mounted volumes, for example ['C:', 'E:']."""
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
    """Turn 'E', 'E:' or 'E:\\' into the root path of the volume."""
    cleaned = drive.strip().rstrip("\\/")
    if not cleaned.endswith(":"):
        cleaned = f"{cleaned[:1]}:"
    return f"{cleaned}\\" if IS_WINDOWS else cleaned


def volume_root(drive: str) -> Path:
    """Volume root as a path — the copy-slack file set is written there.

    A separate function, because this is the only place where the application
    writes to the volume rather than reading it: in tests it is replaced with
    a temporary folder, and file set generation is tested without VeraCrypt.
    """
    return Path(_root_path(drive))


#: Folders that NTFS creates on the volume by itself. The space they take is
#: real and lands in "used", but they are not payload, so in the reconciliation
#: they are shown as a separate row rather than counted as a discrepancy.
SERVICE_DIRS = ("System Volume Information", "$RECYCLE.BIN", "found.000")


@dataclass
class VolumeScan:
    """What is on the mounted volume — to reconcile with what was copied."""

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
    """Walk the volume root, separating the NTFS service folders from payload.

    Service folders are usually unreadable as well, so they are skipped
    before the walk: otherwise they would fill errors with noise.
    """
    root = Path(_root_path(drive))
    sizes: list[int] = []
    errors: list[str] = []
    service: list[str] = []

    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        return VolumeScan(
            Payload.for_files((), cluster_bytes),
            [],
            [tr("sizes.path.denied", path=root, error=exc)],
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
            errors.append(tr("sizes.path.denied", path=entry.path, error=exc))

    return VolumeScan(Payload.for_files(sizes, cluster_bytes), service, errors)


#: The only filesystem the calibration records were taken for. exFAT and
#: FAT32 have their overhead arranged differently, and the metadata model on
#: them is not just inaccurate — it is about something else.
SUPPORTED_FS = "NTFS"


def volume_filesystem(drive: str) -> str:
    """Volume filesystem name: 'NTFS', 'exFAT', 'FAT32'. Empty — it failed."""
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
