"""Automatic collection of measurements: the plan, one step, the self-check.

Taking the calibration by hand means twenty-six containers: create, format,
mount, write down the numbers, unmount, delete. Several hours, and every step
can be done wrong silently. Here VeraCrypt does the same on command, and the
program reads the finished volume.

Two kinds of measurements are collected. An empty volume gives the NTFS
metadata. The same volume filled with a generated file set gives the copy
slack at several different `n` — the only way to separate the per-file slack
from the constant part. Before this collection no measurement had confirmed
it; the full run, thirty steps, measured it at 1363 B per file. The second
kind is more expensive: it writes real gigabytes to the volume, and disk space
has to be counted in advance.

No Qt: the sequence of steps and the parsing of their results are not display,
and must be tested without the interface. Progress and cancellation live in
the dialog that runs these steps one at a time.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Sequence

from .fileset import PAYLOAD_DIR, FileSet, generate
from .formatting import UNIT_AUTO, fmt_both, fmt_with_unit, size_label
from .model import (
    DEFAULT_CLUSTER_BYTES,
    DEFAULT_SAFETY_BYTES,
    MIB,
    VC_HEADER_BYTES,
    CopySlackModel,
    NtfsModel,
    SafetyModel,
    ceil_div,
    solve_container_mib,
)
from .records import Record
from .sizes import scan_paths
from .veracrypt import (
    VeraCrypt,
    VeraCryptError,
    container_name,
    orphans,
    remove_container,
)

#: The size the self-check runs at. A gigabyte is chosen because full format of
#: a gigabyte costs seconds, and of a terabyte hours.
SELF_CHECK_MIB = 1024

#: How far a dynamic container with quick format and a normal one with full
#: format may differ and still count as equal. On overlapping sizes the
#: difference was 8 KiB at 12 GiB and 3 KiB at 80 GiB — a megabyte covers this
#: ripple with room to spare and still catches a real difference, which is
#: what the self-check is for.
SELF_CHECK_TOLERANCE = MIB

#: How much larger than the calculation's prediction the container for a file
#: set is made. The prediction is recorded as is — the prediction check rests
#: on it — but the container cannot be created exactly to it: should the model
#: miss low, the file set does not fit, and instead of a measurement there is a
#: failure. The cushion distorts nothing: the measured slack does not depend on
#: the volume size, and the empty-volume metadata is measured on that very same
#: volume.
SLACK_CUSHION_MIB = 64

#: Fixed and proportional parts of the space margin on the host's free space.
#: Less is not enough: the host filesystem grows too while it is written to,
#: and a dynamic container on a full disk tears in the middle of a write.
SPACE_MARGIN_BYTES = 64 * MIB
SPACE_MARGIN_PERCENT = 5

#: Below this much free space on the host, writing stops. Separate from the
#: margin: that one is set aside before the step starts, and this one triggers
#: if some outsider ate the space along the way.
SPACE_FLOOR_BYTES = 128 * MIB

#: How many bytes of writing the creation of one file is worth — a rough
#: equivalence, and only for the progress bar.
#:
#: Needed because the time of a step does not rest on bytes alone. Ten thousand
#: one-kilobyte files take 39 MiB, but are created noticeably slower than those
#: 39 MiB are written: the cost there is in MFT records and the directory
#: index, not in the amount of data. Without the correction the bar would skip
#: such a file set instantly and then stand still on it while it runs.
#:
#: This value **never** affects the required space: real bytes are counted
#: there, and charging the disk with extra would mean skipping steps for
#: nothing.
FILE_WEIGHT_BYTES = 64 * 1024


class OutOfSpace(OSError):
    """Disk space ran out or is running out. A subclass of OSError on purpose.

    The step catches `OSError` anyway — disk failures arrive as exactly that —
    and giving a lack of space a separate branch would mean diverging from how
    the same trouble arrives from the write itself.
    """


class Stopped(Exception):
    """A cancellation noticed in the middle of writing a file set.

    Between steps cancellation works by itself, but writing four gigabytes is
    one step minutes long, and there is no reason to wait for its end to hear
    "stop": an unfinished file set is thrown away with the container anyway.
    """


# --- the course of one step ------------------------------------------------
#
# Phases are named by words, not numbers: they go straight into the line under
# the progress bar, and a person needs to see what exactly the program has
# been standing on for the third minute.

PHASE_CREATE = "создание контейнера"
PHASE_MOUNT = "монтирование"
PHASE_EMPTY = "замер пустого тома"
PHASE_WRITE = "запись файлов"
PHASE_VERIFY = "сверка содержимого"
PHASE_REMOUNT = "перемонтирование"
PHASE_LEFT = "замер остатка"
PHASE_CLEANUP = "уборка"


@dataclass(frozen=True)
class Progress:
    """What is happening inside the step right now.

    The bytes counted are the ones the step writes to the volume — progress
    is measured by them. For an empty-volume measurement there are none at all,
    and the step adds its whole estimate to the overall count when it ends.
    """

    phase: str
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0

    @property
    def detail(self) -> str:
        """A detail to the phase name. Empty means there is nothing to say.

        Bytes next to files, not instead of them: the file set «Один файл
        4 GiB» has one single file boundary, and «0 из 1» would stand still for
        the whole write. On ten thousand one-kilobyte files, the other way
        round, the file count is what reads, and the bytes creep unnoticed.
        """
        if self.phase != PHASE_WRITE or not self.files_total:
            return ""
        text = f"файлов {self.files_done} из {self.files_total}"
        if self.bytes_total:
            text += (
                f", {fmt_with_unit(self.bytes_done, UNIT_AUTO)}"
                f" из {fmt_with_unit(self.bytes_total, UNIT_AUTO)}"
            )
        return text

    @property
    def share(self) -> float:
        """What share of the step is done — by the same weight as the plan.

        Bytes and files are added together, because separately both lie: a
        bar by bytes stands still on a file set of small files, a bar by files
        on one big file. Outside the write the share is zero: how many bytes
        VeraCrypt has already laid down while creating the container is not
        visible from outside, and it is not worth guessing with the bar — the
        phase is named in words.
        """
        done = self.bytes_done + FILE_WEIGHT_BYTES * self.files_done
        total = self.bytes_total + FILE_WEIGHT_BYTES * self.files_total
        return min(done / total, 1.0) if total else 0.0


@dataclass(frozen=True)
class Step:
    """One container: create, mount, measure, clean up after itself."""

    container_mib: int
    dynamic: bool = True
    quick: bool = True
    self_check: bool = False
    #: The file set to create on the volume. None means an empty-volume
    #: measurement, that is, a point for the NTFS metadata model.
    fileset: FileSet | None = None
    #: What the calculation predicted for this file set and how much safety
    #: margin the prediction held. Zero means there was no prediction (an
    #: empty-volume measurement has nothing to be checked against).
    predicted_mib: int = 0
    predicted_safety_mib: int = 0

    @property
    def key(self) -> str:
        return self.fileset.key if self.fileset is not None else ""

    @property
    def title(self) -> str:
        if self.fileset is not None:
            return f"{self.fileset.title} — контейнер {size_label(self.container_mib)}"
        method = (
            "динамический, быстрое форматирование"
            if self.dynamic and self.quick
            else "обычный, полное форматирование"
        )
        return f"{size_label(self.container_mib)} — {method}"

    def payload_bytes(self, cluster_bytes: int = DEFAULT_CLUSTER_BYTES) -> int:
        return 0 if self.fileset is None else self.fileset.alloc_bytes(cluster_bytes)


@dataclass(frozen=True)
class Measurement:
    """What the mounted volume gave.

    The file set fields are filled only for a slack measurement. An
    empty-volume measurement has none, and the record made from it comes out
    as a calibration point — by the same sign Record recognises it by: no
    data, no left space.
    """

    container_mib: int
    mounted_bytes: int
    empty_free_bytes: int
    cluster_bytes: int
    filesystem: str
    fileset: str = ""
    fileset_title: str = ""
    file_bytes: int | None = None
    file_count: int | None = None
    file_alloc_bytes: int | None = None
    left_bytes: int | None = None
    predicted_mib: int = 0
    predicted_safety_mib: int = 0

    @property
    def ntfs_bytes(self) -> int:
        return self.mounted_bytes - self.empty_free_bytes

    @property
    def copy_slack_bytes(self) -> int | None:
        if self.left_bytes is None or self.file_alloc_bytes is None:
            return None
        return (self.empty_free_bytes - self.left_bytes) - self.file_alloc_bytes

    def as_record(self, note: str = "") -> Record:
        """The measurement as a record.

        What is computable is not written, same as everywhere else.
        """
        title = (
            f"Запас {self.fileset_title}"
            if self.fileset
            else f"Калибровка {size_label(self.container_mib)}"
        )
        return Record(
            id=title,
            container_mib=self.container_mib,
            cluster_bytes=self.cluster_bytes,
            mounted_bytes=self.mounted_bytes,
            empty_free_bytes=self.empty_free_bytes,
            file_bytes=self.file_bytes,
            file_count=self.file_count,
            file_alloc_bytes=self.file_alloc_bytes,
            left_bytes=self.left_bytes,
            filesystem=self.filesystem,
            note=note,
            predicted_mib=self.predicted_mib or None,
            predicted_safety_mib=self.predicted_safety_mib or None,
            fileset=self.fileset,
        )


@dataclass(frozen=True)
class StepResult:
    step: Step
    measurement: Measurement | None = None
    error: str = ""
    #: No going further: the self-check failed or its verdict did not agree.
    fatal: bool = False
    #: The step was not run: there was not enough disk space. Not a failure —
    #: it can be taken later, when space frees up.
    skipped: bool = False
    required_bytes: int = 0
    free_bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.measurement is not None and not self.error


# --- plan ------------------------------------------------------------------


def plan(
    sizes: Sequence[int],
    covered: Sequence[int] = (),
    self_check: bool = True,
) -> list[Step]:
    """Order of empty-volume measurements: the self-check first, then sizes.

    The self-check is the same gigabyte twice: as a dynamic container with
    quick format and as a normal one with full format. If they match, the rest
    can be run as dynamic containers, and a terabyte will not need a terabyte
    of free space. If they differ, dynamic containers cannot be trusted on this
    machine, and that has to be learned in seconds, not after three hours of
    work.

    `covered` are the container sizes that already have an own measurement;
    they are skipped so that "fill in what is missing" does not re-measure
    everything from scratch.
    """
    steps: list[Step] = []
    if self_check:
        steps.append(Step(SELF_CHECK_MIB, dynamic=True, quick=True, self_check=True))
        steps.append(Step(SELF_CHECK_MIB, dynamic=False, quick=False, self_check=True))

    done = set(covered)
    for size in sizes:
        if size in done:
            continue
        if self_check and size == SELF_CHECK_MIB:
            # The gigabyte is already taken by the self-check, and twice.
            continue
        steps.append(Step(size))
    return steps


def slack_step(
    fileset: FileSet,
    ntfs: NtfsModel | None = None,
    slack: CopySlackModel | None = None,
    safety: SafetyModel | None = None,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
    forbidden: Sequence[int] = (),
) -> Step:
    """A step for one file set: what the calculation predicts, what to create.

    The prediction is computed exactly as on the Calculation tab, including
    the choice of safety margin: otherwise the prediction check would check a
    different value from the one the program shows the user. The safety
    advice depends on the volume size, and the volume on the safety margin, so
    we first solve with the default, then refine; a second pass is not needed,
    the safety margin moves the volume by single MiB.

    The container is created larger than the prediction by a cushion — see
    SLACK_CUSHION_MIB. And by one more megabyte if the size coincided with one
    of the recommended sizes: a slack measurement must not land on a row of
    the coverage table, otherwise the Use factory button in that row would
    disable it along with the NTFS point.
    """
    ntfs = ntfs or NtfsModel()
    slack = slack or CopySlackModel()
    payload = fileset.payload(cluster_bytes)

    safety_bytes = DEFAULT_SAFETY_BYTES
    safety_mib = ceil_div(safety_bytes, MIB)
    if safety is not None:
        probe = solve_container_mib(
            payload, ntfs=ntfs, slack=slack, safety_bytes=safety_bytes
        )
        advice = safety.advise(probe.volume_bytes, payload.file_count)
        safety_bytes, safety_mib = advice.total_bytes, advice.total_mib

    predicted = solve_container_mib(
        payload, ntfs=ntfs, slack=slack, safety_bytes=safety_bytes
    )
    taken = set(forbidden)
    container_mib = predicted.container_mib + SLACK_CUSHION_MIB
    while container_mib in taken:
        container_mib += 1

    return Step(
        container_mib=container_mib,
        fileset=fileset,
        predicted_mib=predicted.container_mib,
        predicted_safety_mib=safety_mib,
    )


def slack_plan(
    filesets: Sequence[FileSet],
    ntfs: NtfsModel | None = None,
    slack: CopySlackModel | None = None,
    safety: SafetyModel | None = None,
    covered: Sequence[str] = (),
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
    forbidden: Sequence[int] = (),
) -> list[Step]:
    """Slack measurement steps, from cheap in size to expensive.

    Ordered by size, not by the list: hungry file sets hit the free-space
    limit most often, and if they were put first, one shortage would cancel
    everything that would have fitted perfectly well after.

    `covered` are the keys of file sets that already have an own measurement.
    By keys, not file counts: two file sets with `n = 1` differ in size and
    both are measured, otherwise the reconciliation "slack does not depend on
    file size" becomes impossible.
    """
    done = set(covered)
    chosen = [item for item in filesets if item.key not in done]
    chosen.sort(key=lambda item: item.alloc_bytes(cluster_bytes))
    return [
        slack_step(item, ntfs, slack, safety, cluster_bytes, forbidden)
        for item in chosen
    ]


def disk_bytes(
    step: Step,
    ntfs: NtfsModel | None = None,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> int:
    """How many bytes the step will actually lay down on the disk.

    A dynamic container lands on the disk only with its written clusters, and
    on an empty volume only metadata is written — so a terabyte measurement
    costs not a terabyte but about 136 MiB. They are predicted by the very
    model that the collection calibrates: within the range covered by
    measurements it is accurate to single megabytes.

    A normal container with full format materialises in full — in the whole
    collection exactly one step is like that, the second self-check container.
    """
    ntfs = ntfs or NtfsModel()
    if not (step.dynamic and step.quick):
        return step.container_mib * MIB
    volume = step.container_mib * MIB - VC_HEADER_BYTES
    return VC_HEADER_BYTES + ntfs.overhead(volume) + step.payload_bytes(cluster_bytes)


def required_bytes(
    step: Step,
    ntfs: NtfsModel | None = None,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> int:
    """How much free space the step needs before it can be taken on.

    What lands on the disk plus a margin: the metadata estimate is not exact
    to the byte, and the host filesystem grows too while it is written to. The
    correction for file creation is not included — it is about time, not
    space.
    """
    need = disk_bytes(step, ntfs, cluster_bytes)
    return need + max(SPACE_MARGIN_BYTES, need * SPACE_MARGIN_PERCENT // 100)


def weight_bytes(
    step: Step,
    ntfs: NtfsModel | None = None,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> int:
    """What the step costs in time, expressed in bytes of writing.

    The same as what lands on the disk, plus a correction for creating each
    file: time does not rest on bytes alone, and without it a file set of ten
    thousand small files would weigh 39 MiB yet take longer than half a
    gigabyte of large ones.
    """
    files = step.fileset.file_count if step.fileset is not None else 0
    return disk_bytes(step, ntfs, cluster_bytes) + FILE_WEIGHT_BYTES * files


def total_bytes(
    steps: Sequence[Step],
    ntfs: NtfsModel | None = None,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> int:
    """The weight of the whole plan — overall progress is measured by it.

    Not by the number of steps: a terabyte empty volume is measured in seconds,
    and a four-gigabyte file set is written for minutes, and a bar creeping in
    equal shares would lie several times over. The space margin is not
    included: it is about caution, not work.
    """
    return sum(weight_bytes(step, ntfs, cluster_bytes) for step in steps)


def self_check_verdict(fast: Measurement, slow: Measurement) -> str:
    """Empty means equivalence is confirmed.

    Otherwise the text explaining the refusal.
    """
    difference = abs(fast.ntfs_bytes - slow.ntfs_bytes)
    if difference <= SELF_CHECK_TOLERANCE:
        return ""
    return (
        f"Самопроверка не сошлась: динамический контейнер с быстрым "
        f"форматированием дал {fast.ntfs_bytes} B метаданных, обычный с полным "
        f"— {slow.ntfs_bytes} B, разница {difference} B. На этой машине "
        f"динамические контейнеры меряются иначе, и гнать по ним остальные "
        f"размеры нельзя: числа получились бы не про те контейнеры, которые "
        f"будут созданы на самом деле."
    )


# --- one step --------------------------------------------------------------


def measure(
    veracrypt: VeraCrypt,
    workdir: Path,
    step: Step,
    progress: Callable[[Progress], None] | None = None,
    check: Callable[[int], None] | None = None,
) -> Measurement:
    """Create a container, take the numbers from it and clean up after itself.

    Cleanup in finally and without exceptions: the mounted volume and the
    container file must be removed even on a crash, otherwise terabyte files
    pile up silently.
    """
    path = Path(workdir) / container_name(step.container_mib, step.key)
    letter = veracrypt.free_letter()

    def say(phase: str, **extra) -> None:
        if progress is not None:
            progress(Progress(phase, **extra))

    try:
        say(PHASE_CREATE)
        veracrypt.create(
            path, step.container_mib * MIB, dynamic=step.dynamic, quick=step.quick
        )
        say(PHASE_MOUNT)
        veracrypt.mount(path, letter)
        say(PHASE_EMPTY)
        total, free = veracrypt.volumes.usage(letter)
        empty = Measurement(
            container_mib=step.container_mib,
            mounted_bytes=total,
            empty_free_bytes=free,
            cluster_bytes=veracrypt.volumes.cluster(letter) or 0,
            filesystem=veracrypt.volumes.filesystem(letter),
        )
        if step.fileset is None:
            return empty
        return _measure_slack(veracrypt, path, letter, step, empty, say, check)
    finally:
        say(PHASE_CLEANUP)
        veracrypt.unmount_quietly(letter)
        remove_container(path)


def _measure_slack(
    veracrypt: VeraCrypt,
    path: Path,
    letter: str,
    step: Step,
    empty: Measurement,
    say: Callable[..., None],
    check: Callable[[int], None] | None,
) -> Measurement:
    """Fill the volume with the file set and measure the left space.

    Left space is measured on a freshly mounted volume, not right after the
    write. The model predicts exactly Left space — what VeraCrypt will show a
    person when they mount the container with their data — and exactly that
    state has to be measured, with all the metadata written out.
    """
    fileset = step.fileset
    cluster = empty.cluster_bytes or DEFAULT_CLUSTER_BYTES
    payload = fileset.payload(cluster)

    # The volume's cluster is read, not assumed, and on a non-standard one the
    # file set size may turn out quite different from the one the container
    # was computed for: ten thousand one-kilobyte files with a 65536 cluster
    # take not 39 MiB but 625. Better to say so than to cut the write off
    # halfway.
    if payload.alloc_bytes >= empty.empty_free_bytes:
        raise VeraCryptError(
            f"Набор «{fileset.title}» занимает {fmt_both(payload.alloc_bytes)} "
            f"при кластере {cluster} B, а на томе свободно "
            f"{fmt_both(empty.empty_free_bytes)}. Замер не начат."
        )

    root = Path(veracrypt.volumes.root(letter)) / PAYLOAD_DIR

    # Logical size, not cluster-rounded size: generate counts written bytes,
    # and the denominator must be in the same units. On one-kilobyte files in
    # 4 KiB clusters the cluster-rounded size is four times the logical one:
    # the bytes would stall at a quarter of their total, and the step's share,
    # the allowance for files included, at about 96 percent.
    written_total = fileset.logical_bytes

    def on_progress(files_done: int, bytes_done: int) -> None:
        say(
            PHASE_WRITE,
            files_done=files_done,
            files_total=payload.file_count,
            bytes_done=bytes_done,
            bytes_total=written_total,
        )

    say(PHASE_WRITE, files_total=payload.file_count, bytes_total=written_total)
    generate(root, fileset, on_progress=on_progress, check=check)

    # A reconciliation by walking the tree: what was generated may have landed
    # not as intended — a small file that fits right into its MFT record gets
    # no cluster, and the expected cluster-rounded size is then overstated, and
    # the measured slack goes negative. This has to be caught here, not sorted
    # out later with a negative number in the table.
    say(PHASE_VERIFY)
    scan = scan_paths([root], cluster)
    if scan.file_count != payload.file_count:
        raise VeraCryptError(
            f"На томе оказалось файлов {scan.file_count}, а набор «"
            f"{fileset.title}» состоит из {payload.file_count}. Замер "
            f"негоден."
        )

    say(PHASE_REMOUNT)
    veracrypt.unmount(letter)
    veracrypt.mount(path, letter)
    say(PHASE_LEFT)
    _total, left = veracrypt.volumes.usage(letter)

    return replace(
        empty,
        fileset=fileset.key,
        fileset_title=fileset.title,
        file_bytes=scan.logical_bytes,
        file_count=scan.file_count,
        file_alloc_bytes=scan.payload.alloc_bytes,
        left_bytes=left,
        predicted_mib=step.predicted_mib,
        predicted_safety_mib=step.predicted_safety_mib,
    )


# --- the whole collection --------------------------------------------------


def free_space(workdir: str | Path) -> int:
    """Free space in the working folder. Unavailable is zero: "won't fit"."""
    try:
        return shutil.disk_usage(Path(workdir)).free
    except OSError:
        return 0


@dataclass
class Collector:
    """Runs the steps one at a time and watches the self-check.

    One at a time, not all at once: the dialog has to show progress and hear
    cancellation, and the measurement of each size is saved right away — the
    collection runs for hours, and a crash in the middle must not cost
    everything already measured.
    """

    veracrypt: VeraCrypt
    workdir: Path
    steps: list[Step] = field(default_factory=list)
    #: The metadata model: it estimates the space a step will take.
    ntfs: NtfsModel = field(default_factory=NtfsModel)
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES
    #: The host's free space. Replaced in tests — the real disk is full one
    #: time and empty the next, and there is nothing to check against it.
    free_bytes: Callable[[], int] | None = None
    progress: Callable[[Progress], None] | None = None
    #: Interrupt the current step. Returning True means stop. Asked while the
    #: file set is being written: four gigabytes take minutes to write.
    should_stop: Callable[[], bool] | None = None
    #: What the self-check gave. Accumulates up to a pair, then the verdict is
    #: made.
    _checks: list[Measurement] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir)
        if self.free_bytes is None:
            self.free_bytes = lambda: free_space(self.workdir)

    def prepare(self) -> list[Path]:
        """Remove containers left over from an interrupted collection."""
        removed = []
        for path in orphans(self.workdir):
            if remove_container(path):
                removed.append(path)
        return removed

    def required(self, step: Step) -> int:
        return required_bytes(step, self.ntfs, self.cluster_bytes)

    def step_weight(self, step: Step) -> int:
        return weight_bytes(step, self.ntfs, self.cluster_bytes)

    def weight(self) -> int:
        """Weight of the whole plan in bytes — the progress bar denominator."""
        return total_bytes(self.steps, self.ntfs, self.cluster_bytes)

    def _guard(self, _bytes_done: int) -> None:
        """Checked during the write: cancellation and disk space left."""
        if self.should_stop is not None and self.should_stop():
            raise Stopped()
        if self.free_bytes() < SPACE_FLOOR_BYTES:
            raise OutOfSpace(
                f"На диске осталось меньше {fmt_both(SPACE_FLOOR_BYTES)} — "
                f"запись остановлена, чтобы не порвать том на середине."
            )

    def run_step(self, step: Step) -> StepResult:
        need = self.required(step)
        free = self.free_bytes()
        if free < need:
            return StepResult(
                step,
                error=(
                    f"не хватает места: нужно {fmt_both(need)}, свободно "
                    f"{fmt_both(free)}"
                ),
                skipped=True,
                required_bytes=need,
                free_bytes=free,
            )

        try:
            measurement = measure(
                self.veracrypt, self.workdir, step, self.progress, self._guard
            )
        except Stopped:
            # Cancellation is not a step failure: there is simply no
            # measurement, and the container is already cleaned up in finally.
            return StepResult(step, error="остановлено по требованию")
        except (VeraCryptError, OSError) as exc:
            # OSError goes here too: the volume may vanish from under our feet
            # between mounting and reading, and that is a failure of the step,
            # not of the whole collection. A failed self-check stops
            # everything: without it there is no knowing whether dynamic
            # containers can be trusted on this machine.
            return StepResult(step, error=str(exc), fatal=step.self_check)

        if not step.self_check:
            return StepResult(step, measurement)

        self._checks.append(measurement)
        if len(self._checks) < 2:
            return StepResult(step, measurement)
        verdict = self_check_verdict(*self._checks[:2])
        return StepResult(step, measurement, error=verdict, fatal=bool(verdict))
