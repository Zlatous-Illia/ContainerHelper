"""Арифметика графиков: границы, деления, отображение в пиксели, попадание.

Без Qt — как и всё, что считает числа. Рисование живёт в `ui/chart.py`, а
проверять надо именно это: где пройдёт ось, какие подписи встанут на делениях и
в какую точку попал курсор. Глазами такое не проверяется вовсе — кривая,
нарисованная мимо, выглядит ровно так же убедительно, как нарисованная верно.

Float здесь допустим и правила «только целые числа в расчётном пути» не
нарушает: расчётный путь — это размер контейнера, а тут пиксели. Ни одно число
отсюда в модель не возвращается.

Логарифмическая ось — **по основанию два, а не десять**. Все размеры в этой
программе кратны степени двойки, и десятичные декады расставили бы подписи
между ними: «1 000 000 000 B» вместо «1 GiB». По той же причине шаг линейной
байтовой оси выбирается в долях кратной единицы, а не в круглых десятичных
числах.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .formatting import (
    DEFAULT_UNIT,
    GROUP_SEPARATOR,
    UNIT_AUTO,
    Unit,
    fmt_bytes,
    fmt_with_unit,
    resolve_unit,
)

#: Вид оси. От него зависит только шаг делений и подпись на них.
AXIS_PLAIN = "plain"
AXIS_BYTES = "bytes"
AXIS_MIB = "mib"
AXIS_COUNT = "count"

#: Вид серии. Как её рисовать, решает виджет; здесь вид нужен затем, что от
#: него зависит, участвует ли серия в поиске ближайшей точки и как считаются
#: границы.
KIND_DOTS = "dots"
KIND_LINE = "line"
#: Линия и точки на ней одной серией. Двумя сериями это давало бы в
#: легенде две записи одного цвета про одно и то же.
KIND_LINE_DOTS = "line+dots"
KIND_STEPS = "steps"
KIND_BARS = "bars"
KIND_STACK = "stack"

#: Разметка графика. Обычная — две оси; полоса — одно составное значение во
#: всю ширину, у которого оси нет вовсе: там нечего откладывать, там доли.
LAYOUT_AXES = "axes"
LAYOUT_STACK = "stack"

#: Сколько делений просить у оси. Не жёсткое число: шаг округляется до
#: круглого, и делений выходит от четырёх до девяти.
TICK_TARGET = 6

#: Дальше этого расстояния в пикселях курсор считается ни к чему не
#: относящимся. Иначе подсказка липнет к точке через полэкрана.
HIT_RADIUS = 18.0


@dataclass(frozen=True)
class Axis:
    """Ось: как подписать и в каком масштабе вести."""

    title: str
    kind: str = AXIS_PLAIN
    log: bool = False


@dataclass(frozen=True)
class Point:
    """Точка графика вместе с тем, что о ней сказать и куда по ней перейти.

    `tip` — готовый текст подсказки: собирать его здесь, а не в виджете,
    правильно потому, что подсказка объясняет замер, а не пиксель. `key` —
    то, что виджет отдаст наружу по щелчку: обычно номер строки таблицы.
    """

    x: float
    y: float
    tip: str = ""
    key: object = None


@dataclass(frozen=True)
class Series:
    """Одна линия, набор точек или столбики.

    `tone` — не цвет, а его номер: цвета берутся из палитры окна, чтобы
    график жил в той же теме, что и остальная программа.
    """

    name: str
    points: tuple[Point, ...]
    kind: str = KIND_DOTS
    tone: int = 0
    #: Показывать ли серию. Щелчок по легенде переключает.
    visible: bool = True

    def with_visible(self, visible: bool) -> "Series":
        return Series(self.name, self.points, self.kind, self.tone, visible)


@dataclass(frozen=True)
class Chart:
    """Всё, что нужно нарисовать: оси, серии и подпись под ними."""

    title: str
    x: Axis
    y: Axis
    series: tuple[Series, ...] = ()
    note: str = ""
    #: Провести ли нулевую линию по Y. Для остатков она и есть модель.
    zero_line: bool = False
    layout: str = LAYOUT_AXES
    #: Подписи делений по X, когда величина не числовая, а перечислимая:
    #: запись, набор файлов, слагаемое. Точки тогда стоят на целых 0, 1, 2…
    categories: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not any(series.points for series in self.series)


def category_ticks(categories: tuple[str, ...], lo: float, hi: float) -> list[Tick]:
    """Деления перечислимой оси: по одному на категорию, на целых позициях.

    Прорежаются, когда записей больше, чем влезает подписей: пропущенная
    подпись честнее налезающих друг на друга.
    """
    if not categories:
        return []
    inside = [index for index in range(len(categories)) if lo <= index <= hi]
    if not inside:
        return []
    stride = max(1, math.ceil(len(inside) / 12))
    return [Tick(float(index), categories[index]) for index in inside[::stride]]


@dataclass(frozen=True)
class Tick:
    value: float
    text: str


# --- границы ---------------------------------------------------------------


def bounds(series: tuple[Series, ...], axis: str = "x") -> tuple[float, float]:
    """Наименьшее и наибольшее значение по оси среди видимых серий.

    Пустой набор даёт (0, 1): нулевой размах не с чем масштабировать, а
    падать из-за отсутствия данных график не должен — их иногда просто ещё
    не сняли.
    """
    values = [
        (point.x if axis == "x" else point.y)
        for item in series
        if item.visible
        for point in item.points
    ]
    if not values:
        return 0.0, 1.0
    return min(values), max(values)


def padded(
    lo: float,
    hi: float,
    log: bool = False,
    share: float = 0.06,
    include_zero: bool = False,
) -> tuple[float, float]:
    """Раздвинуть границы, чтобы крайние точки не липли к рамке.

    На логарифмической оси отступ берётся долей от размаха показателей, а не
    от значений: иначе у левого края он съедал бы целую октаву, а у правого
    не был бы виден.
    """
    if include_zero and not log:
        lo, hi = min(lo, 0.0), max(hi, 0.0)

    if log:
        lo = max(lo, 1e-9)
        hi = max(hi, lo * 2)
        low, high = math.log2(lo), math.log2(hi)
        margin = max((high - low) * share, 0.05)
        return 2.0 ** (low - margin), 2.0 ** (high + margin)

    if hi <= lo:
        # Единственное значение: развести границы вокруг него, иначе делить
        # на нулевой размах.
        step = abs(hi) * share or 1.0
        return lo - step, hi + step
    margin = (hi - lo) * share
    return lo - margin, hi + margin


# --- деления ---------------------------------------------------------------


def _nice_step(span: float, count: int) -> float:
    """Круглый шаг из ряда 1-2-5, дающий примерно `count` делений.

    Ряд 1-2-5 — то, чем деления на осях отличаются от кустарных: без него
    подписи выходят вида 0,0037 и 0,0074, и график сразу выглядит машинным.

    Шаг берётся **ближайший** по числу делений, а не первый подходящий сверху.
    Округление вверх выглядит логично, но на размахе в 137 MiB отправляет с 20
    сразу на 50: вместо семи делений остаётся два, и ось перестаёт что-либо
    говорить о промежуточных значениях.
    """
    raw = span / max(count, 1)
    if raw <= 0:
        return 1.0
    magnitude = 10.0 ** math.floor(math.log10(raw))
    steps = [factor * magnitude for factor in (1.0, 2.0, 5.0, 10.0)]
    return min(steps, key=lambda step: abs(span / step - count))


def _byte_step(span: float, count: int, unit: Unit) -> tuple[float, Unit]:
    """Шаг байтовой оси и единица, в которой считать подписи.

    Шаг ищется в долях кратной единицы, а не в десятичных числах: деления
    через 4 MiB читаются, а через 5 000 000 B — нет, хотя число круглее.
    """
    resolved = resolve_unit(int(span) if span >= 1 else 1, unit)
    factor = float(resolved.factor or 1)
    return _nice_step(span / factor, count) * factor, resolved


def _decimals(step: float) -> int:
    """Сколько знаков после запятой нужно, чтобы шаг не схлопнулся в подписи."""
    if step >= 1:
        return 0
    return min(6, max(0, int(math.ceil(-math.log10(step)))))


def _linear_values(lo: float, hi: float, step: float) -> list[float]:
    if step <= 0:
        return [lo]
    first = math.ceil(lo / step)
    last = math.floor(hi / step)
    if last < first:
        return []
    # Через умножение, а не накоплением: сложение шага сорок раз уводит
    # значение настолько, что подпись «0.30000000000000004» доезжает до глаза.
    return [index * step for index in range(int(first), int(last) + 1)]


def _log_base(axis: Axis) -> float:
    """Основание логарифмической оси.

    Для байтов — двойка: все размеры здесь кратны степени двойки, и десятичные
    декады расставили бы подписи мимо них — «1 000 000 000 B» вместо «1 GiB».
    Для счётных величин — десятка: число файлов степенью двойки не бывает, и
    ряд 1, 4, 16, 64 читается хуже, чем 1, 10, 100.
    """
    return 2.0 if axis.kind == AXIS_BYTES else 10.0


def _log_values(lo: float, hi: float, base: float, limit: int) -> list[float]:
    """Степени основания внутри диапазона, прорежённые до влезающего числа."""
    low = math.floor(math.log(max(lo, 1e-9), base))
    high = math.ceil(math.log(max(hi, 2e-9), base))
    exponents = [e for e in range(int(low), int(high) + 1) if lo <= base**e <= hi]
    if not exponents:
        return []
    stride = max(1, math.ceil(len(exponents) / max(limit, 1)))
    return [base**e for e in exponents[::stride]]


def _group(value: float, digits: int) -> str:
    """Число с разделителями разрядов и запятой в дробной части."""
    text = format(value, f",.{digits}f").replace(",", GROUP_SEPARATOR)
    return text.replace(".", ",") if digits else text


def _unit_digits(step: float, resolved: Unit) -> int:
    """Сколько знаков нужно подписи, чтобы шаг был виден.

    Шаг ровно в четыре MiB не нуждается в «4.00»: нули после запятой на оси —
    шум, из-за которого подписи налезают друг на друга.
    """
    units = step / float(resolved.factor or 1)
    if abs(units - round(units)) < 1e-9:
        return 0
    return _decimals(units)


def _size_tick(value: float) -> str:
    """Подпись деления логарифмической байтовой оси.

    Значения на ней — степени двойки, и в подходящей кратной единице каждое из
    них целое: «1 GiB», а не «1.000 GiB». Нули после запятой тут не просто
    шум — из-за них подписи налезают друг на друга.
    """
    resolved = resolve_unit(int(round(value)) or 1, UNIT_AUTO)
    scaled = value / float(resolved.factor or 1)
    if abs(scaled - round(scaled)) < 1e-9:
        return f"{_group(scaled, 0)} {resolved.label}"
    return fmt_with_unit(int(round(value)), UNIT_AUTO)


def ticks(
    axis: Axis,
    lo: float,
    hi: float,
    unit: Unit = DEFAULT_UNIT,
    count: int = TICK_TARGET,
) -> list[Tick]:
    """Деления оси с готовыми подписями."""
    if axis.log:
        values = _log_values(lo, hi, _log_base(axis), max(count, 2) * 2)
        if axis.kind == AXIS_BYTES:
            # На логарифмической оси единица у каждого деления своя. Общая не
            # годится по определению: диапазон здесь шире одной кратной
            # единицы — от 512 MiB до терабайта, — и половина подписей стала
            # бы «0.001».
            return [Tick(value, _size_tick(value)) for value in values]
        return [Tick(value, fmt_bytes(int(round(value)))) for value in values]

    span = hi - lo
    if axis.kind == AXIS_BYTES:
        step, resolved = _byte_step(span, count, unit)
        digits = _unit_digits(step, resolved)
        factor = float(resolved.factor or 1)
        return [
            Tick(value, _group(value / factor, digits))
            for value in _linear_values(lo, hi, step)
        ]

    step = _nice_step(span, count)
    digits = _decimals(step)
    return [
        Tick(value, _group(value, digits)) for value in _linear_values(lo, hi, step)
    ]


def axis_caption(axis: Axis, lo: float, hi: float, unit: Unit = DEFAULT_UNIT) -> str:
    """Заголовок оси вместе с единицей, в которой подписаны деления.

    На логарифмической байтовой оси единицы в заголовке нет: она у каждого
    деления своя, и общая подпись врала бы про половину из них.
    """
    if axis.kind == AXIS_BYTES and not axis.log:
        resolved = resolve_unit(int(abs(hi)) or 1, unit)
        return f"{axis.title}, {resolved.label}"
    if axis.kind == AXIS_MIB:
        return f"{axis.title}, MiB"
    return axis.title


# --- отображение в пиксели -------------------------------------------------


@dataclass(frozen=True)
class Span:
    """Диапазон значений по одной оси и способ его пройти."""

    lo: float
    hi: float
    log: bool = False

    def share(self, value: float) -> float:
        """Доля от начала диапазона: 0 — левый край, 1 — правый."""
        if self.log:
            lo, hi = max(self.lo, 1e-9), max(self.hi, 1e-9)
            if hi <= lo:
                return 0.5
            return (math.log2(max(value, 1e-9)) - math.log2(lo)) / (
                math.log2(hi) - math.log2(lo)
            )
        if self.hi <= self.lo:
            return 0.5
        return (value - self.lo) / (self.hi - self.lo)

    def value(self, share: float) -> float:
        """Обратно: из доли в значение. Нужно для зума рамкой."""
        if self.log:
            lo, hi = max(self.lo, 1e-9), max(self.hi, 1e-9)
            return 2.0 ** (math.log2(lo) + share * (math.log2(hi) - math.log2(lo)))
        return self.lo + share * (self.hi - self.lo)

    def zoomed(self, first: float, second: float) -> "Span":
        """Новый диапазон по двум долям — тем, что дала рамка выделения."""
        low, high = sorted((first, second))
        return Span(self.value(low), self.value(high), self.log)

    def scaled(self, factor: float, anchor: float = 0.5) -> "Span":
        """Раздвинуть или сжать вокруг доли `anchor` — это зум колесом."""
        pivot = self.value(anchor)
        if self.log:
            low, high = math.log2(max(self.lo, 1e-9)), math.log2(max(self.hi, 1e-9))
            mid = math.log2(max(pivot, 1e-9))
            return Span(
                2.0 ** (mid + (low - mid) * factor),
                2.0 ** (mid + (high - mid) * factor),
                True,
            )
        return Span(
            pivot + (self.lo - pivot) * factor,
            pivot + (self.hi - pivot) * factor,
            False,
        )

    def shifted(self, share: float) -> "Span":
        """Сдвинуть на долю размаха — это панорама перетаскиванием."""
        if self.log:
            low, high = math.log2(max(self.lo, 1e-9)), math.log2(max(self.hi, 1e-9))
            step = (high - low) * share
            return Span(2.0 ** (low + step), 2.0 ** (high + step), True)
        step = (self.hi - self.lo) * share
        return Span(self.lo + step, self.hi + step, False)


@dataclass(frozen=True)
class Frame:
    """Прямоугольник графика в пикселях плюс диапазоны по обеим осям."""

    left: float
    top: float
    width: float
    height: float
    x: Span
    y: Span

    def px(self, value: float) -> float:
        return self.left + self.width * self.x.share(value)

    def py(self, value: float) -> float:
        # Экранный ноль сверху, а ось Y растёт снизу вверх.
        return self.top + self.height * (1.0 - self.y.share(value))

    def at_px(self, pixel: float) -> float:
        return self.x.value((pixel - self.left) / self.width if self.width else 0.5)

    def at_py(self, pixel: float) -> float:
        share = (pixel - self.top) / self.height if self.height else 0.5
        return self.y.value(1.0 - share)

    def contains(self, x_px: float, y_px: float) -> bool:
        return (
            self.left <= x_px <= self.left + self.width
            and self.top <= y_px <= self.top + self.height
        )


@dataclass(frozen=True)
class Hit:
    """Во что попал курсор."""

    series: Series
    point: Point
    distance: float


def nearest(
    frame: Frame,
    series: tuple[Series, ...],
    x_px: float,
    y_px: float,
    radius: float = HIT_RADIUS,
) -> Hit | None:
    """Ближайшая к курсору точка видимых серий — или ничего.

    Расстояние меряется в пикселях, а не в значениях: по значениям ближайшей
    всегда оказывалась бы точка на той оси, у которой размах мельче.
    """
    best: Hit | None = None
    for item in series:
        if not item.visible or item.kind == KIND_STACK:
            continue
        for point in item.points:
            dx = frame.px(point.x) - x_px
            dy = frame.py(point.y) - y_px
            distance = math.hypot(dx, dy)
            if distance <= radius and (best is None or distance < best.distance):
                best = Hit(item, point, distance)
    return best


def stack_hit(
    frame: Frame,
    series: Series,
    x_px: float,
    y_px: float,
) -> Point | None:
    """Сегмент полосы под курсором. Полоса лежит горизонтально во всю ширину.

    Отдельно от `nearest`, потому что у полосы нет точек — есть протяжённости,
    и попасть в неё значит попасть в площадь.

    Сегмент тоньше пикселя навести курсором нельзя, и здесь это не
    исправляется намеренно. Расширить его зону попадания за счёт соседей —
    значит развести показанное и опрошенное: курсор стоял бы на метаданных
    NTFS, а подсказка говорила про заголовок VeraCrypt. Такие сегменты
    объясняет легенда, где у каждого слагаемого стоит его точное число.
    """
    if not frame.contains(x_px, y_px) or not series.points:
        return None
    total = sum(max(point.y, 0.0) for point in series.points)
    if total <= 0:
        return None
    offset = frame.left
    for point in series.points:
        span = frame.width * max(point.y, 0.0) / total
        if x_px <= offset + span:
            return point
        offset += span
    return series.points[-1]
