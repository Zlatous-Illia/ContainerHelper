"""Charts from measurements and models. What to draw is here, how in `ui/`.

No Qt for the same reason as `plot.py`: the content of a chart is data, not
styling. What needs testing is which points ended up on it and what the
tooltip says, and that needs no window.
"""

from __future__ import annotations

from typing import Sequence

from .factory import factory_data
from .formatting import fmt_both, fmt_bytes, plural, size_label
from .model import (
    MIB,
    VC_HEADER_BYTES,
    CopySlackModel,
    MetadataModel,
    Payload,
    Solution,
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
        record.mounted_bytes
        for record in store.calibration_points()
        if record.mounted_bytes and not record.disabled
    }
    factory = factory_data().by_volume()
    return [
        size_mib
        for size_mib in sizes
        if (size_mib * MIB - VC_HEADER_BYTES) not in own
        and (size_mib * MIB - VC_HEADER_BYTES) not in factory
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
            float(record.mounted_bytes),
            float(overhead),
            f"{record.id}\n"
            f"Том: {fmt_both(record.mounted_bytes)}\n"
            f"Метаданные: {fmt_both(overhead)}",
            key=record.id,
        )
        (mine if _is_own(record, own) else factory).append(point)

    curve: list[Point] = []
    if model.points:
        lo = min(point[0] for point in model.points)
        hi = max(point[0] for point in model.points)
        curve = [Point(x, float(model.overhead(int(x)))) for x in _geometric(lo, hi)]

    note = (
        "Модель ведёт прямую между соседними замерами, а зависимость прямой не "
        "является: $LogFile меняется ступенями."
    )
    holes = uncovered(store, sizes)
    if holes:
        names = ", ".join(size_label(size) for size in holes[:UNCOVERED_SHOWN])
        if len(holes) > UNCOVERED_SHOWN:
            names += f" и ещё {len(holes) - UNCOVERED_SHOWN}"
        note += (
            f" Не покрыто замерами: {len(holes)} "
            f"{plural(len(holes), 'размер', 'размера', 'размеров')} — {names}; "
            f"там прямая идёт через пустое место."
        )
    return Chart(
        "Метаданные NTFS от размера тома",
        Axis("Размер тома", AXIS_BYTES, log=True),
        Axis("Метаданные NTFS", AXIS_BYTES),
        (
            Series("модель", tuple(curve), KIND_LINE, tone=2),
            Series("свои замеры", tuple(mine), KIND_DOTS, tone=0),
            Series("заводские", tuple(factory), KIND_DOTS, tone=1),
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
    volumes = [check.record.mounted_bytes for check in checks]
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
            float(record.mounted_bytes),
            float(check.deviation),
            f"{record.id}\n"
            f"Том: {fmt_both(record.mounted_bytes)}\n"
            f"Измерено: {fmt_both(check.measured)}\n"
            f"Модель без этой точки: {fmt_both(check.predicted)}\n"
            f"Промах: {fmt_both(check.deviation)}",
            key=record.id,
        )
        if record.mounted_bytes in edges:
            edge.append(point)
        else:
            (under if check.deviation > 0 else over).append(point)

    return Chart(
        "Промах модели NTFS, проверка исключением",
        Axis("Размер тома", AXIS_BYTES, log=True),
        Axis("Измерено минус модель", AXIS_BYTES),
        (
            # As stems, not bare dots: zero here is the model itself, and the
            # stem from it to the dot shows which way and how far the miss
            # goes, without making the eye measure the distance to the line.
            Series("модель занизила", tuple(under), KIND_STEMS, tone=1),
            Series("модель завысила", tuple(over), KIND_STEMS, tone=0),
            Series("край диапазона", tuple(edge), KIND_STEMS, tone=3, visible=False),
        ),
        note=(
            "Ноль — это сама модель. Вверх — занижение, единственная опасная "
            "сторона: столько не хватило бы контейнеру. Крайние замеры "
            "спрятаны — без них модель экстраполирует, и промах там на порядки "
            "больше; вернуть их можно щелчком по легенде."
        ),
        zero_line=True,
    )


def ntfs_share(store) -> Chart:
    """What share of the volume the metadata eats.

    The curve in bytes answers a different question — "how much of it" — and
    against a volume in gigabytes the difference between 0,2 % and 0,4 % is
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
        share = overhead / record.mounted_bytes * 100.0
        point = Point(
            float(record.mounted_bytes),
            share,
            (
                f"{record.id}\n"
                f"Том: {fmt_both(record.mounted_bytes)}\n"
                f"Метаданные: {fmt_both(overhead)}\n"
                f"Доля тома: {share:.3f} %"
            ).replace(".", ","),
            key=record.id,
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
        "Доля тома под метаданными",
        Axis("Размер тома", AXIS_BYTES, log=True),
        # Logarithmic vertically too: the share spans three orders of
        # magnitude — from 0,013 % on a terabyte volume to 16 % at sixty-four
        # megabytes — and on a linear axis everything except the smallest
        # volumes lies in one line near zero.
        Axis("Доля тома, %", AXIS_PLAIN, log=True),
        (
            Series("модель", tuple(curve), KIND_LINE, tone=2),
            Series("свои замеры", tuple(mine), KIND_DOTS, tone=0),
            Series("заводские", tuple(factory), KIND_DOTS, tone=1),
        ),
        note=(
            "Сколько тома уходит не на данные. Соседний график наклонов — про "
            "другое: там скорость роста на участке, а не уровень."
        ),
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
        tip = (
            f"{size_label(int(x0 // MIB))} → {size_label(int(x1 // MIB))}\n"
            f"Наклон: {slope:.3f} % от размера тома\n"
            f"Прирост: {fmt_both(y1 - y0)}"
        ).replace(".", ",")
        steps.append(Point(float(x0), slope, tip))
    if steps:
        # Close the last step, otherwise it breaks off at the second-to-last
        # measurement and looks shorter than it is.
        steps.append(Point(float(points[-1][0]), steps[-1].y, steps[-1].tip))

    return Chart(
        "Наклон отрезков модели NTFS",
        Axis("Размер тома", AXIS_BYTES, log=True),
        Axis("Наклон, % от размера тома", AXIS_PLAIN),
        (Series("отрезки", tuple(steps), KIND_STEPS, tone=3),),
        note=(
            "Ровная ступень — участок, где метаданные растут пропорционально "
            "тому. Скачок — порог, на котором сменился размер $LogFile."
        ),
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
            f"{record.id}\n"
            f"Файлов: {fmt_bytes(record.file_count)}\n"
            f"Измеренный запас: {fmt_both(measured)}\n"
            f"На файл: {fmt_bytes(measured // max(record.file_count, 1))} B",
            key=record.id,
        )
        (mine if _is_own(record, own) else factory).append(point)

    counts = [point.x for point in [*mine, *factory]]
    curve: list[Point] = []
    if counts:
        for n in _geometric(min(counts), max(counts), 60):
            curve.append(Point(n, float(model.slack(int(round(n))))))

    per_file = (
        f"Наклон: {fmt_bytes(model.per_file)} B на файл."
        if model.per_file_calibrated
        else "Наклон не измерен: для него нужны замеры при двух разных n."
    )
    return Chart(
        "Запас на копирование от числа файлов",
        Axis("Файлов", AXIS_COUNT, log=True),
        Axis("Измеренный запас", AXIS_BYTES),
        (
            Series("модель", tuple(curve), KIND_LINE, tone=2),
            Series("свои замеры", tuple(mine), KIND_DOTS, tone=0),
            Series("заводские", tuple(factory), KIND_DOTS, tone=1),
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
            f"{record.id}\n"
            f"Измерено: {fmt_both(check.measured)}\n"
            f"Модель без этого замера: {fmt_both(check.predicted)}\n"
            f"Промах: {fmt_both(check.deviation)}",
            key=record.id,
        )
        (under if check.deviation > 0 else over).append(point)

    return Chart(
        "Промах модели запаса, проверка исключением",
        Axis("Файлов", AXIS_COUNT, log=True),
        Axis("Измерено минус модель", AXIS_BYTES),
        (
            Series("модель занизила", tuple(under), KIND_STEMS, tone=1),
            Series("модель завысила", tuple(over), KIND_STEMS, tone=0),
        ),
        note="Ноль — сама модель. Вверх — занижение.",
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
        labels.append(record.id)
        misses.append(
            Point(
                float(index),
                float(miss),
                f"{record.id}\n"
                f"Обещано: {record.predicted_mib} MiB\n"
                f"Хватило бы: {record.minimum_mib} MiB\n"
                f"Перезаклад: {miss} MiB\n"
                f"В том числе страховка: {record.predicted_safety_mib or 0} MiB",
                key=record.id,
            )
        )
        if model_miss is not None:
            bare.append(
                Point(
                    float(index),
                    float(model_miss),
                    f"{record.id}\nПромах без страховки: {model_miss} MiB",
                    key=record.id,
                )
            )

    short = sum(1 for point in bare if point.y < 0)
    note = (
        "Всё выше нуля — перезаклад. Точка ниже нуля означает, что данные не "
        "влезли бы без страховки."
        if not short
        else f"Ниже нуля: {short} {plural(short, 'запись', 'записи', 'записей')} — "
        "там расчёт спасла только страховка."
    )
    return Chart(
        "Промах прогноза по записям",
        Axis("Запись", AXIS_PLAIN),
        Axis("Обещано минус необходимо", AXIS_MIB),
        (
            Series("перезаклад", tuple(misses), KIND_BARS, tone=0),
            # As dots, not bars: a bar over a bar reads as "part of a whole",
            # and this is a separate value of the same miss, taken from a
            # different place. Below zero the dot stands alone, and the
            # caption under the chart spells out what it means.
            Series("без страховки", tuple(bare), KIND_DOTS, tone=1),
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
        ("Полезные данные (по кластерам)", solution.payload_alloc),
        ("Заголовок VeraCrypt", solution.vc_header),
        ("Метаданные NTFS", solution.metadata_bytes),
        ("Запас на копирование", solution.copy_slack),
        ("Страховочный запас", solution.safety_bytes),
        ("Округление до целых MiB", rounding),
    )
    points = tuple(
        Point(0.0, float(value), f"{name}: {fmt_both(value)}") for name, value in parts
    )
    return Chart(
        f"Контейнер {solution.container_mib} MiB — из чего он сложен",
        Axis(""),
        Axis(""),
        (Series("слагаемые", points, KIND_STACK),),
        note=(
            f"Из них кластерный хвост: {fmt_both(solution.cluster_tail)} — "
            "доплата за округление каждого файла вверх до кластера."
        ),
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
        tip = (
            f"Кластер {fmt_bytes(cluster)} B\n"
            f"Займут: {fmt_both(alloc)}\n"
            f"Хвост: {fmt_both(tail)}"
        )
        point = Point(float(cluster), float(alloc), tip, key=cluster)
        points.append(point)
        if cluster == current:
            chosen.append(point)

    flat = (
        tuple(
            Point(float(cluster), float(logical), f"Логический размер: {fmt_both(logical)}")
            for cluster in choices
        )
        if choices
        else ()
    )
    return Chart(
        "Занятое место от размера кластера",
        Axis("Размер кластера", AXIS_BYTES, log=True),
        Axis("Займут на томе", AXIS_BYTES),
        (
            Series("по кластерам", tuple(points), KIND_LINE_DOTS, tone=0),
            Series("логический размер", flat, KIND_LINE, tone=4),
            Series("выбрано сейчас", tuple(chosen), KIND_DOTS, tone=1),
        ),
        note=(
            f"Файлов: {fmt_bytes(len(sizes))}. Расстояние между линиями — "
            "кластерный хвост, доплата за округление каждого файла."
        ),
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
    return bool(factory_data().samples)
