"""Measurement records: schema, checks, JSON store.

Schema rule: only measured values are written to the file. Everything
computable (volume size, filesystem tail, NTFS metadata, used space, copy
slack) is
computed on read and never saved. In the original manual records the
contradictions appeared precisely because of duplicated computable fields.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .factory import FactoryPoint, FactorySample, factory_data
from .fileset import FILE_SETS
from .formatting import size_label
from .i18n import LANGUAGES, catalog, current, tr, tr_in
from .paths import CALIBRATION_NAME
from .sizes import SUPPORTED_FS
from .model import (
    DEFAULT_CLUSTER_BYTES,
    MIB,
    VC_HEADERS_BYTES,
    CopySlackModel,
    MetadataModel,
    Payload,
    SafetyModel,
    Solution,
    ceil_div,
    round_up,
    solve_container_mib,
    solve_raw_container_mib,
    volume_of,
)

SCHEMA_VERSION = 7

#: Schema 1 did not know file_alloc_bytes, schema 2 did not know filesystem,
#: schema 3 did not know the calibration key, schema 4 kept that key in one
#: file with the records, schema 5 left filesystem out when it was not read,
#: schema 6 wrote the names and notes of collected records as text in the
#: language of the moment. All of them are still read: a missing field means
#: exactly the old behaviour — a missing filesystem is NTFS — empty-volume
#: measurements move on read first from records to calibration, and from
#: there to their own file, and a written name that a collected record would
#: be given anyway is forgotten (`_forget_generated`). The new version is
#: always what gets written.
SUPPORTED_SCHEMAS = (1, 2, 3, 4, 5, 6, 7)

#: `Record.veracrypt` of a collection whose VeraCrypt version could not be
#: read: collection still runs then, and the record still has to say where
#: its numbers came from.
UNREAD_VERSION = "?"

#: What a version in an old collection note looks like: "1.26.24", "1.24a".
#: Anything else after "VeraCrypt" is a person's own words.
VERSION_SHAPE = re.compile(r"\d+(\.\d+)*[a-z]?")

#: Plausibility bounds for NTFS metadata. The lower one catches lost digits
#: (Cache 2 has 36 573 written instead of 36 573 184). The upper one grows
#: with the volume: on a terabyte the metadata is 136 MiB (factory point),
#: past the fixed 128 MiB, so a fixed ceiling here would give false alarms.
NTFS_MIN_BYTES = MIB
NTFS_MAX_FLOOR = 128 * MIB
NTFS_MAX_SHARE = 0.02


#: The scope an error invalidates. A record with a broken left_bytes still
#: gives a usable point for the metadata model, and there is no need to lose it
#: because of that.
SCOPE_METADATA = "metadata"
SCOPE_SLACK = "slack"
SCOPE_BOTH = "both"


#: FAT as VeraCrypt names it in its format options. Windows reports the same
#: volume as FAT32 (or FAT on a small one), and both mean one profile.
FAT = "FAT"
EXFAT = "exFAT"


@dataclass(frozen=True)
class VolumeProfile:
    """What a calibration belongs to: the filesystem and its cluster size.

    Metadata and copy slack are measured for one filesystem with one cluster,
    and a measurement of another profile says nothing about this one: exFAT
    spends a fraction of NTFS's bytes per file, and mixed in, its measurement
    would pull the NTFS copy slack down — the dangerous side. So every model
    is built from one profile's measurements, and every "the same
    measurement" — superseding, disabling, covering a size — is decided
    within a profile.
    """

    filesystem: str
    cluster_bytes: int


#: The profile every measurement so far was taken on, and the one the
#: program calculates for until the profile can be chosen.
DEFAULT_PROFILE = VolumeProfile(SUPPORTED_FS, DEFAULT_CLUSTER_BYTES)

#: A volume VeraCrypt leaves without a filesystem (`/filesystem None`). Named
#: the way VeraCrypt names it. Nothing is measured for it and nothing needs to
#: be: without a filesystem there is neither metadata nor copy slack, and the
#: container is the data plus the headers (`solve_raw_container_mib`).
NO_FILESYSTEM = VolumeProfile("None", 0)


def filesystem_name(raw: str) -> str:
    """The profile's name for the filesystem as the volume reports it.

    Empty is NTFS: the filesystem was not read before schema 3, and every
    record of that time was taken on NTFS. An unknown name is kept as is —
    it is a profile of its own, not NTFS.
    """
    name = raw.strip()
    upper = name.upper()
    if not upper or upper == SUPPORTED_FS:
        return SUPPORTED_FS
    if upper in (FAT, "FAT32"):
        return FAT
    if upper == EXFAT.upper():
        return EXFAT
    return name


def profile_of(item: "Record | FactoryPoint | FactorySample") -> VolumeProfile:
    """The profile a record or a factory measurement was taken on.

    An unread cluster (0) counts as 4 KiB on NTFS, and `validate` relies on it:
    that is NTFS's cluster on every size the program reaches. Elsewhere it
    stays 0 — exFAT's default depends on the volume size, and a guess would
    file the record under a profile it was not taken on.
    """
    return volume_profile(item.filesystem, item.cluster_bytes)


def volume_profile(filesystem: str, cluster_bytes: int) -> VolumeProfile:
    """The profile of a volume from what the volume reports, as `profile_of`."""
    name = filesystem_name(filesystem)
    if not cluster_bytes and name == SUPPORTED_FS:
        cluster_bytes = DEFAULT_CLUSTER_BYTES
    return VolumeProfile(name, cluster_bytes)


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    scope: str = SCOPE_BOTH

    def affects(self, scope: str) -> bool:
        return self.scope in (scope, SCOPE_BOTH)


@dataclass
class Record:
    id: str
    container_mib: int
    created: str = ""
    mounted_bytes: int | None = None
    empty_free_bytes: int | None = None
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES
    file_bytes: int | None = None
    file_count: int | None = None
    #: Σ ceil(size_i / cluster) × cluster, taken by walking the folder. For
    #: one file it is derived from file_bytes, for a folder it is not: the sum
    #: of logical sizes does not restore the per-cluster rounding of each file.
    file_alloc_bytes: int | None = None
    left_bytes: int | None = None
    #: Volume filesystem, read at measurement time. Empty means it was not
    #: read: a record typed in by hand, or a volume that did not answer. It
    #: counts as NTFS and is written out
    #: as NTFS — explicitly, so that the file says which profile the record
    #: calibrates.
    filesystem: str = ""
    note: str = ""
    flagged: bool = False
    #: Disabled calibration point: the numbers stay in the file, but the
    #: factory value goes into the calculation. This way a "reset to factory"
    #: loses nothing.
    disabled: bool = False
    #: What the calculation promised when the container was created from it,
    #: and how much safety margin that promise held. Stored, because they
    #: cannot be computed after the fact: both models change with every new
    #: measurement, and a recalculation would answer "what would I say today",
    #: not "what did I say then".
    predicted_mib: int | None = None
    predicted_safety_mib: int | None = None
    #: File set key, if the data was generated by automatic collection. Empty
    #: means a real copy. The distinction matters: a file set describes the
    #: machine, not the user's data, and on another machine it has to be
    #: rebuilt.
    fileset: str = ""
    #: VeraCrypt version that made the measurement, if automatic collection
    #: did. A fact about the machine, not text: the note "Automatic
    #: collection, VeraCrypt …" is built from it when shown (`shown_note`).
    veracrypt: str = ""

    def __post_init__(self) -> None:
        if not self.created:
            self.created = datetime.now().isoformat(timespec="seconds")

    # --- computable values: never stored ------------------------------------

    @property
    def container_bytes(self) -> int:
        return self.container_mib * MIB

    @property
    def volume_bytes(self) -> int:
        """The volume size: the container without the VeraCrypt headers.

        Not the measured capacity: that one is smaller by the filesystem tail
        (`tail_bytes`). This is the X axis of the metadata model and the key
        a calibration point is found by.
        """
        return volume_of(self.container_bytes)

    @property
    def tail_bytes(self) -> int | None:
        """What the filesystem holds back from its own capacity: one cluster on NTFS."""
        if self.mounted_bytes is None:
            return None
        return self.volume_bytes - self.mounted_bytes

    @property
    def metadata_bytes(self) -> int | None:
        """Everything the empty volume does not give to files, the tail included.

        Counted from the volume size rather than from the measured capacity:
        what the capacity leaves out differs between filesystems — FAT does
        not count its tables into it — and the volume size is the same for
        all of them. The measured capacity is still required: without it
        nothing checks that the container size was entered right.
        """
        if self.mounted_bytes is None or self.empty_free_bytes is None:
            return None
        return self.volume_bytes - self.empty_free_bytes

    @property
    def consumed_bytes(self) -> int | None:
        if self.empty_free_bytes is None or self.left_bytes is None:
            return None
        return self.empty_free_bytes - self.left_bytes

    @property
    def name(self) -> str:
        """The name as shown: the one given, or one built from the data.

        Collection does not write a name. "Calibration 1 GiB" and "Copy slack
        500 files of 1 KiB" say nothing the record itself does not, and
        written down they would stay in the language they were written in.
        """
        return self.id or generated_name(self)

    @property
    def shown_note(self) -> str:
        """The note as shown: the one given, or the collection's own."""
        return self.note or collection_note(self.veracrypt)

    @property
    def is_calibration_point(self) -> bool:
        """Empty-volume measurement: neither data nor left space after copying.

        No separate field is kept — the flag is derived. A calibration point
        is defined precisely by nothing having been put into the container.
        """
        return self.file_bytes is None and self.left_bytes is None

    @property
    def payload_alloc(self) -> int | None:
        """Space taken by the data itself, including rounding to clusters.

        The measured value takes priority over the derived one: for a folder
        of many files round_up(Σ size_i) is less than Σ round_up(size_i), and
        the difference would all end up in copy slack.
        """
        if self.file_alloc_bytes is not None:
            return self.file_alloc_bytes
        if self.file_bytes is None:
            return None
        return round_up(self.file_bytes, self.cluster_bytes)

    @property
    def copy_slack_measured(self) -> int | None:
        consumed = self.consumed_bytes
        alloc = self.payload_alloc
        if consumed is None or alloc is None:
            return None
        return consumed - alloc

    # --- checking the prediction after the fact ----------------------------

    @property
    def minimum_mib(self) -> int | None:
        """The smallest container this data would still have fitted into.

        The used space on the volume is `volume_bytes - left_bytes`, and it
        already includes everything: NTFS metadata with the filesystem tail,
        the cluster-rounded data and copy slack. Add the VeraCrypt headers and
        round up to MiB — and you get the Container init that would have been
        just enough.

        The estimate is slightly high: a smaller container would give a
        smaller volume, and with it less metadata. The error goes towards
        overstating the minimum, that is, the miss comes out smaller than the
        real one — the metric errs on the side of alarm, not complacency, and
        that is the right side.
        """
        if self.mounted_bytes is None or self.left_bytes is None:
            return None
        return ceil_div(self.volume_bytes - self.left_bytes + VC_HEADERS_BYTES, MIB)

    @property
    def miss_mib(self) -> int | None:
        """Promised minus the minimum that would suffice.

        Plus is overestimate, zero is an exact hit, minus means **the data
        would not have fitted**. The value is computed for the sake of that
        last case: underestimate is the only dangerous side of the
        calculation.
        """
        minimum = self.minimum_mib
        if self.predicted_mib is None or minimum is None:
            return None
        return self.predicted_mib - minimum

    @property
    def model_miss_mib(self) -> int | None:
        """The same miss minus the intentional safety margin.

        Without this number the miss is ambiguous: +5 MiB looks the same both
        when the model is exact with a 5 MiB safety margin and when the model
        underestimated by 3 and 8 MiB of safety margin hid it. The second is a
        harbinger of failure that looks healthy.
        """
        miss = self.miss_mib
        if miss is None:
            return None
        return miss - (self.predicted_safety_mib or 0)

    @property
    def forecast_checked(self) -> bool:
        """Anything to compare: prediction recorded and left space measured."""
        return self.miss_mib is not None

    # --- serialisation ------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "created": self.created,
            "container_mib": self.container_mib,
            "cluster_bytes": self.cluster_bytes,
        }
        for name in (
            "mounted_bytes",
            "empty_free_bytes",
            "file_bytes",
            "file_count",
            "file_alloc_bytes",
            "left_bytes",
            "predicted_mib",
            "predicted_safety_mib",
        ):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        data["filesystem"] = self.filesystem or SUPPORTED_FS
        if self.fileset:
            data["fileset"] = self.fileset
        if self.note:
            data["note"] = self.note
        if self.veracrypt:
            data["veracrypt"] = self.veracrypt
        if self.flagged:
            data["flagged"] = True
        if self.disabled:
            data["disabled"] = True
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Record":
        return _forget_generated(cls(
            id=str(data.get("id", "")),
            created=str(data.get("created", "")),
            container_mib=int(data["container_mib"]),
            mounted_bytes=_opt_int(data.get("mounted_bytes")),
            empty_free_bytes=_opt_int(data.get("empty_free_bytes")),
            cluster_bytes=int(data.get("cluster_bytes", DEFAULT_CLUSTER_BYTES)),
            file_bytes=_opt_int(data.get("file_bytes")),
            file_count=_opt_int(data.get("file_count")),
            file_alloc_bytes=_opt_int(data.get("file_alloc_bytes")),
            left_bytes=_opt_int(data.get("left_bytes")),
            filesystem=str(data.get("filesystem") or SUPPORTED_FS),
            note=str(data.get("note", "")),
            flagged=bool(data.get("flagged", False)),
            disabled=bool(data.get("disabled", False)),
            predicted_mib=_opt_int(data.get("predicted_mib")),
            predicted_safety_mib=_opt_int(data.get("predicted_safety_mib")),
            fileset=str(data.get("fileset", "")),
            veracrypt=str(data.get("veracrypt", "")),
        ))


def collection_note(veracrypt: str) -> str:
    """The note of a record collected by this VeraCrypt version; empty for a
    record collection did not make."""
    if not veracrypt:
        return ""
    if veracrypt == UNREAD_VERSION:
        return tr("collect.dialog.note")
    return tr("collect.dialog.note.version", version=veracrypt)


def generated_name(record: Record, language: str | None = None) -> str:
    """The name a collected record goes by; empty for any other record.

    In the current language, or in the given one when recognising a name
    written by an older version.
    """
    language = language or current()
    if record.fileset:
        titles = {item.key: item.title for item in FILE_SETS}
        title = titles.get(record.fileset)
        fileset = tr_in(language, title) if title else record.fileset
        return tr_in(language, "collect.record.slack", fileset=fileset)
    if record.is_calibration_point:
        size = size_label(record.container_mib)
        return tr_in(language, "collect.record.point", size=size)
    return ""


def _forget_generated(record: Record) -> Record:
    """Drop the name and note that schema 6 wrote as text.

    Only what collection itself would write, in any shipped language: a name
    a person gave stays. The note "Automatic collection, VeraCrypt 1.26.24"
    gives back its version, and the text is built again when shown.
    """
    if record.id and record.id in {
        generated_name(record, code) for code in LANGUAGES
    }:
        record.id = ""
    if not record.note:
        return record
    for code in LANGUAGES:
        if record.note == tr_in(code, "collect.dialog.note"):
            record.note = ""
            record.veracrypt = record.veracrypt or UNREAD_VERSION
            break
        template = catalog(code).get("collect.dialog.note.version")
        if not isinstance(template, str) or "{version}" not in template:
            continue
        head, _, tail = template.partition("{version}")
        version = record.note[len(head) : len(record.note) - len(tail)]
        if (
            record.note.startswith(head)
            and record.note.endswith(tail)
            and VERSION_SHAPE.fullmatch(version)
        ):
            record.note = ""
            record.veracrypt = record.veracrypt or version
            break
    return record


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def validate(record: Record) -> list[Issue]:
    """Checks, each catching a real class of manual input error."""
    issues: list[Issue] = []

    if record.container_mib <= 0:
        issues.append(Issue("container_mib", tr("records.issue.container_mib")))

    mounted = record.mounted_bytes
    free = record.empty_free_bytes

    # The tail and the metadata range are checked only on NTFS with a 4 KiB
    # cluster: that is the only profile measured so far. That NTFS keeps one
    # cluster back at other cluster sizes too is expected, not measured, and
    # what the other filesystems keep and spend on metadata is not known yet.
    # A check that fired on a correct measurement would flag it and throw it
    # out of its own profile's calibration.
    is_measured = profile_of(record) == DEFAULT_PROFILE
    tail = record.tail_bytes
    if tail is not None:
        if tail < 0:
            issues.append(
                Issue(
                    "tail_negative",
                    tr(
                        "records.issue.tail_negative",
                        mounted=mounted,
                        volume=record.volume_bytes,
                    ),
                    SCOPE_METADATA,
                )
            )
        elif is_measured and tail != DEFAULT_CLUSTER_BYTES:
            issues.append(
                Issue(
                    "tail_unusual",
                    tr(
                        "records.issue.tail_unusual",
                        tail=tail,
                        cluster=DEFAULT_CLUSTER_BYTES,
                    ),
                    SCOPE_METADATA,
                )
            )

    if mounted is not None and free is not None:
        if free >= mounted:
            issues.append(
                Issue(
                    "free_ge_mounted",
                    tr("records.issue.free_ge_mounted", free=free, mounted=mounted),
                )
            )
        elif is_measured:
            ntfs = record.volume_bytes - free
            ceiling = max(NTFS_MAX_FLOOR, int(record.volume_bytes * NTFS_MAX_SHARE))
            if not (NTFS_MIN_BYTES <= ntfs <= ceiling):
                issues.append(
                    Issue(
                        "ntfs_range",
                        tr(
                            "records.issue.ntfs_range",
                            ntfs=ntfs,
                            low=NTFS_MIN_BYTES,
                            high=ceiling,
                        ),
                        SCOPE_METADATA,
                    )
                )

    consumed = record.consumed_bytes
    alloc = record.payload_alloc
    if consumed is not None and alloc is not None and consumed < alloc:
        issues.append(
            Issue(
                "consumed_lt_file",
                tr("records.issue.consumed_lt_file", consumed=consumed, alloc=alloc),
                SCOPE_SLACK,
            )
        )

    alloc_measured = record.file_alloc_bytes
    if alloc_measured is not None:
        if record.file_bytes is not None and alloc_measured < record.file_bytes:
            issues.append(
                Issue(
                    "alloc_lt_logical",
                    tr(
                        "records.issue.alloc_lt_logical",
                        alloc=alloc_measured,
                        logical=record.file_bytes,
                    ),
                    SCOPE_SLACK,
                )
            )
        if record.cluster_bytes > 0 and alloc_measured % record.cluster_bytes:
            issues.append(
                Issue(
                    "alloc_not_aligned",
                    tr(
                        "records.issue.alloc_not_aligned",
                        alloc=alloc_measured,
                        cluster=record.cluster_bytes,
                    ),
                    SCOPE_SLACK,
                )
            )

    if record.file_count is not None:
        if record.file_count < 1:
            issues.append(
                Issue(
                    "file_count",
                    tr("records.issue.file_count"),
                    SCOPE_SLACK,
                )
            )
        elif record.file_bytes is not None and record.file_count > record.file_bytes:
            issues.append(
                Issue(
                    "file_count_gt_bytes",
                    tr(
                        "records.issue.file_count_gt_bytes",
                        count=record.file_count,
                        bytes=record.file_bytes,
                    ),
                    SCOPE_SLACK,
                )
            )

    return issues


def is_usable(record: Record, scope: str = SCOPE_BOTH) -> bool:
    """Whether the record is fit for calibrating the given model.

    What is checked is not "are there any errors at all" but whether they
    touch exactly the value that is taken from the record.
    """
    if record.flagged:
        return False
    if scope == SCOPE_BOTH:
        return not validate(record)
    return not any(issue.affects(scope) for issue in validate(record))


def gives_metadata_point(
    record: Record, profile: VolumeProfile = DEFAULT_PROFILE
) -> bool:
    """Whether the record puts a point on this profile's metadata curve.

    Also what decides whether it covers a size: a record that does not reach
    the curve must not push the factory point at that size off it either.
    The volume size alone is the same for every filesystem and cluster, so
    without the profile an exFAT or an 8 KiB record would land on a factory
    NTFS size and leave the curve one node short.
    """
    return (
        profile_of(record) == profile
        and record.metadata_bytes is not None
        and is_usable(record, SCOPE_METADATA)
    )


def gives_slack_sample(
    record: Record, profile: VolumeProfile = DEFAULT_PROFILE
) -> bool:
    """Whether the record is a copy-slack measurement of this profile."""
    return (
        profile_of(record) == profile
        and record.copy_slack_measured is not None
        and is_usable(record, SCOPE_SLACK)
    )


def metadata_points(
    records: Iterable[Record], profile: VolumeProfile = DEFAULT_PROFILE
) -> list[tuple[int, int]]:
    """Points (volume size → metadata) for calibrating MetadataModel."""
    return [
        (record.volume_bytes, record.metadata_bytes)
        for record in records
        if gives_metadata_point(record, profile)
    ]


def slack_samples(
    records: Iterable[Record], profile: VolumeProfile = DEFAULT_PROFILE
) -> list[tuple[int, int]]:
    """Pairs (file count → measured slack) for calibrating CopySlackModel."""
    return [
        (record.file_count or 1, record.copy_slack_measured)
        for record in records
        if gives_slack_sample(record, profile)
    ]


def build_models(
    records: Sequence[Record], profile: VolumeProfile = DEFAULT_PROFILE
) -> tuple[MetadataModel, CopySlackModel]:
    """Build both models of the profile from the accumulated records."""
    return (
        MetadataModel(metadata_points(records, profile)),
        CopySlackModel.calibrate(slack_samples(records, profile)),
    )


def missing_calibration(
    ntfs: MetadataModel, slack: CopySlackModel
) -> tuple[str, ...]:
    """Which of the two models has nothing of its own profile to stand on.

    An uncalibrated model is not empty: it falls back to the defaults, and
    the defaults are NTFS with 4 KiB — `19 MiB + 4 KiB + 0.17 %` of the
    volume and 1536 B per file. On another profile they are not a cautious guess but a
    guess about another filesystem: exFAT with a 1 MiB cluster spends a whole
    cluster on a directory where the NTFS default budgets 192 KiB. So a
    profile calculates only on its own measurements, and this says what is
    missing: the metadata needs two points, the copy slack a per-file slope
    fitted from its own measurements. That takes two different file counts,
    and a positive slope across them: with one count, or with a slope that
    came out flat, the per-file slack would again be the NTFS default.

    Scopes, not sentences: the words belong to the window, and the scopes
    are the ones `Issue` already uses.
    """
    missing = []
    if not ntfs.calibrated:
        missing.append(SCOPE_METADATA)
    if not slack.per_file_fitted:
        missing.append(SCOPE_SLACK)
    return tuple(missing)


class Uncalibrated(ValueError):
    """The profile has too few measurements of its own to calculate on."""

    def __init__(self, profile: VolumeProfile, missing: tuple[str, ...]) -> None:
        super().__init__(f"{profile}: nothing measured for {', '.join(missing)}")
        self.profile = profile
        self.missing = missing


def solve_for_profile(
    payload: Payload,
    profile: VolumeProfile,
    ntfs: MetadataModel,
    slack: CopySlackModel,
    safety_bytes: int,
) -> Solution:
    """Solve for the container on the given profile, or refuse.

    The models must be the profile's own (`Store.models(profile)`). A volume
    without a filesystem needs none of them, whatever cluster is attached to
    it. Any other profile without its own calibration raises `Uncalibrated`
    rather than borrowing the NTFS defaults: a number that is wrong for this
    filesystem looks exactly like a right one, and a refusal with a reason is
    the only answer that cannot be taken for a result.

    The payload must be rounded to the profile's cluster. Rounded to 4 KiB
    and solved on exFAT with 32 KiB, it loses up to 28 KiB per file — an
    underestimate that the models, calibrated on the right cluster, cannot
    see. A mismatch is a programming error, not a state of the data, so it
    raises `ValueError`.
    """
    if profile.filesystem == NO_FILESYSTEM.filesystem:
        return solve_raw_container_mib(payload, safety_bytes)
    if profile.cluster_bytes and payload.cluster_bytes != profile.cluster_bytes:
        raise ValueError(
            f"payload rounded to {payload.cluster_bytes} B, "
            f"profile {profile} has {profile.cluster_bytes} B"
        )
    missing = missing_calibration(ntfs, slack)
    if missing:
        raise Uncalibrated(profile, missing)
    return solve_container_mib(
        payload, ntfs=ntfs, slack=slack, safety_bytes=safety_bytes
    )


def build_safety(
    records: Sequence[Record],
    factory_volumes: set[int] | None = None,
    profile: VolumeProfile = DEFAULT_PROFILE,
) -> SafetyModel:
    """Build the safety margin model from the accumulated records.

    The deviations are taken from the leave-one-out check: only it shows how
    the model behaves where there was no point. Scaling them to the width of
    the segment in question is left to SafetyModel — the raw deviation is
    taken on a gap about twice as wide as the real one.

    Beyond the edge of the measured range there is no local evidence at all,
    and the largest underestimate over all records goes there — deliberately
    cautious.
    """
    checks = metadata_cross_check(records, profile)
    deviations = [
        (check.record.volume_bytes, check.deviation, check.record.name)
        for check in checks
        if check.record.mounted_bytes
    ]
    slack_deviations = [
        (check.record.file_count or 1, check.deviation, check.record.name)
        for check in slack_cross_check(records, profile)
    ]
    return SafetyModel(
        ntfs=MetadataModel(metadata_points(records, profile)),
        ntfs_deviations=deviations,
        slack_deviations=slack_deviations,
        extrapolation_bytes=worst_shortfall(checks),
        factory_volumes=factory_volumes or set(),
    )


@dataclass(frozen=True)
class Check:
    """How well the model would have hit the record without seeing it."""

    record: Record
    measured: int
    predicted: int
    calibrated: bool

    @property
    def deviation(self) -> int:
        """A positive value means the model underestimated, i.e. missed low."""
        return self.measured - self.predicted


def metadata_cross_check(
    records: Sequence[Record], profile: VolumeProfile = DEFAULT_PROFILE
) -> list[Check]:
    """Check the metadata model, leaving the checked record out of calibration.

    Without leaving it out the check is meaningless: a piecewise-linear model
    passes exactly through its points, and the deviation would always be zero.
    """
    checks = []
    for index, record in enumerate(records):
        if not gives_metadata_point(record, profile):
            continue
        others = [*records[:index], *records[index + 1 :]]
        model = MetadataModel(metadata_points(others, profile))
        checks.append(
            Check(
                record=record,
                measured=record.metadata_bytes,
                predicted=model.overhead(record.volume_bytes),
                calibrated=model.calibrated,
            )
        )
    return checks


def slack_cross_check(
    records: Sequence[Record], profile: VolumeProfile = DEFAULT_PROFILE
) -> list[Check]:
    """The same for copy slack."""
    checks = []
    for index, record in enumerate(records):
        if not gives_slack_sample(record, profile):
            continue
        others = [*records[:index], *records[index + 1 :]]
        model = CopySlackModel.calibrate(slack_samples(others, profile))
        checks.append(
            Check(
                record=record,
                measured=record.copy_slack_measured,
                predicted=model.slack(record.file_count or 1),
                calibrated=model.calibrated,
            )
        )
    return checks


def worst_shortfall(checks: Sequence[Check]) -> int:
    """Largest underestimate. Zero means the model underestimated nowhere."""
    return max((check.deviation for check in checks), default=0)


def slack_key(record: Record) -> str:
    """What tells copy-slack measurements apart — and what supersedes what.

    The key is the file set, not the file count. The file count would not do:
    two sets with `n = 1` are deliberately made different in size (64 MiB
    and 4 GiB), to compare them with each other and check that copy slack
    does not depend on file size. A key by `n` made them mutually exclusive —
    on the first real run the second silently ate the first together with
    its NTFS point, that is, the supersede rule destroyed exactly the
    reconciliation that both sets exist for.

    A measurement taken by hand has no file set, and it is still identified
    by the file count: two manual measurements at the same `n` are two
    measurements of the same thing.
    """
    return record.fileset or f"n={record.file_count}"


def _measurement_key(record: Record) -> tuple[VolumeProfile, bool, int | str]:
    """What makes two machine measurements the same one.

    A point by its volume, a copy-slack measurement by its file set, both
    within the profile. Superseding and the move from the old store agree on
    it: a key by volume alone let an exFAT point, or a copy-slack
    measurement, silently drop an NTFS point on the same volume.
    """
    if record.is_calibration_point:
        return profile_of(record), True, record.volume_bytes
    return profile_of(record), False, slack_key(record)


def factory_slack_record(sample: FactorySample) -> Record:
    """A factory copy-slack measurement as a record. Same numbers as the file.

    Such a record serves both models at once: its empty volume is measured,
    so it is also an NTFS point. That is by design — the measurement was
    taken on a real volume, and throwing away its metadata would be wasteful.
    """
    titles = {item.key: item.title for item in FILE_SETS}
    known = titles.get(sample.fileset)
    title = tr(known) if known else sample.fileset
    return Record(
        id=tr("records.factory_slack.id", title=title).strip(),
        created="",
        container_mib=sample.container_mib,
        cluster_bytes=sample.cluster_bytes,
        mounted_bytes=sample.mounted_bytes,
        empty_free_bytes=sample.empty_free_bytes,
        file_bytes=sample.file_bytes,
        file_count=sample.file_count,
        file_alloc_bytes=sample.file_alloc_bytes,
        left_bytes=sample.left_bytes,
        filesystem=sample.filesystem,
        fileset=sample.fileset,
    )


def factory_points(profile: VolumeProfile = DEFAULT_PROFILE) -> list[FactoryPoint]:
    """Factory empty-volume measurements of the profile."""
    return [point for point in factory_data().points if profile_of(point) == profile]


def factory_samples(profile: VolumeProfile = DEFAULT_PROFILE) -> list[FactorySample]:
    """Factory copy-slack measurements of the profile."""
    return [
        sample for sample in factory_data().samples if profile_of(sample) == profile
    ]


@dataclass(frozen=True)
class Forecast:
    """Summary of checking the prediction after the fact.

    Computed over the records that have both the calculation's prediction
    and a measured left space. Everything else has nothing to check against.
    """

    checked: int
    worst_miss: int
    worst_model_miss: int
    #: Records where the promised container would not have been enough.
    #: Empty means the calculation never underestimated, and that is the main
    #: thing to know.
    short: tuple[str, ...]

    @property
    def any_short(self) -> bool:
        return bool(self.short)


def forecast(records: Iterable[Record]) -> Forecast:
    """How the calculation did on the records it can be compared against."""
    checked = [record for record in records if record.forecast_checked]
    if not checked:
        return Forecast(0, 0, 0, ())
    return Forecast(
        checked=len(checked),
        worst_miss=min(record.miss_mib for record in checked),
        worst_model_miss=min(record.model_miss_mib for record in checked),
        short=tuple(record.name for record in checked if record.miss_mib < 0),
    )


class StoreError(Exception):
    """The store is unavailable or corrupted."""


def calibration_file_for(records_file: str | os.PathLike[str]) -> Path:
    """The measurements file next to the records file, with a fixed name.

    The name is fixed, not derived from the records file name: measurements
    describe the machine, not a set of records, and two records files in one
    folder must share one calibration rather than breed two copies of the
    same numbers.
    """
    return Path(records_file).with_name(CALIBRATION_NAME)


def _read_json(path: Path) -> dict[str, Any]:
    """Read the file, check the schema version. A missing file is no error."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(
            tr(
                "records.error.corrupt",
                path=path,
                error=exc,
                backup=path.with_suffix(path.suffix + ".bak"),
            )
        ) from exc
    except OSError as exc:
        raise StoreError(tr("records.error.read", path=path, error=exc)) from exc

    version = raw.get("schema")
    if version not in SUPPORTED_SCHEMAS:
        supported = ", ".join(str(item) for item in SUPPORTED_SCHEMAS)
        raise StoreError(
            tr("records.error.schema", version=repr(version), supported=supported)
        )
    return raw


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic write with a single .bak backup."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise StoreError(tr("records.error.write", path=path, error=exc)) from exc


@dataclass
class Store:
    """Two JSON files: copy records and empty-volume measurements.

    Separate, because these are different values with different lifetimes.
    Copy records describe the data and move together with it; measurements
    describe the machine — the Windows build and the VeraCrypt version — and
    are wrong on another machine. In one file they had to travel together,
    and someone else's calibration arrived posing as your own.

    One store owns both files: the calculation stands on them together, and
    there would be nowhere to read them separately.
    """

    path: Path
    records: list[Record] = field(default_factory=list)
    calibration: list[Record] = field(default_factory=list)
    #: Where the measurements went. Empty means the neighbouring file with the
    #: fixed name.
    calibration_path: Path | None = None

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.calibration_path is None:
            self.calibration_path = calibration_file_for(self.path)
        else:
            self.calibration_path = Path(self.calibration_path)
        #: The measurements came from the old single-file store and have not
        #: yet been written to their own place. While that is so, the old file
        #: holds a copy of them.
        self.migrated = False

    @property
    def backup_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".bak")

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        calibration_path: str | os.PathLike[str] | None = None,
    ) -> "Store":
        path = Path(path)
        points_path = (
            Path(calibration_path)
            if calibration_path is not None
            else calibration_file_for(path)
        )

        raw = _read_json(path)
        version = int(raw.get("schema", SCHEMA_VERSION))
        records = [Record.from_json(item) for item in raw.get("records", [])]

        # Measurements from the old single-file store. Before schema four they
        # were mixed in with the records, in schema four they had their own
        # key in the same file. The numbers do not change, only the place does.
        legacy = [Record.from_json(item) for item in raw.get("calibration", [])]
        if version < 4:
            legacy.extend(record for record in records if record.is_calibration_point)
            records = [record for record in records if not record.is_calibration_point]

        points = [
            Record.from_json(item)
            for item in _read_json(points_path).get("calibration", [])
        ]
        # The own measurements file outranks the arrived one: if the user has
        # already taken a point on this volume, the old copy from the records
        # file does not supersede it.
        covered = {_measurement_key(record) for record in points}
        points.extend(
            record for record in legacy if _measurement_key(record) not in covered
        )

        store = cls(
            path=path,
            records=records,
            calibration=points,
            calibration_path=points_path,
        )
        store.migrated = bool(legacy)
        return store

    def save(self) -> None:
        _write_json(
            self.path,
            {
                "schema": SCHEMA_VERSION,
                "records": [record.to_json() for record in self.records],
            },
        )
        # An empty measurements file is not created: on a new machine there are
        # no own points at all, and there is no reason to put a dummy next to
        # the records. One that has become empty is rewritten, otherwise the
        # deleted measurements would come back on the next read.
        if self.calibration or self.calibration_path.exists():
            _write_json(
                self.calibration_path,
                {
                    "schema": SCHEMA_VERSION,
                    "calibration": [record.to_json() for record in self.calibration],
                },
            )
        self.migrated = False

    def calibration_records(
        self, profile: VolumeProfile = DEFAULT_PROFILE
    ) -> list[Record]:
        """The profile's points in the model: own enabled ones plus factory.

        An own measurement supersedes the factory one at the same volume size.
        Not "the larger of the two", as in MetadataModel._dedupe: the factory value
        was taken on another machine, and a smaller own value is truer than
        any foreign one.
        """
        own = [
            record
            for record in self.calibration
            if not record.disabled and profile_of(record) == profile
        ]
        covered = {
            record.volume_bytes
            for record in [*own, *self.records]
            if gives_metadata_point(record, profile)
        }

        filled = list(own)
        for point in factory_points(profile):
            if point.volume_bytes in covered:
                continue
            filled.append(
                Record(
                    id=tr("records.factory_point.id", mib=point.container_mib),
                    created="",
                    container_mib=point.container_mib,
                    cluster_bytes=point.cluster_bytes,
                    mounted_bytes=point.mounted_bytes,
                    empty_free_bytes=point.empty_free_bytes,
                    filesystem=point.filesystem,
                )
            )

        # Factory copy-slack measurements are superseded by own ones by file
        # count, not by volume size: copy slack depends on n, and an own point
        # at the same n is truer than a foreign one for exactly the same reason
        # as with the metadata.
        counts = {
            record.file_count
            for record in [*self.records, *own]
            if record.file_count
            and record.copy_slack_measured is not None
            and profile_of(record) == profile
        }
        for sample in factory_samples(profile):
            if sample.file_count in counts:
                continue
            filled.append(factory_slack_record(sample))
        return filled

    def all_for_model(self, profile: VolumeProfile = DEFAULT_PROFILE) -> list[Record]:
        """Everything the profile's model is built on: copy records and points."""
        return [
            *(record for record in self.records if profile_of(record) == profile),
            *self.calibration_records(profile),
        ]

    def calibration_points(self) -> list[Record]:
        """Own empty-volume measurements — what the coverage table shows."""
        return [record for record in self.calibration if record.is_calibration_point]

    def slack_measurements(self) -> list[Record]:
        """Own copy-slack measurements.

        They live in the same file as the calibration points: both values
        describe the machine, not the data, and are equally wrong on another
        machine. No separate field tells them apart — the flag is derived,
        like everything else computable: a copy-slack measurement has both
        data and left space, so it does not count as a calibration point.
        """
        return [
            record for record in self.calibration if not record.is_calibration_point
        ]

    def put_calibration(self, record: Record) -> None:
        """Put a measurement, superseding the previous one of the same kind.

        Superseding is separate: a calibration point replaces the point on
        the same volume, a copy-slack measurement replaces the measurement of
        the same file set. A shared key by `volume_bytes` would knock out an
        NTFS point with a copy-slack measurement taken at the same volume
        size, and vice versa — silently, because both records look equally
        legitimate. And both keys hold within the profile: an exFAT point on
        a volume already measured on NTFS is a second curve's node, not a
        retake.
        """
        key = _measurement_key(record)
        keep = [item for item in self.calibration if _measurement_key(item) != key]
        self.calibration = [*keep, record]

    def factory_volumes(self, profile: VolumeProfile = DEFAULT_PROFILE) -> set[int]:
        """The profile's volume sizes covered only by factory data.

        Copy-slack measurements also give a metadata point — the empty volume
        is measured before the files are written — so the factory ones among
        them count here on a par with the points: a segment whose both ends
        are foreign must get the factory margin regardless of which
        collection took the foreign numbers.
        """
        own = {
            record.volume_bytes
            for record in [*self.records, *self.calibration]
            if gives_metadata_point(record, profile) and not record.disabled
        }
        volumes = {point.volume_bytes for point in factory_points(profile)}
        volumes.update(sample.volume_bytes for sample in factory_samples(profile))
        return {volume for volume in volumes if volume not in own}

    def models(
        self, profile: VolumeProfile = DEFAULT_PROFILE
    ) -> tuple[MetadataModel, CopySlackModel]:
        return build_models(self.all_for_model(profile), profile)

    def safety(self, profile: VolumeProfile = DEFAULT_PROFILE) -> SafetyModel:
        return build_safety(
            self.all_for_model(profile), self.factory_volumes(profile), profile
        )

    def add(self, record: Record) -> None:
        self.records.append(record)

    def replace_at(self, index: int, record: Record) -> None:
        self.records[index] = record

    def index_of(self, record: Record) -> int | None:
        """Where this very record lies. By identity, not by equality.

        Edit windows are modeless, and while one of them is open, the list has
        time to change: a neighbouring record was deleted, a new one added —
        the index taken at opening already points to someone else's row.
        Equality does not work here either: two records with identical fields
        are common, `Record` compares by value, and the edit would go into
        whichever came first.

        None means the record is no longer in the store: it was deleted from
        another window.
        """
        for index, item in enumerate(self.records):
            if item is record:
                return index
        return None

    def remove_at(self, index: int) -> None:
        del self.records[index]

    def measure_into(self, record: Record, total: int, free: int) -> Record:
        """Spread one volume measurement over the record's fields.

        An empty volume gives the capacity and the free space; the same
        measurement after copying gives the left space. Which field gets
        filled is decided by whether empty_free_bytes is already filled.
        """
        if record.empty_free_bytes is None:
            return replace(record, mounted_bytes=total, empty_free_bytes=free)
        return replace(record, left_bytes=free)
