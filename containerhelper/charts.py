"""Charts from measurements and models. What to draw is here, how in `ui/`.

No Qt for the same reason as `plot.py`: the content of a chart is data, not
styling. What needs testing is which points ended up on it and what the
tooltip says, and that needs no window.
"""

from __future__ import annotations

from typing import Sequence

from .formatting import fmt_both, fmt_bytes, size_label
from .i18n import tr, tr_n
from .model import (
    MIB,
    CopySlackModel,
    MetadataModel,
    Payload,
    Solution,
    volume_of,
)
from .plot import (
    AXIS_BYTES,
    AXIS_COUNT,
    AXIS_MIB,
    AXIS_PLAIN,
    KIND_BARS,
    KIND_DOTS,
    KIND_LINE,
    KIND_LINE_DOTS,
    KIND_STACK,
    KIND_STEMS,
    KIND_STEPS,
    LAYOUT_STACK,
    Axis,
    Chart,
    Point,
    Series,
)
from .records import (
    Record,
    profile_of,
    factory_points,
    factory_samples,
    gives_metadata_point,
    metadata_cross_check,
    metadata_points,
    slack_cross_check,
    slack_samples,
)

#: How many points to take for the model curve. The model is piecewise-linear
#: in volume, and the axis is logarithmic — on it a straight segment becomes
#: an arc, and it cannot be drawn by its two ends: the line would miss its own
#: measurements.
CURVE_SAMPLES = 160


def _series(key: str, *args, **kwargs) -> Series:
    """A series named in the current language and keyed by its catalog key.

    The key is what a hidden series is remembered by: the name changes with
    the language.
    """
    return Series(tr(key), *args, key=key, **kwargs)


def _own(store) -> list[Record]:
    """Records taken here: both copy records and calibration ones."""
    return [*store.records, *store.calibration]


def _is_own(record: Record, own: Sequence[Record]) -> bool:
    """Whether the record is own or factory.

    Compared by identity, not by name: a factory record is synthesised on the
    fly from the package resources and is not in `records`/`calibration` at
    all, and a person is free to give any name, including «Заводская».
    """
    return any(record is item for item in own)


def _point_key(record: Record) -> tuple:
    """What a chart point is found by again after a refresh.

    Not the shown name: that one changes with the language, and the selected
    point would be lost on every switch. A calibration point or a file set
    measurement — own or factory, the name is built either way — is told
    apart by its size and file set: one supersedes the other on exactly
    those. Any other record keeps the name it was given.
    """
    if record.fileset or record.is_calibration_point:
        return ("", profile_of(record), record.container_mib, record.fileset)
    return (record.id, profile_of(record), record.container_mib, record.fileset)


def _geometric(lo: float, hi: float, count: int = CURVE_SAMPLES) -> list[float]:
    """Points evenly spaced on a log scale: the same number in every octave."""
    if lo <= 0 or hi <= lo:
        return [lo, hi]
    ratio = (hi / lo) ** (1.0 / max(count - 1, 1))
    return [lo * ratio**index for index in range(count)]


# --- NTFS metadata ---------------------------------------------------------


#: How many uncovered sizes to name one by one. Beyond that the list stops
#: reading and starts crowding out the caption itself.
UNCOVERED_SHOWN = 5


def uncovered(store, sizes: Sequence[int]) -> list[int]:
    """Recommended sizes that have neither an own nor a factory measurement.

    That is exactly where the model draws a straight line through empty space,
    and that is where the next measurement is worth taking. An own measurement
    disabled with the Use factory button leaves the size uncovered only if
    there is no factory one at that size either.
    """
    own = {
        record.volume_bytes
        for record in store.calibration_points()
        if gives_metadata_point(record) and not record.disabled
    }
    factory = {point.volume_bytes for point in factory_points()}
    return [
        size_mib
        for size_mib in sizes
        if volume_of(size_mib * MIB) not in own
        and volume_of(size_mib * MIB) not in factory
    ]


def ntfs_curve(store, sizes: Sequence[int] = ()) -> Chart:
    """Measured metadata and the model's polyline over it.

    `sizes` is the list of recommended sizes; by it the caption says which of
    them are covered by nothing. This used to be a separate panel — the
    "coverage strip", three rows of dots — and turned out to be redundant: own
    against factory is already visible on the curve by colour, and everything
    the strip added beyond that fits in one line. The table on the Calibration
    tab says the same in more detail and with buttons.
    """
    own = _own(store)
    records = store.all_for_model()
    model = MetadataModel(metadata_points(records))

    mine: list[Point] = []
    factory: list[Point] = []
    for record in records:
        overhead = record.metadata_bytes
        if overhead is None or not record.mounted_bytes:
            continue
        point = Point(
            float(record.volume_bytes),
            float(overhead),
            tr(
                "charts.ntfs_curve.tip",
                id=record.name,
                volume=fmt_both(record.volume_bytes),
                metadata=fmt_both(overhead),
            ),
            key=_point_key(record),
        )
        (mine if _is_own(record, own) else factory).append(point)

    curve: list[Point] = []
    if model.points:
        lo = min(point[0] for point in model.points)
        hi = max(point[0] for point in model.points)
        curve = [Point(x, float(model.overhead(int(x)))) for x in _geometric(lo, hi)]

    note = tr("charts.ntfs_curve.note")
    holes = uncovered(store, sizes)
    if holes:
        names = ", ".join(size_label(size) for size in holes[:UNCOVERED_SHOWN])
        if len(holes) > UNCOVERED_SHOWN:
            names = tr(
                "charts.ntfs_curve.more",
                names=names,
                n=len(holes) - UNCOVERED_SHOWN,
            )
        note += " " + tr_n("charts.ntfs_curve.uncovered", len(holes), names=names)
    return Chart(
        tr("charts.ntfs_curve.title"),
        Axis(tr("charts.axis.volume"), AXIS_BYTES, log=True),
        Axis(tr("charts.ntfs_curve.y"), AXIS_BYTES),
        (
            _series("charts.series.model", tuple(curve), KIND_LINE, tone=2),
            _series("charts.series.own", tuple(mine), KIND_DOTS, tone=0),
            _series("charts.series.factory", tuple(factory), KIND_DOTS, tone=1),
        ),
        note=note,
    )


def ntfs_residuals(store) -> Chart:
    """Leave-one-out check: how far the model would miss without this point.

    Drawing residuals from the model itself is pointless — a piecewise-linear
    curve passes exactly through its own measurements, and the deviation would
    come out zero everywhere. So each point is checked by a model built
    without it; the safety advice is computed exactly the same way.
    """
    checks = [
        check
        for check in metadata_cross_check(store.all_for_model())
        if check.record.mounted_bytes
    ]
    volumes = [check.record.volume_bytes for check in checks]
    # The outermost points are a special case: without them the model has
    # nothing to draw a straight line between, and it is forced to extrapolate
    # with the baseline slope. The miss there is hundreds of times larger and
    # measures something else, so the series is separate and hidden by
    # default: otherwise one outlier of −433 MiB presses everything else to
    # zero, and everything else is what this is about. A click on the legend
    # brings it back.
    edges = {min(volumes), max(volumes)} if volumes else set()

    under: list[Point] = []
    over: list[Point] = []
    edge: list[Point] = []
    for check in checks:
        record = check.record
        point = Point(
            float(record.volume_bytes),
            float(check.deviation),
            tr(
                "charts.ntfs_residuals.tip",
                id=record.name,
                volume=fmt_both(record.volume_bytes),
                measured=fmt_both(check.measured),
                predicted=fmt_both(check.predicted),
                miss=fmt_both(check.deviation),
            ),
            key=_point_key(record),
        )
        if record.volume_bytes in edges:
            edge.append(point)
        else:
            (under if check.deviation > 0 else over).append(point)

    return Chart(
        tr("charts.ntfs_residuals.title"),
        Axis(tr("charts.axis.volume"), AXIS_BYTES, log=True),
        Axis(tr("charts.axis.residual"), AXIS_BYTES),
        (
            # As stems, not bare dots: zero here is the model itself, and the
            # stem from it to the dot shows which way and how far the miss
            # goes, without making the eye measure the distance to the line.
            _series("charts.series.under", tuple(under), KIND_STEMS, tone=1),
            _series("charts.series.over", tuple(over), KIND_STEMS, tone=0),
            _series(
                "charts.ntfs_residuals.edge",
                tuple(edge),
                KIND_STEMS,
                tone=3,
                visible=False,
            ),
        ),
        note=tr("charts.ntfs_residuals.note"),
        zero_line=True,
    )


def ntfs_share(store) -> Chart:
    """What share of the volume the metadata eats.

    The curve in bytes answers a different question — "how much of it" — and
    against a volume in gigabytes the difference between 0.2 % and 0.4 % is
    the thickness of a line there. And the share is what metadata is judged by
    at a glance: "how much of the volume goes to something other than data".

    Segment slopes are different values too: the slope says how fast the
    metadata grows over a stretch, and the share how much of it there is in
    total at this size. A flat stretch of slope and a flat stretch of share
    are not the same thing.
    """
    own = _own(store)
    records = store.all_for_model()
    model = MetadataModel(metadata_points(records))

    mine: list[Point] = []
    factory: list[Point] = []
    for record in records:
        overhead = record.metadata_bytes
        if overhead is None or not record.mounted_bytes:
            continue
        share = overhead / record.volume_bytes * 100.0
        point = Point(
            float(record.volume_bytes),
            share,
            tr(
                "charts.ntfs_share.tip",
                id=record.name,
                volume=fmt_both(record.volume_bytes),
                metadata=fmt_both(overhead),
                share=f"{share:.3f}",
            ),
            key=_point_key(record),
        )
        (mine if _is_own(record, own) else factory).append(point)

    curve: list[Point] = []
    if model.points:
        lo = min(point[0] for point in model.points)
        hi = max(point[0] for point in model.points)
        curve = [
            Point(x, model.overhead(int(x)) / x * 100.0) for x in _geometric(lo, hi)
        ]

    return Chart(
        tr("charts.ntfs_share.title"),
        Axis(tr("charts.axis.volume"), AXIS_BYTES, log=True),
        # Logarithmic vertically too: the share spans three orders of
        # magnitude — from 0.013 % on a terabyte volume to 16 % at sixty-four
        # megabytes — and on a linear axis everything except the smallest
        # volumes lies in one line near zero.
        Axis(tr("charts.ntfs_share.y"), AXIS_PLAIN, log=True),
        (
            _series("charts.series.model", tuple(curve), KIND_LINE, tone=2),
            _series("charts.series.own", tuple(mine), KIND_DOTS, tone=0),
            _series("charts.series.factory", tuple(factory), KIND_DOTS, tone=1),
        ),
        note=tr("charts.ntfs_share.note"),
    )


def ntfs_slopes(store) -> Chart:
    """Slope of each polyline segment — the curve's shape, not its error.

    On the curve itself this cannot be made out: against a volume in gigabytes
    the difference between 0.128 % and 0.324 % is the thickness of a line. And
    that is exactly the $LogFile steps.
    """
    model = MetadataModel(metadata_points(store.all_for_model()))
    points = model.points
    steps: list[Point] = []
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        slope = (y1 - y0) / (x1 - x0) * 100.0
        tip = tr(
            "charts.ntfs_slopes.tip",
            start=size_label(int(x0 // MIB)),
            end=size_label(int(x1 // MIB)),
            slope=f"{slope:.3f}",
            growth=fmt_both(y1 - y0),
        )
        steps.append(Point(float(x0), slope, tip))
    if steps:
        # Close the last step, otherwise it breaks off at the second-to-last
        # measurement and looks shorter than it is.
        steps.append(Point(float(points[-1][0]), steps[-1].y, steps[-1].tip))

    return Chart(
        tr("charts.ntfs_slopes.title"),
        Axis(tr("charts.axis.volume"), AXIS_BYTES, log=True),
        Axis(tr("charts.ntfs_slopes.y"), AXIS_PLAIN),
        (_series("charts.ntfs_slopes.series", tuple(steps), KIND_STEPS, tone=3),),
        note=tr("charts.ntfs_slopes.note"),
        zero_line=True,
    )


# --- copy slack ------------------------------------------------------------


def slack_curve(store) -> Chart:
    """Measured slack against the file count, and the model's line."""
    own = _own(store)
    records = store.all_for_model()
    model = CopySlackModel.calibrate(slack_samples(records))

    mine: list[Point] = []
    factory: list[Point] = []
    for record in records:
        measured = record.copy_slack_measured
        if measured is None or not record.file_count:
            continue
        point = Point(
            float(record.file_count),
            float(measured),
            tr(
                "charts.slack_curve.tip",
                id=record.name,
                files=fmt_bytes(record.file_count),
                measured=fmt_both(measured),
                per_file=fmt_bytes(measured // max(record.file_count, 1)),
            ),
            key=_point_key(record),
        )
        (mine if _is_own(record, own) else factory).append(point)

    counts = [point.x for point in [*mine, *factory]]
    curve: list[Point] = []
    if counts:
        for n in _geometric(min(counts), max(counts), 60):
            curve.append(Point(n, float(model.slack(int(round(n))))))

    per_file = (
        tr("charts.slack_curve.slope", per_file=fmt_bytes(model.per_file))
        if model.per_file_calibrated
        else tr("charts.slack_curve.no_slope")
    )
    return Chart(
        tr("charts.slack_curve.title"),
        Axis(tr("charts.axis.files"), AXIS_COUNT, log=True),
        Axis(tr("charts.slack_curve.y"), AXIS_BYTES),
        (
            _series("charts.series.model", tuple(curve), KIND_LINE, tone=2),
            _series("charts.series.own", tuple(mine), KIND_DOTS, tone=0),
            _series("charts.series.factory", tuple(factory), KIND_DOTS, tone=1),
        ),
        note=per_file,
    )


def slack_residuals(store) -> Chart:
    """Leave-one-out check for slack — the same logic as for the metadata."""
    under: list[Point] = []
    over: list[Point] = []
    for check in slack_cross_check(store.all_for_model()):
        record = check.record
        point = Point(
            float(record.file_count or 1),
            float(check.deviation),
            tr(
                "charts.slack_residuals.tip",
                id=record.name,
                measured=fmt_both(check.measured),
                predicted=fmt_both(check.predicted),
                miss=fmt_both(check.deviation),
            ),
            key=_point_key(record),
        )
        (under if check.deviation > 0 else over).append(point)

    return Chart(
        tr("charts.slack_residuals.title"),
        Axis(tr("charts.axis.files"), AXIS_COUNT, log=True),
        Axis(tr("charts.axis.residual"), AXIS_BYTES),
        (
            _series("charts.series.under", tuple(under), KIND_STEMS, tone=1),
            _series("charts.series.over", tuple(over), KIND_STEMS, tone=0),
        ),
        note=tr("charts.slack_residuals.note"),
        zero_line=True,
    )


# --- prediction against fact -----------------------------------------------


def forecast_misses(store) -> Chart:
    """What the calculation promised against what would have just sufficed.

    The two values side by side are not for completeness: a miss of +5 MiB
    looks the same both when the model is exact with a 5 MiB safety margin and
    when the model underestimated by three and eight megabytes of safety margin
    hid it. The second is a harbinger of failure that looks healthy.
    """
    checked = [record for record in store.records if record.forecast_checked]
    misses: list[Point] = []
    bare: list[Point] = []
    labels: list[str] = []
    for index, record in enumerate(checked):
        miss = record.miss_mib or 0
        model_miss = record.model_miss_mib
        labels.append(record.name)
        misses.append(
            Point(
                float(index),
                float(miss),
                tr(
                    "charts.forecast.tip",
                    id=record.name,
                    predicted=record.predicted_mib,
                    minimum=record.minimum_mib,
                    miss=miss,
                    safety=record.predicted_safety_mib or 0,
                ),
                key=_point_key(record),
            )
        )
        if model_miss is not None:
            bare.append(
                Point(
                    float(index),
                    float(model_miss),
                    tr("charts.forecast.bare_tip", id=record.name, miss=model_miss),
                    key=_point_key(record),
                )
            )

    short = sum(1 for point in bare if point.y < 0)
    note = (
        tr("charts.forecast.note")
        if not short
        else tr_n("charts.forecast.short", short)
    )
    return Chart(
        tr("charts.forecast.title"),
        Axis(tr("charts.forecast.x"), AXIS_PLAIN),
        Axis(tr("charts.forecast.y"), AXIS_MIB),
        (
            _series("charts.forecast.misses", tuple(misses), KIND_BARS, tone=0),
            # As dots, not bars: a bar over a bar reads as "part of a whole",
            # and this is a separate value of the same miss, taken from a
            # different place. Below zero the dot stands alone, and the
            # caption under the chart spells out what it means.
            _series("charts.forecast.bare", tuple(bare), KIND_DOTS, tone=1),
        ),
        note=note,
        zero_line=True,
        categories=tuple(labels),
    )


# --- current calculation ---------------------------------------------------


def container_breakdown(solution: Solution) -> Chart:
    """The bar: what the computed container is made of.

    Thin components are not visible here, and that is the message: the
    VeraCrypt header on a terabyte container is a hairline. The numbers for
    each component are in the legend, because a hairline cannot be pointed at
    with the cursor.
    """
    rounding = max(solution.predicted_left_bytes - solution.safety_bytes, 0)
    parts = (
        ("charts.breakdown.payload", solution.payload_alloc),
        ("charts.breakdown.header", solution.vc_header),
        ("charts.breakdown.metadata", solution.metadata_bytes),
        ("charts.breakdown.copy_slack", solution.copy_slack),
        ("charts.breakdown.safety", solution.safety_bytes),
        ("charts.breakdown.rounding", rounding),
    )
    points = tuple(
        Point(
            0.0,
            float(value),
            tr("charts.breakdown.tip", part=tr(name), value=fmt_both(value)),
        )
        for name, value in parts
    )
    return Chart(
        tr("charts.breakdown.title", container=solution.container_mib),
        Axis(""),
        Axis(""),
        (_series("charts.breakdown.series", points, KIND_STACK),),
        note=tr("charts.breakdown.note", tail=fmt_both(solution.cluster_tail)),
        layout=LAYOUT_STACK,
    )


def cluster_tail(
    sizes: Sequence[int],
    choices: Sequence[int],
    current: int,
) -> Chart:
    """What the choice of cluster size will cost this particular data.

    The only chart computed from the real sizes of the selected files, not
    from the model. And the largest effect in the whole program: on small
    files the difference between 512 and 65536 comes out several times over.
    """
    logical = sum(sizes)
    points: list[Point] = []
    chosen: list[Point] = []
    for cluster in choices:
        alloc = Payload.for_files(sizes, cluster).alloc_bytes
        tail = alloc - logical
        tip = tr(
            "charts.cluster_tail.tip",
            cluster=fmt_bytes(cluster),
            alloc=fmt_both(alloc),
            tail=fmt_both(tail),
        )
        point = Point(float(cluster), float(alloc), tip, key=cluster)
        points.append(point)
        if cluster == current:
            chosen.append(point)

    flat = (
        tuple(
            Point(
                float(cluster),
                float(logical),
                tr("charts.cluster_tail.logical_tip", logical=fmt_both(logical)),
            )
            for cluster in choices
        )
        if choices
        else ()
    )
    return Chart(
        tr("charts.cluster_tail.title"),
        Axis(tr("charts.cluster_tail.x"), AXIS_BYTES, log=True),
        Axis(tr("charts.cluster_tail.y"), AXIS_BYTES),
        (
            _series(
                "charts.cluster_tail.alloc", tuple(points), KIND_LINE_DOTS, tone=0
            ),
            _series("charts.cluster_tail.logical", flat, KIND_LINE, tone=4),
            _series("charts.cluster_tail.chosen", tuple(chosen), KIND_DOTS, tone=1),
        ),
        note=tr("charts.cluster_tail.note", files=fmt_bytes(len(sizes))),
    )


def empty_chart(title: str, note: str) -> Chart:
    """A chart with no data, but explaining what exactly is missing.

    An empty space saying "no measurements" is more honest than an empty space
    saying nothing: otherwise it is unclear whether the program broke or there
    is nothing to measure yet.
    """
    return Chart(title, Axis(""), Axis(""), (), note=note)


def has_factory_slack() -> bool:
    """Whether there are factory slack measurements.

    If not, there is nothing to draw the second series with.
    """
    return bool(factory_samples())
