"""Overhead models and the VeraCrypt container size calculation.

The whole module works in whole bytes. The only place a float appears is the
model coefficients; the result is immediately rounded up to an integer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

MIB = 1024 * 1024

#: VeraCrypt headers: 128 KiB at the start of the container (the header and
#: the hidden volume's header) and their 128 KiB backup at the end. The
#: volume size is the container minus exactly this, whatever the filesystem.
#:
#: The container never matched the volume capacity by this alone: on three
#: containers, 8050, 10475 and 11130 MiB, `container_bytes - mounted_bytes`
#: came out 266 240 B to the byte — 4096 B more. Those 4096 B are the
#: filesystem's, not VeraCrypt's: NTFS with a 4 KiB cluster reports a
#: capacity one cluster short of its volume. They used to be counted into a
#: single header constant, which held only as long as every volume was NTFS
#: with that one cluster size; now they are the filesystem tail
#: (`Record.tail_bytes`) and go into the metadata, where the filesystem's
#: other overhead lies.
VC_HEADERS_BYTES = 262_144

#: Default NTFS metadata model: 19 MiB + 4 KiB + 0.17 % of the volume size.
#: The largest underestimate on the three original records (Cache 1, 2 and 4)
#: is 554 330 B (0.53 MiB), on Cache 1. The 19 MiB were fitted while the
#: filesystem tail was still counted into the header; the 4 KiB is that tail,
#: moved into the metadata along with it, so that the model does not lose it.
DEFAULT_NTFS_BASE = 19 * MIB + 4096
DEFAULT_NTFS_RATE = 0.0017

#: Copy slack when there are no measurements at all — neither own nor
#: factory. Both parts are deliberately cautious, but for different reasons.
#:
#: The per-file slack is measured: on the series of 500, 5 000 and 10 000
#: files the rate came out at 1363 B per file and held within two bytes. It
#: also breaks down physically — 1024 B per MFT record plus ~339 B per entry
#: in the directory index. DEFAULT_SLACK_PER_FILE is that rate with headroom,
#: 1536 B, because the second half depends on the file name length: the
#: measurements were taken on 34-character names, and in real data they can be
#: twice as long.
#:
#: The constant part stayed as it was, although the synthetic measurement at
#: n = 1 gave only 4096 B — one cluster. The discrepancy with the manual
#: records (114 688 and 143 360 B at the same n = 1) is not explained: those
#: were copied with Explorer, not written by the program, and what exactly
#: Windows adds around a real copy is unknown. The user copies with Explorer,
#: so the larger of the two is kept here: 192 KiB costs nothing next to a
#: safety margin of megabytes, and underestimate is the only dangerous side.
DEFAULT_SLACK_BASE = 192 * 1024
DEFAULT_SLACK_PER_FILE = 1536

DEFAULT_SAFETY_BYTES = 4 * MIB
DEFAULT_CLUSTER_BYTES = 4096

#: Lower bound of the NTFS metadata prediction. Protects against degenerate
#: extrapolation from two close, noisy points.
MIN_NTFS_BYTES = MIB


def ceil_div(value: int, divisor: int) -> int:
    """Division rounding up, on integers only."""
    return -(-value // divisor)


def round_up(value: int, unit: int) -> int:
    """Round up to a multiple of unit."""
    return ceil_div(value, unit) * unit


class MetadataModel:
    """NTFS metadata as a function of the volume size (`volume_of`).

    The metadata here includes the filesystem tail: it is what the empty
    volume does not give to files, `volume - empty free space`.

    Fewer than two points — the default affine model. Two or more —
    piecewise-linear interpolation over the measured points, and outside the
    range the baseline slope anchored to the nearest measured point (see
    `_extend`).

    The piecewise-linear form was chosen because the dependence is not
    proportional: $LogFile barely grows with the volume and hits a ceiling of
    64 MiB, only $Bitmap grows linearly. One straight line through 1 GiB and
    100 GiB would give a systematic error.
    """

    def __init__(
        self,
        points: Iterable[tuple[int, int]] | None = None,
        base: int = DEFAULT_NTFS_BASE,
        rate: float = DEFAULT_NTFS_RATE,
    ) -> None:
        self.base = base
        self.rate = rate
        self.points = self._dedupe(points or ())

    @staticmethod
    def _dedupe(points: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
        """Collapse matching volume sizes, keeping the largest value.

        The largest, not the average: the model's error must go to the safe
        side.
        """
        best: dict[int, int] = {}
        for volume, overhead in points:
            if volume <= 0 or overhead < 0:
                continue
            if volume not in best or overhead > best[volume]:
                best[volume] = overhead
        return sorted(best.items())

    @property
    def calibrated(self) -> bool:
        return len(self.points) >= 2

    @property
    def covered_range(self) -> tuple[int, int] | None:
        """Range of volume sizes covered by measurements."""
        if not self.points:
            return None
        return self.points[0][0], self.points[-1][0]

    def is_extrapolation(self, volume_bytes: int) -> bool:
        span = self.covered_range
        if span is None or not self.calibrated:
            return True
        return not (span[0] <= volume_bytes <= span[1])

    def overhead(self, volume_bytes: int) -> int:
        if not self.calibrated:
            value = self.base + math.ceil(self.rate * volume_bytes)
        else:
            value = self._interpolate(volume_bytes)
        return max(value, MIN_NTFS_BYTES)

    def _interpolate(self, volume_bytes: int) -> int:
        points = self.points
        first, last = points[0], points[-1]

        if volume_bytes < first[0]:
            return self._extend(first, volume_bytes)
        if volume_bytes > last[0]:
            return self._extend(last, volume_bytes)

        index = 0
        while points[index + 1][0] < volume_bytes:
            index += 1
        (x0, y0), (x1, y1) = points[index], points[index + 1]
        return y0 + ceil_div((y1 - y0) * (volume_bytes - x0), x1 - x0)

    def _segment_slopes(self) -> list[float]:
        points = self.points
        return [
            (points[i + 1][1] - points[i][1]) / (points[i + 1][0] - points[i][0])
            for i in range(len(points) - 1)
        ]

    def interpolation_bound(self, volume_bytes: int) -> int:
        """How much the model can underestimate at exactly this point.

        Between two measurements the model draws a straight line, and the
        real dependence is not a straight line. Where it is concave — and
        above 16 GiB it is exactly that, because the parts of the metadata hit
        their ceilings one after another — the chord passes under the curve,
        and that gap is the risk. Where it is convex, the chord runs above.

        The bound assumes the curve stays under two straight lines: one from
        the left end with the slope of the previous segment and one from the
        right end with the slope of the next. The nearer of them minus the
        chord gives the bound. Beyond the last segment the slope is taken as
        zero: this is the most cautious assumption, and it is also close to
        the truth — on large volumes only $Bitmap grows.

        The shape between two points is only guessed from the neighbouring
        segments, so a zero here does not mean the model cannot
        underestimate. Below 8 GiB the curve is a staircase: $LogFile changes
        size in steps at discrete thresholds, and a step can hide between any
        two points. A measurement at 1610 MiB lay 202 672 B above the chord
        where this bound said zero, and only MIN_SAFETY_BYTES covered it; a
        later one at 1536 MiB lay 233 472 B above. That floor has to stay.

        Outside the measured range zero is returned: there it is not
        interpolation but extrapolation at work, and its error is estimated
        differently.
        """
        if not self.calibrated:
            return 0
        points = self.points
        if not (points[0][0] <= volume_bytes <= points[-1][0]):
            return 0

        slopes = self._segment_slopes()
        index = 0
        while points[index + 1][0] < volume_bytes:
            index += 1
        (x0, y0), (x1, y1) = points[index], points[index + 1]

        left_slope = slopes[index - 1] if index else slopes[index]
        right_slope = slopes[index + 1] if index + 1 < len(slopes) else 0.0

        ceiling = min(
            y0 + left_slope * (volume_bytes - x0),
            y1 - right_slope * (x1 - volume_bytes),
        )
        chord = y0 + (y1 - y0) * (volume_bytes - x0) / (x1 - x0)
        return max(0, math.ceil(ceiling - chord))

    def _extend(self, anchor: tuple[int, int], volume_bytes: int) -> int:
        """Extend the dependence beyond the measured range.

        The slope taken is not the fitted one but the baseline one — the same
        as in the default model — anchored to the nearest measured point. A
        fitted slope beyond the data is unreliable: measurements often stand
        close together, and their local slope, carried many times further
        than its own span, misses by an order of magnitude. The leave-one-out
        check gave 10.8 MiB of underestimate on this with a safety margin of
        4 MiB.

        Inside the range the measurements are right; outside it is the
        physical growth (`$Bitmap` is linear in the volume size, `$LogFile`
        hits a ceiling), shifted so as to meet the edge of the measured range.
        """
        x_anchor, y_anchor = anchor
        return y_anchor + math.ceil(self.rate * (volume_bytes - x_anchor))


class CopySlackModel:
    """Space the files themselves take beyond their cluster-rounded size.

    An MFT record per file, growth of the directory indexes, the service
    structures of the first write on the volume.
    """

    def __init__(
        self,
        base: int = DEFAULT_SLACK_BASE,
        per_file: int = DEFAULT_SLACK_PER_FILE,
        sample_count: int = 0,
        file_counts: tuple[int, ...] = (),
        per_file_fitted: bool = False,
    ) -> None:
        self.base = base
        self.per_file = per_file
        self.sample_count = sample_count
        self.file_counts = file_counts
        #: The per-file slack came from the measurements, not the default.
        #: Two different n are not enough for that: a slope that came out
        #: zero or negative leaves the default in place.
        self.per_file_fitted = per_file_fitted

    @property
    def calibrated(self) -> bool:
        return self.sample_count > 0

    @property
    def per_file_calibrated(self) -> bool:
        """The per-file slack can be separated only with different n."""
        return len(set(self.file_counts)) >= 2

    def slack(self, file_count: int) -> int:
        return self.base + self.per_file * max(file_count, 1)

    @classmethod
    def calibrate(cls, samples: Sequence[tuple[int, int]]) -> "CopySlackModel":
        """Fit the coefficients to pairs (file count, measured slack).

        The slope is found by least squares, but only if the records cover
        at least two different n and the slope came out positive; otherwise
        the default value stays. The intercept is then raised to the upper
        envelope, so that the model underestimates none of the records.
        """
        usable = [(n, value) for n, value in samples if n >= 1 and value >= 0]
        if not usable:
            return cls()

        counts = [n for n, _ in usable]
        per_file = DEFAULT_SLACK_PER_FILE
        fitted = False
        if len(set(counts)) >= 2:
            slope = cls._least_squares_slope(usable)
            if slope > 0:
                per_file = math.ceil(slope)
                fitted = True

        base = max(value - per_file * n for n, value in usable)
        return cls(
            base=max(base, 0),
            per_file=per_file,
            sample_count=len(usable),
            file_counts=tuple(counts),
            per_file_fitted=fitted,
        )

    @staticmethod
    def _least_squares_slope(samples: Sequence[tuple[int, int]]) -> float:
        n_mean = sum(n for n, _ in samples) / len(samples)
        y_mean = sum(y for _, y in samples) / len(samples)
        numerator = sum((n - n_mean) * (y - y_mean) for n, y in samples)
        denominator = sum((n - n_mean) ** 2 for n, _ in samples)
        return numerator / denominator if denominator else 0.0


@dataclass(frozen=True)
class Payload:
    """What is to be put into the container."""

    logical_bytes: int
    alloc_bytes: int
    file_count: int
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES

    @property
    def cluster_tail(self) -> int:
        return self.alloc_bytes - self.logical_bytes

    @classmethod
    def for_file(
        cls, size_bytes: int, cluster_bytes: int = DEFAULT_CLUSTER_BYTES
    ) -> "Payload":
        return cls(
            logical_bytes=size_bytes,
            alloc_bytes=round_up(size_bytes, cluster_bytes),
            file_count=1,
            cluster_bytes=cluster_bytes,
        )

    @classmethod
    def for_files(
        cls,
        sizes: Iterable[int],
        cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
    ) -> "Payload":
        """Sum of cluster-rounded sizes, not logical ones.

        The cost of MFT records is not included here — it belongs entirely to
        CopySlackModel, so as not to be counted twice.
        """
        logical = 0
        alloc = 0
        count = 0
        for size in sizes:
            logical += size
            alloc += round_up(size, cluster_bytes)
            count += 1
        return cls(
            logical_bytes=logical,
            alloc_bytes=alloc,
            file_count=count,
            cluster_bytes=cluster_bytes,
        )


@dataclass(frozen=True)
class Solution:
    """Result breakdown by component — for the table on the Calculation tab."""

    container_mib: int
    container_bytes: int
    volume_bytes: int
    payload_logical: int
    payload_alloc: int
    cluster_tail: int
    vc_header: int
    metadata_bytes: int
    copy_slack: int
    safety_bytes: int
    predicted_left_bytes: int
    ntfs_extrapolated: bool
    slack_unverified: bool


def volume_of(container_bytes: int) -> int:
    """Volume size of a container: everything VeraCrypt leaves to the filesystem.

    Computed, not measured. The measured capacity (`mounted_bytes`) is smaller
    by the filesystem tail, and the tail differs between filesystems and
    cluster sizes; the volume size does not. That is why it is the X axis of
    the metadata model and the key of a calibration point.
    """
    return container_bytes - VC_HEADERS_BYTES


def solve_container_mib(
    payload: Payload,
    ntfs: MetadataModel | None = None,
    slack: CopySlackModel | None = None,
    safety_bytes: int = DEFAULT_SAFETY_BYTES,
    max_iterations: int = 8,
) -> Solution:
    """Find the minimal container size in MiB that the payload fits into.

    NTFS metadata depends on the volume size, and the volume size depends on
    the result being sought, so the solution is iterative. It converges in two
    or three iterations: NTFS changes much more slowly than the volume itself.
    """
    ntfs = ntfs or MetadataModel()
    slack = slack or CopySlackModel()

    slack_bytes = slack.slack(payload.file_count)
    fixed = payload.alloc_bytes + VC_HEADERS_BYTES + slack_bytes + safety_bytes

    volume_guess = payload.alloc_bytes
    container_mib = 0
    metadata_bytes = 0

    for _ in range(max_iterations):
        metadata_bytes = ntfs.overhead(volume_guess)
        container_mib = ceil_div(fixed + metadata_bytes, MIB)
        next_volume = volume_of(container_mib * MIB)
        if next_volume == volume_guess:
            break
        volume_guess = next_volume

    container_bytes = container_mib * MIB
    volume_bytes = volume_of(container_bytes)
    metadata_bytes = ntfs.overhead(volume_bytes)

    return Solution(
        container_mib=container_mib,
        container_bytes=container_bytes,
        volume_bytes=volume_bytes,
        payload_logical=payload.logical_bytes,
        payload_alloc=payload.alloc_bytes,
        cluster_tail=payload.cluster_tail,
        vc_header=VC_HEADERS_BYTES,
        metadata_bytes=metadata_bytes,
        copy_slack=slack_bytes,
        safety_bytes=safety_bytes,
        predicted_left_bytes=(
            volume_bytes - metadata_bytes - payload.alloc_bytes - slack_bytes
        ),
        ntfs_extrapolated=ntfs.is_extrapolation(volume_bytes),
        slack_unverified=payload.file_count > 1 and not slack.per_file_calibrated,
    )


#: Factory margin added to the safety margin while both ends of the segment
#: are factory points. It equals the default value: someone else's data
#: deserves exactly as much trust as a model with no calibration at all.
#: Another Windows build could have chosen a different `$LogFile` size, and
#: underestimate is the only dangerous side here. An own measurement nearby
#: removes the factory margin.
FACTORY_MARGIN_BYTES = 4 * MIB

#: The safety margin never goes below this. A free-space measurement is
#: slightly noisy in itself, and advising zero would be a lie about precision.
MIN_SAFETY_BYTES = MIB

#: By how many times a copy-slack measurement's file count may differ from the
#: calculation's and still count as a similar file count. A wide window: the
#: per-file slack changes slowly, and measurements are few and far apart in n.
SLACK_NEIGHBOUR_RATIO = 8.0


@dataclass(frozen=True)
class SafetyAdvice:
    """How much safety margin this very calculation needs, and why."""

    total_bytes: int
    metadata_bytes: int
    slack_bytes: int
    ntfs_reason: str
    slack_reason: str
    #: Names of the records the advice is built on. Empty means there is
    #: nothing to build it on.
    basis: tuple[str, ...] = ()

    @property
    def total_mib(self) -> int:
        return ceil_div(self.total_bytes, MIB)


class SafetyModel:
    """Safety margin for a specific size, not one number for all cases.

    A general "largest miss over all records" is a useless value: it is taken
    from the size where the model is weakest, and drags that weakness onto
    calculations that have nothing to do with it. A 5 GiB container must not
    pay for the curve breaking somewhere around 48 GiB.

    The advice is made of two independent parts:

    * NTFS — the interpolation bound at exactly this point and the misses on
      records of similar size, scaled to the width of the segment in question;
    * copy slack — the misses on measurements with a similar file count.

    About scaling to width. The miss is taken from the leave-one-out check:
    only it shows how the model behaves where there was no point. But it
    measures the model without one point, that is, with a gap about twice as
    wide as the real one, and taking its miss as is was exactly that general
    "largest miss" this class moves away from: it does not shrink as
    measurements are added, because each new measurement is immediately left
    out in its turn. The error of linear interpolation grows as the square of
    the gap width, so a miss taken on a gap `h_loo` is rescaled to the real
    segment `h` by the factor `(h / h_loo)²`. Taking the residual of the full
    model would be useless: at its own point it is zero by construction.

    Where there are no records nearby, the honest answer is the default
    value, not an optimistic zero.
    """

    def __init__(
        self,
        ntfs: MetadataModel | None = None,
        ntfs_deviations: Sequence[tuple[int, int, str]] = (),
        slack_deviations: Sequence[tuple[int, int, str]] = (),
        extrapolation_bytes: int = DEFAULT_SAFETY_BYTES,
        floor: int = MIN_SAFETY_BYTES,
        default_bytes: int = DEFAULT_SAFETY_BYTES,
        factory_volumes: set[int] | None = None,
        factory_margin: int = FACTORY_MARGIN_BYTES,
    ) -> None:
        self.ntfs = ntfs or MetadataModel()
        self.ntfs_deviations = tuple(ntfs_deviations)
        self.slack_deviations = tuple(slack_deviations)
        #: What to budget beyond the edge of the measurements: there is no
        #: local evidence there at all, and caution is the only answer
        #: available.
        self.extrapolation_bytes = extrapolation_bytes
        self.floor = floor
        self.default_bytes = default_bytes
        #: Volumes covered only by factory data, and the factory margin for
        #: them. Another Windows build could have chosen a different $LogFile
        #: size; underestimate is the only dangerous side, so while both ends
        #: of a segment are factory ones, a constant is added to the safety
        #: margin. An own measurement nearby removes it.
        self.factory_volumes = set(factory_volumes or ())
        self.factory_margin = factory_margin

    @staticmethod
    def _nearby(
        samples: Sequence[tuple[int, int, str]], target: int, ratio: float
    ) -> list[tuple[int, int, str]]:
        """Records whose file count is within a factor of ratio of target."""
        if target <= 0:
            return []
        low, high = target / ratio, target * ratio
        return [item for item in samples if item[0] and low <= item[0] <= high]

    @staticmethod
    def _worst(samples: Sequence[tuple[int, int, str]]) -> int:
        """Largest underestimate in the sample. Overestimates do not count."""
        return max((deviation for _, deviation, _ in samples), default=0)

    def _advise_ntfs(self, volume_bytes: int) -> tuple[int, str, tuple[str, ...]]:
        if not self.ntfs.calibrated:
            return (
                self.default_bytes,
                "модель NTFS не откалибрована — значение по умолчанию",
                (),
            )

        if self.ntfs.is_extrapolation(volume_bytes):
            return (
                max(self.extrapolation_bytes, self.default_bytes),
                "размер вне измеренного диапазона — осторожная оценка по всем "
                "записям сразу",
                (),
            )

        bound = self.ntfs.interpolation_bound(volume_bytes)
        span = self._segment_span(volume_bytes)
        neighbours = self._segment_endpoints(volume_bytes)

        # The weight zeroes a neighbour's miss at the measurements themselves
        # and raises it towards the middle of the gap: that is exactly where
        # linear interpolation errs, while at the nodes it is exact by
        # construction.
        weight = self._segment_weight(volume_bytes)
        scaled = 0
        for volume, deviation, _ in neighbours:
            if deviation <= 0:
                continue
            gap = self._loo_span(volume)
            if not gap:
                continue
            scaled = max(scaled, math.ceil(deviation * weight * (span / gap) ** 2))

        names = tuple(name for _, _, name in neighbours)
        value = max(bound, scaled)
        if not value:
            reason = "модель здесь не занижает: замер рядом, хорда идёт сверху"
        elif scaled > bound:
            reason = "промах на записях похожего размера, приведённый к этому отрезку"
        else:
            reason = "изгиб кривой между соседними замерами"

        if self._leans_on_factory(volume_bytes):
            value += self.factory_margin
            reason += "; отрезок держится на заводских замерах"
        return value, reason, names

    def _leans_on_factory(self, volume_bytes: int) -> bool:
        """Both segment ends are factory points; no own measurements nearby."""
        if not self.factory_volumes or not self.ntfs.calibrated:
            return False
        points = self.ntfs.points
        index = 0
        while index + 1 < len(points) - 1 and points[index + 1][0] < volume_bytes:
            index += 1
        edges = (points[index][0], points[index + 1][0])
        return all(edge in self.factory_volumes for edge in edges)

    def _segment_endpoints(
        self, volume_bytes: int
    ) -> list[tuple[int, int, str]]:
        """Records between which the target size lies.

        "Similar size" means exactly these, not everything that fell into a
        window by size ratio. A window of a factor of two would pull in
        next-but-one neighbours: next to 90 GiB a record at 48 GiB turned up,
        where the curve has a knee, and its miss was carried into a flat
        region where, by the measurements, only the bitmap grows.
        """
        points = self.ntfs.points
        index = 0
        while index + 1 < len(points) - 1 and points[index + 1][0] < volume_bytes:
            index += 1
        edges = {points[index][0], points[index + 1][0]}
        return [item for item in self.ntfs_deviations if item[0] in edges]

    def _segment_weight(self, volume_bytes: int) -> float:
        """How deep the target size sits inside the segment.

        Zero at the ends, one in the middle. At the measurement itself the
        model is exact, and attributing someone else's error to it there is
        unfair.
        """
        points = self.ntfs.points
        index = 0
        while index + 1 < len(points) - 1 and points[index + 1][0] < volume_bytes:
            index += 1
        x0, x1 = points[index][0], points[index + 1][0]
        if x1 == x0:
            return 0.0
        position = (volume_bytes - x0) / (x1 - x0)
        position = min(max(position, 0.0), 1.0)
        return 4 * position * (1 - position)

    def _segment_span(self, volume_bytes: int) -> int:
        """Width of the segment between measurements where the target falls."""
        points = self.ntfs.points
        index = 0
        while index + 1 < len(points) - 1 and points[index + 1][0] < volume_bytes:
            index += 1
        return points[index + 1][0] - points[index][0]

    def _loo_span(self, volume_bytes: int) -> int:
        """Width of the gap that appears when this point is left out.

        It is exactly the width the miss was measured on: to the left and to
        the right remain the neighbours of the left-out point.
        """
        points = self.ntfs.points
        volumes = [volume for volume, _ in points]
        if volume_bytes not in volumes or len(volumes) < 2:
            return 0
        index = volumes.index(volume_bytes)
        low = volumes[index - 1] if index else volumes[0]
        high = volumes[index + 1] if index + 1 < len(volumes) else volumes[-1]
        return high - low

    def _fallback_slack(self, file_count: int) -> tuple[int, str, tuple[str, ...]]:
        """What to budget without a copy-slack measurement near the file count.

        The risk here depends on the file count, not on their total size. The
        constant part is measured and small — hundreds of kilobytes — so the
        safety floor is enough for one file. The per-file slack is measured
        too — 1363 B per file on 500…10 000 files, see DEFAULT_SLACK_PER_FILE
        — but it is the part multiplied by the file count: an underestimate
        of a few hundred bytes per file (longer names in the directory index)
        grows into megabytes on many files, and with no measurement at hand
        there is nothing to size it by. There the full default value is
        taken.
        """
        if file_count <= 1:
            return (
                self.floor,
                "один файл: постоянная часть запаса измерена, риск мал",
                (),
            )
        return (
            self.default_bytes,
            f"нет замера с похожим числом файлов, а их {file_count} — "
            "значение по умолчанию",
            (),
        )

    def _advise_slack(self, file_count: int) -> tuple[int, str, tuple[str, ...]]:
        if not self.slack_deviations:
            return self._fallback_slack(file_count)
        neighbours = self._nearby(
            self.slack_deviations, max(file_count, 1), SLACK_NEIGHBOUR_RATIO
        )
        if not neighbours:
            return self._fallback_slack(file_count)
        return (
            max(self._worst(neighbours), 0),
            "промах на замерах с похожим числом файлов",
            tuple(name for _, _, name in neighbours),
        )

    def advise(self, volume_bytes: int, file_count: int = 1) -> SafetyAdvice:
        metadata_bytes, ntfs_reason, ntfs_names = self._advise_ntfs(volume_bytes)
        slack_bytes, slack_reason, slack_names = self._advise_slack(file_count)
        total = max(metadata_bytes + slack_bytes, self.floor)
        return SafetyAdvice(
            total_bytes=round_up(total, MIB),
            metadata_bytes=metadata_bytes,
            slack_bytes=slack_bytes,
            ntfs_reason=ntfs_reason,
            slack_reason=slack_reason,
            basis=tuple(dict.fromkeys(ntfs_names + slack_names)),
        )


def fit_safety(
    payload: Payload,
    ntfs: MetadataModel,
    slack: CopySlackModel,
    safety: SafetyModel,
    seed_bytes: int = DEFAULT_SAFETY_BYTES,
    max_rounds: int = 8,
) -> SafetyAdvice:
    """The safety margin advised for the volume that this margin itself gives.

    The advice depends on the volume size, and the volume on the margin, so
    the two are solved together: solve with a margin, advise for that volume,
    solve again with the advice, until the advice repeats. The seed is fixed,
    not whatever the Calculation tab's field holds: seeded with the field, the
    answer depended on the previous calculation, and at 547 MiB in 10 000 files
    it walked 582 ↔ 581 MiB on every recalculation of one and the same input.

    The advice does not always settle on one value: there it alternates
    between 5 and 4 MiB — the volume a 4 MiB margin gives is advised 5 MiB,
    the volume a 5 MiB margin gives is advised 4. The largest advice met is
    taken. Every advice met was also tried, so the volume it gives is advised
    no more than itself — the margin is enough at the very size it produces.
    Underestimate is the only dangerous side, and a megabyte too much is its
    price.

    The one exception is `max_rounds`: reached, it leaves the last advice
    untried. It is a guard against a loop, not a working limit — over 4134
    inputs from 1 MiB to 1 TiB on the factory calibration no input needed
    more than three rounds.
    """
    advised: dict[int, SafetyAdvice] = {}
    safety_bytes = seed_bytes
    while safety_bytes not in advised and len(advised) < max_rounds:
        probe = solve_container_mib(
            payload, ntfs=ntfs, slack=slack, safety_bytes=safety_bytes
        )
        advice = safety.advise(probe.volume_bytes, payload.file_count)
        advised[safety_bytes] = advice
        safety_bytes = advice.total_bytes
    return max(advised.values(), key=lambda advice: advice.total_bytes)


def solve_raw_container_mib(
    payload: Payload, safety_bytes: int = MIN_SAFETY_BYTES
) -> Solution:
    """The container for a volume without a filesystem (VeraCrypt's "None").

    There is no filesystem to spend anything: no metadata, no copy slack, no
    clusters — the data lies on the volume byte for byte, so its logical size
    is what counts. The container is the data plus the VeraCrypt headers plus
    the safety margin. The margin is the floor, not the advice: there is no
    model here whose miss it would cover, and the floor is what guards the rest
    of the calculation too. A larger margin is taken as given, a smaller one
    is raised to the floor — the Calculation tab's field goes down to zero.
    """
    safety_bytes = max(safety_bytes, MIN_SAFETY_BYTES)
    container_mib = ceil_div(
        payload.logical_bytes + VC_HEADERS_BYTES + safety_bytes, MIB
    )
    container_bytes = container_mib * MIB
    volume_bytes = volume_of(container_bytes)
    return Solution(
        container_mib=container_mib,
        container_bytes=container_bytes,
        volume_bytes=volume_bytes,
        payload_logical=payload.logical_bytes,
        payload_alloc=payload.logical_bytes,
        cluster_tail=0,
        vc_header=VC_HEADERS_BYTES,
        metadata_bytes=0,
        copy_slack=0,
        safety_bytes=safety_bytes,
        predicted_left_bytes=volume_bytes - payload.logical_bytes,
        ntfs_extrapolated=False,
        slack_unverified=False,
    )
