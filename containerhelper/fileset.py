"""File sets for measuring copy slack.

Copy slack is what the mere appearance of files on the volume costs beyond
their cluster-rounded size: an MFT record per file, the growth of the folder
index, the service structures of the first write. The model counts it as
`base + per_file × n`, and the per-file slack is still confirmed by nothing:
the two parts can only be separated by measurements with different file
counts, and the store held exactly one such measurement.

Hence the file sets: several deliberately different `n`, taken in a row by one
machine. The files are generated **right on the mounted volume**, not copied
from somewhere on disk. There is nothing to copy — nobody has a set of this
shape lying around — and the host would have to keep a second copy next to the
container and spend twice as much space. For the measured value it is one and
the same: copy slack measures the appearance of files on the volume, not where
the bytes came from.

No Qt: the composition of the sets and their arithmetic are not display, and
must be tested without the interface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .model import DEFAULT_CLUSTER_BYTES, MIB, Payload, round_up

KIB = 1024
GIB = 1024 * MIB

#: The smallest file in a set. A kilobyte, not less: a file shorter than about
#: seven hundred bytes NTFS keeps right in the MFT record and gives it no
#: cluster at all. Then Σ ceil(size / cluster) overstates the used space, the
#: measured slack comes out negative, and the record is rejected by the "used
#: is smaller than the file" check.
SMALL_FILE = KIB

#: File name in a set. Long on purpose: the longer the name, the larger the
#: entry in the directory index, and with short names the index would grow less
#: than with real data. Underestimating the slack is the only dangerous side,
#: and any error here must lean the other way.
FILE_NAME = "containerhelper-payload-{index:06d}.bin"

#: Folder on the volume that the set goes into. Not the root: data is almost
#: always put in as a folder, and the root index is arranged differently from
#: the index of an ordinary folder.
PAYLOAD_DIR = "containerhelper-payload"

#: Chunk size to write with. Four megabytes is a compromise between the number
#: of system calls and the memory for the buffer.
WRITE_CHUNK = 4 * MIB


@dataclass(frozen=True)
class Group:
    """One size group of a set: so many files of such a size."""

    count: int
    size_bytes: int

    @property
    def logical_bytes(self) -> int:
        return self.count * self.size_bytes

    def alloc_bytes(self, cluster_bytes: int = DEFAULT_CLUSTER_BYTES) -> int:
        """Sum of cluster-rounded sizes, not the sum's cluster-rounded size.

        Each file is rounded up separately: on five hundred files of a
        kilobyte each, the difference between these two values is one and a
        half megabytes, and all of it would end up in the measured slack.
        """
        return self.count * round_up(self.size_bytes, cluster_bytes)


@dataclass(frozen=True)
class FileSet:
    """File set: what it consists of and how much space it will take."""

    key: str
    title: str
    groups: tuple[Group, ...]

    @property
    def file_count(self) -> int:
        return sum(group.count for group in self.groups)

    @property
    def logical_bytes(self) -> int:
        return sum(group.logical_bytes for group in self.groups)

    def alloc_bytes(self, cluster_bytes: int = DEFAULT_CLUSTER_BYTES) -> int:
        return sum(group.alloc_bytes(cluster_bytes) for group in self.groups)

    def payload(self, cluster_bytes: int = DEFAULT_CLUSTER_BYTES) -> Payload:
        """Payload, without unfolding the set into a list of sizes.

        Ten thousand numbers for a sum that is computed by multiplication are
        wasted work, both in the loop and in memory.
        """
        return Payload(
            logical_bytes=self.logical_bytes,
            alloc_bytes=self.alloc_bytes(cluster_bytes),
            file_count=self.file_count,
            cluster_bytes=cluster_bytes,
        )


#: The file sets that calibrate copy slack. The file counts are chosen so that
#: there are many distinct `n`: the slope in CopySlackModel.calibrate is found
#: by least squares and is taken at all only with two or more distinct `n`,
#: and the dependence on `n` is not quite linear — the MFT grows in chunks,
#: and from two points the slope would be random.
#:
#: The amount of data is small almost everywhere: slack depends on the number
#: of files, not on their size. Exactly this assumption is what the two sets
#: with `n = 1` — 64 MiB and 4 GiB — check. Should they diverge, the model
#: "slack depends only on n" is wrong, and that must be learned explicitly,
#: not suspected.
FILE_SETS = (
    FileSet("one", "Один файл 64 MiB", (Group(1, 64 * MIB),)),
    FileSet("fifty", "50 файлов по 10 MiB", (Group(50, 10 * MIB),)),
    FileSet("small-500", "500 файлов по 1 KiB", (Group(500, SMALL_FILE),)),
    FileSet("small-5000", "5 000 файлов по 1 KiB", (Group(5000, SMALL_FILE),)),
    FileSet("small-10000", "10 000 файлов по 1 KiB", (Group(10000, SMALL_FILE),)),
    FileSet(
        "mixed",
        "500 × 1 KiB + 50 × 10 MiB + 1 × 1 GiB",
        (Group(500, SMALL_FILE), Group(50, 10 * MIB), Group(1, GIB)),
    ),
    FileSet("huge", "Один файл 4 GiB", (Group(1, 4 * GIB),)),
)


def fileset_by_key(key: str) -> FileSet | None:
    for item in FILE_SETS:
        if item.key == key:
            return item
    return None


def file_sizes(fileset: FileSet) -> Iterable[int]:
    """File sizes of a set in order — the same order they are created in."""
    for group in fileset.groups:
        for _ in range(group.count):
            yield group.size_bytes


def generate(
    root: Path,
    fileset: FileSet,
    on_progress: Callable[[int, int], None] | None = None,
    check: Callable[[int], None] | None = None,
) -> int:
    """Create the set in the root folder. Returns the number of files created.

    `check` is called at the boundary of each file and after each full chunk
    of a large file; it is passed the number of bytes already written. It can
    interrupt the write only by an exception — the return value is not looked
    at, because a silent refusal here is indistinguishable from success. Both
    cancellation and watching the host's free space work through it: a
    terabyte disk can run out mid-write because of an unrelated process, and
    writing into a dynamic container on a disk that has run out tears the
    volume.

    `on_progress` is called in the same places as `check`: at a file boundary
    and after each full chunk. It must not be called only at file boundaries —
    the "One 4 GiB file" set is one boundary per several minutes of writing,
    and all that time the progress bar stands still and the window looks hung.
    Throttling is the caller's concern: on ten thousand files, a signal across
    the thread boundary ten thousand times would clog the event queue.
    """
    root.mkdir(parents=True, exist_ok=True)
    # Not zeros: the volume is fresh and uncompressed, but there is no reason
    # to depend on zeros not being folded away by anything on it. The buffer
    # is no larger than the largest file — a set of kilobyte files has no use
    # for four megabytes of random bytes.
    largest = max((group.size_bytes for group in fileset.groups), default=0)
    buffer = os.urandom(max(min(largest, WRITE_CHUNK), 1))

    index = 0
    files_done = 0
    bytes_done = 0
    since_check = 0
    for group in fileset.groups:
        for _ in range(group.count):
            if check is not None:
                check(bytes_done)
                since_check = 0
            path = root / FILE_NAME.format(index=index)
            index += 1
            with open(path, "wb") as handle:
                left = group.size_bytes
                while left > 0:
                    chunk = min(left, len(buffer))
                    handle.write(buffer if chunk == len(buffer) else buffer[:chunk])
                    left -= chunk
                    bytes_done += chunk
                    since_check += chunk
                    if since_check >= WRITE_CHUNK:
                        if check is not None:
                            check(bytes_done)
                        if on_progress is not None:
                            on_progress(files_done, bytes_done)
                        since_check = 0
            files_done += 1
            if on_progress is not None:
                on_progress(files_done, bytes_done)
    return files_done
