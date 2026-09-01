"""Сборка графиков из замеров и моделей. Что рисовать — здесь, чем — в `ui/`.

Без Qt по той же причине, что и `plot.py`: содержимое графика — это данные, а
не оформление. Проверять надо, какие точки на нём оказались и что написано в
подсказке, а для этого окно не нужно.
"""

from __future__ import annotations

from typing import Sequence

from .factory import factory_data
from .formatting import fmt_both, fmt_bytes, plural, size_label
from .model import (
    MIB,
    VC_HEADER_BYTES,
    CopySlackModel,
    NtfsModel,
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
    ntfs_cross_check,
    ntfs_points,
    slack_cross_check,
    slack_samples,
)

#: Сколько точек брать на кривую модели. Модель кусочно-линейна по объёму, а
#: ось логарифмическая — на ней прямой отрезок становится дугой, и двумя
#: концами его не нарисовать: линия пройдёт мимо собственных замеров.
CURVE_SAMPLES = 160


def _own(store) -> list[Record]:
    """Записи, снятые здесь: и о копировании, и калибровочные."""
    return [*store.records, *store.calibration]


def _is_own(record: Record, own: Sequence[Record]) -> bool:
    """Своя запись или заводская.

    Сравнение по тождеству, а не по имени: заводская запись синтезируется на
    лету из ресурсов пакета, и в `records`/`calibration` её нет вовсе, а имя
    человек волен написать какое угодно, в том числе «Заводская».
    """
    return any(record is item for item in own)


def _geometric(lo: float, hi: float, count: int = CURVE_SAMPLES) -> list[float]:
    """Точки, равномерные по логарифму: столько же в каждой октаве."""
    if lo <= 0 or hi <= lo:
        return [lo, hi]
    ratio = (hi / lo) ** (1.0 / max(count - 1, 1))
    return [lo * ratio**index for index in range(count)]


# --- метаданные NTFS -------------------------------------------------------


#: Сколько непокрытых размеров называть поимённо. Дальше список перестаёт
#: читаться и начинает вытеснять саму подпись.
UNCOVERED_SHOWN = 5


def uncovered(store, sizes: Sequence[int]) -> list[int]:
    """Рекомендованные размеры, на которых нет ни своего замера, ни заводского.

    Именно там модель ведёт прямую через пустое место, и туда же стоит снять
    следующий замер. Своё, отключённое кнопкой «К заводскому», покрытием не
    считается только если и заводского на этом размере нет.
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
    """Измеренные метаданные и ломаная модели поверх них.

    `sizes` — список рекомендуемых размеров; по нему подпись говорит, какие из
    них не покрыты ничем. Отдельной панелью это уже стояло — «лента покрытия»,
    три ряда точек, — и оказалось лишним: своё против заводского на кривой уже
    видно по цвету, а всё, что лента добавляла сверх этого, укладывается в
    одну строку. Таблица на «Калибровке» говорит то же самое подробнее и с
    кнопками.
    """
    own = _own(store)
    records = store.all_for_model()
    model = NtfsModel(ntfs_points(records))

    mine: list[Point] = []
    factory: list[Point] = []
    for record in records:
        overhead = record.ntfs_bytes
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
    """Проверка исключением: насколько модель промахнулась бы без этой точки.

    Остатки от самой модели рисовать бессмысленно — кусочно-линейная кривая
    проходит ровно через свои замеры, и отклонение везде вышло бы нулевым.
    Поэтому каждая точка проверяется моделью, построенной без неё; ровно так
    же считается и совет по страховке.
    """
    checks = [
        check
        for check in ntfs_cross_check(store.all_for_model())
        if check.record.mounted_bytes
    ]
    volumes = [check.record.mounted_bytes for check in checks]
    # Крайние точки — особый случай: без них модели не между чем вести прямую,
    # и она вынуждена экстраполировать базовым наклоном. Промах там в сотни раз
    # больше и мерит другое, поэтому серия отдельная и по умолчанию спрятана:
    # иначе один выброс в −433 MiB прижимает к нулю всё остальное, а всё
    # остальное тут и есть предмет разговора. Щелчок по легенде её вернёт.
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
            # Стеблями, а не голыми точками: ноль здесь — сама модель, и
            # стебель от неё до точки показывает, куда и насколько промах, не
            # заставляя глаз мерить расстояние до линии.
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
    """Какую долю тома съедают метаданные.

    Кривая в байтах отвечает на другой вопрос — «сколько их», — и на фоне тома
    в гигабайтах разница между 0,2 % и 0,4 % там толщиной в линию. А доля и
    есть то, чем метаданные меряют на глаз: «сколько тома уйдёт не на данные».

    С наклонами отрезков это тоже разные величины: наклон говорит, как быстро
    метаданные растут на участке, а доля — сколько их всего на этом размере.
    Ровный участок наклона и ровный участок доли — не одно и то же.
    """
    own = _own(store)
    records = store.all_for_model()
    model = NtfsModel(ntfs_points(records))

    mine: list[Point] = []
    factory: list[Point] = []
    for record in records:
        overhead = record.ntfs_bytes
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
        # Логарифмическая и по вертикали: доля расходится на три порядка — от
        # 0,013 % на терабайтном томе до 16 % на шестидесяти четырёх
        # мегабайтах, — и на линейной оси всё, кроме самых мелких томов,
        # ложится в одну линию у нуля.
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
    """Наклон каждого отрезка ломаной — форма кривой, а не её ошибка.

    На самой кривой этого не разглядеть: на фоне тома в гигабайтах разница
    между 0.128 % и 0.324 % — толщина линии. А это и есть ступени $LogFile.
    """
    model = NtfsModel(ntfs_points(store.all_for_model()))
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
        # Замкнуть последнюю ступень, иначе она обрывается на предпоследнем
        # замере и выглядит короче, чем есть.
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


# --- запас на копирование --------------------------------------------------


def slack_curve(store) -> Chart:
    """Измеренный запас против числа файлов и прямая модели."""
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
    """Проверка исключением для запаса — та же логика, что и у метаданных."""
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


# --- прогноз против факта --------------------------------------------------


def forecast_misses(store) -> Chart:
    """Что обещал расчёт против того, чего хватило бы впритык.

    Две величины рядом не для полноты: промах в +5 MiB одинаково выглядит и
    когда модель точна при страховке в 5 MiB, и когда модель занизила на три,
    а восемь мегабайт страховки это скрыли. Второе — предвестник аварии,
    который выглядит здоровым.
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
            # Точками, а не столбиками: столбик поверх столбика читается как
            # «часть целого», а это отдельная величина того же промаха, снятая
            # с другого места. Ниже нуля точка стоит одна, и подпись под
            # графиком проговаривает, что она означает.
            Series("без страховки", tuple(bare), KIND_DOTS, tone=1),
        ),
        note=note,
        zero_line=True,
        categories=tuple(labels),
    )


# --- текущий расчёт --------------------------------------------------------


def container_breakdown(solution: Solution) -> Chart:
    """Полоса: из чего сложен посчитанный контейнер.

    Тонкие слагаемые тут не видны, и это и есть сообщение: заголовок VeraCrypt
    на терабайтном контейнере — волосок. Числа у каждого слагаемого стоят в
    легенде, потому что навести на волосок курсором нельзя.
    """
    rounding = max(solution.predicted_left_bytes - solution.safety_bytes, 0)
    parts = (
        ("Полезные данные (по кластерам)", solution.payload_alloc),
        ("Заголовок VeraCrypt", solution.vc_header),
        ("Метаданные NTFS", solution.ntfs_bytes),
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
    """Во что обойдётся выбор размера кластера именно этим данным.

    Единственный график, который считается по настоящим размерам выбранных
    файлов, а не по модели. И самый крупный эффект во всей программе: на
    мелких файлах между 512 и 65536 разница выходит в разы.
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
    """График без данных, но с объяснением, чего именно не хватает.

    Пустое место с надписью «замеров нет» честнее пустого места без надписи:
    иначе непонятно, программа сломалась или мерить ещё нечего.
    """
    return Chart(title, Axis(""), Axis(""), (), note=note)


def has_factory_slack() -> bool:
    """Есть ли заводские замеры запаса. Пусто — рисовать вторую серию нечем."""
    return bool(factory_data().samples)
