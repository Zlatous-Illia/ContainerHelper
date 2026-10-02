"""Factory calibration points that ship inside the program.

The point is for a new copy to calculate sensibly from the first launch:
measuring twenty-six empty containers from 512 MiB to 1 TiB is several hours
of work, and without dynamic containers a terabyte of free space as well.

The data is read-only. It cannot be replaced, only overridden by an own
measurement at the same volume size — and that is the right order,
because the size of the metadata is decided not by NTFS in general but by the
specific formatting code: the Windows build and the VeraCrypt version. Own is
always truer than factory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from .model import MIB, volume_of

PACKAGE_DATA = "containerhelper.data"
FACTORY_FILE = "factory_points.json"

#: Factory copy-slack measurements. A separate file, not a key in the same
#: one: NTFS points are taken with empty containers in minutes, while copy
#: slack measurements write gigabytes to the volume, and the two are updated
#: separately.
FACTORY_SLACK_FILE = "factory_slack.json"

#: What a factory measurement without the `filesystem` field was taken on.
#: Every one shipped so far is NTFS; the field is written out all the same,
#: so that a measurement on another filesystem cannot pass for NTFS by
#: leaving it out.
FACTORY_FILESYSTEM = "NTFS"


@dataclass(frozen=True)
class FactoryPoint:
    container_mib: int
    cluster_bytes: int
    mounted_bytes: int
    empty_free_bytes: int
    filesystem: str = FACTORY_FILESYSTEM

    @property
    def volume_bytes(self) -> int:
        return volume_of(self.container_mib * MIB)

    @property
    def metadata_bytes(self) -> int:
        """Counted from the volume size, as `Record.metadata_bytes` is."""
        return self.volume_bytes - self.empty_free_bytes


@dataclass(frozen=True)
class FactorySample:
    """Factory copy-slack measurement: a file set on an empty volume.

    Only measured values are stored, as everywhere: the slack itself is
    derived from them. It was exactly the duplication of computable fields
    that corrupted the original handwritten records.
    """

    fileset: str
    container_mib: int
    cluster_bytes: int
    mounted_bytes: int
    empty_free_bytes: int
    file_bytes: int
    file_count: int
    file_alloc_bytes: int
    left_bytes: int
    filesystem: str = FACTORY_FILESYSTEM

    @property
    def volume_bytes(self) -> int:
        return volume_of(self.container_mib * MIB)

    @property
    def copy_slack_bytes(self) -> int:
        return (self.empty_free_bytes - self.left_bytes) - self.file_alloc_bytes


@dataclass(frozen=True)
class FactoryData:
    """What the files hold, without their `source` and `note`.

    Those two are for whoever reads the file; the window says the same from
    the language catalog (`calibration.intro.factory`), in the language it
    speaks.
    """

    points: tuple[FactoryPoint, ...]
    #: Factory copy-slack measurements. Empty until they have been taken even
    #: once: an empty list is more honest than made-up numbers.
    samples: tuple[FactorySample, ...] = ()


def _empty() -> FactoryData:
    return FactoryData(points=(), samples=())


def _read_resource(name: str) -> dict:
    """Read a package resource. A missing file is not an error but emptiness.

    Through importlib.resources, not by a path on disk: in a one-file build
    the resource is unpacked into a temporary folder, and an ordinary path
    does not lead there.
    """
    try:
        from importlib.resources import files

        raw = (files(PACKAGE_DATA) / name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_samples(data: dict) -> tuple[FactorySample, ...]:
    """Factory copy-slack measurements.

    A broken file leaves them empty rather than bringing anything down. An
    empty list is a working state, not a breakage: copy-slack measurements
    appear only after a real run on a live machine.
    """
    try:
        return tuple(
            FactorySample(
                fileset=str(item.get("fileset", "")),
                container_mib=int(item["container_mib"]),
                cluster_bytes=int(item.get("cluster_bytes", 4096)),
                mounted_bytes=int(item["mounted_bytes"]),
                empty_free_bytes=int(item["empty_free_bytes"]),
                file_bytes=int(item["file_bytes"]),
                file_count=int(item["file_count"]),
                file_alloc_bytes=int(item["file_alloc_bytes"]),
                left_bytes=int(item["left_bytes"]),
                filesystem=str(item.get("filesystem", FACTORY_FILESYSTEM)),
            )
            for item in data.get("samples", ())
        )
    except (ValueError, KeyError, TypeError):
        return ()


@lru_cache(maxsize=1)
def factory_data() -> FactoryData:
    """Read factory points and copy-slack measurements from the package.

    A missing file of either kind is not an error: the program is simply left
    without the corresponding factory data.
    """
    data = _read_resource(FACTORY_FILE)
    try:
        points = tuple(
            FactoryPoint(
                container_mib=int(item["container_mib"]),
                cluster_bytes=int(item.get("cluster_bytes", 4096)),
                mounted_bytes=int(item["mounted_bytes"]),
                empty_free_bytes=int(item["empty_free_bytes"]),
                filesystem=str(item.get("filesystem", FACTORY_FILESYSTEM)),
            )
            for item in data.get("points", ())
        )
    except (ValueError, KeyError, TypeError):
        return _empty()

    return FactoryData(
        points=points,
        samples=_read_samples(_read_resource(FACTORY_SLACK_FILE)),
    )


def factory_volume(container_mib: int) -> int:
    """Volume size produced by a container of this size."""
    return volume_of(container_mib * MIB)
