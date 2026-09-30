"""Chart arithmetic: bounds, ticks, mapping to pixels, hit testing.

No Qt — like everything that computes numbers. Drawing lives in `ui/chart.py`,
and what needs testing is exactly this: where the axis goes, which labels land
on the ticks and which point the cursor hit. None of this can be checked by
eye — a curve drawn in the wrong place looks exactly as convincing as one
drawn right.

Float is allowed here and does not break the rule "only integers in the
calculation path": the calculation path is the container size, and here it is
pixels. No number from here goes back into the model.

The logarithmic axis is **base two, not ten**. All sizes in this program are
multiples of a power of two, and decimal decades would put the labels between
them: "1 000 000 000 B" instead of "1 GiB". For the same reason the step of a
linear byte axis is chosen in fractions of a binary unit, not in round decimal
numbers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .formatting import (
    DEFAULT_UNIT,
    GROUP_SEPARATOR,
    UNIT_AUTO,
    Unit,
    fmt_bytes,
    fmt_with_unit,
    resolve_unit,
)

#: Axis kind. Only the tick step and the tick labels depend on it.
AXIS_PLAIN = "plain"
AXIS_BYTES = "bytes"
AXIS_MIB = "mib"
AXIS_COUNT = "count"

#: Series kind. How to draw it is the widget's decision; the kind is needed
#: here because it decides whether the series takes part in the nearest-point
#: search and how the bounds are computed.
KIND_DOTS = "dots"
KIND_LINE = "line"
#: A line and the dots on it as one series. As two series this would give two
#: legend entries of the same colour about the same thing.
KIND_LINE_DOTS = "line+dots"
KIND_STEPS = "steps"
#: A dot with a stem down to zero. For values where zero is not the edge of the
#: scale but the model itself: the stem shows which way and how far the miss
#: went, and the dot stays a dot — each measurement stands on its own, and they
#: must not be joined by a line.
KIND_STEMS = "stems"
KIND_BARS = "bars"
KIND_STACK = "stack"

#: Chart layout. The usual one is two axes; the bar is one composite value
#: across the full width, which has no axis at all: there is nothing to plot
#: along it, only shares.
LAYOUT_AXES = "axes"
LAYOUT_STACK = "stack"

#: How many ticks to ask of an axis. Not a hard number: the step is rounded to
#: a round one, and there end up being four to nine ticks.
TICK_TARGET = 6

#: Beyond this distance in pixels the cursor counts as belonging to nothing.
#: Otherwise the tooltip sticks to a point half a screen away.
HIT_RADIUS = 18.0


@dataclass(frozen=True)
class Axis:
    """An axis: how to label it and at what scale to run it."""

    title: str
    kind: str = AXIS_PLAIN
    log: bool = False


@dataclass(frozen=True)
class Point:
    """A chart point with what to say about it and where it leads.

    `tip` is the finished tooltip text: building it here and not in the widget
    is right because the tooltip explains a measurement, not a pixel. `key` is
    what the widget hands out on a click: usually a table row number.
    """

    x: float
    y: float
    tip: str = ""
    key: object = None


@dataclass(frozen=True)
class Series:
    """One line, a set of dots or bars.

    `tone` is not a colour but its number: colours are taken from the window
    palette, so that the chart lives in the same theme as the rest of the
    program.
    """

    name: str
    points: tuple[Point, ...]
    kind: str = KIND_DOTS
    tone: int = 0
    #: Whether to show the series. A click on the legend toggles it.
    visible: bool = True

    def with_visible(self, visible: bool) -> "Series":
        return Series(self.name, self.points, self.kind, self.tone, visible)


@dataclass(frozen=True)
class Chart:
    """Everything to draw: axes, series and the caption under them."""

    title: str
    x: Axis
    y: Axis
    series: tuple[Series, ...] = ()
    note: str = ""
    #: Whether to draw the zero line on Y. For residuals it is the model.
    zero_line: bool = False
    layout: str = LAYOUT_AXES
    #: X tick labels when the value is not numeric but categorical: a record,
    #: a file set, a component. The points then stand at integers 0, 1, 2…
    categories: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not any(series.points for series in self.series)


def category_ticks(categories: tuple[str, ...], lo: float, hi: float) -> list[Tick]:
    """Ticks of a categorical axis: one per category, at integer positions.

    They are thinned out when there are more records than labels fit: a
    skipped label is more honest than labels running into each other.
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


# --- bounds ----------------------------------------------------------------


def bounds(series: tuple[Series, ...], axis: str = "x") -> tuple[float, float]:
    """The smallest and largest value along the axis among visible series.

    An empty set gives (0, 1): a zero range has nothing to scale by, and the
    chart must not crash for lack of data — sometimes it simply has not been
    measured yet.
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
    """Widen the bounds so that the outermost points do not stick to the frame.

    On a logarithmic axis the padding is a share of the range of exponents,
    not of values: otherwise at the left edge it would eat a whole octave, and
    at the right it would not be visible.
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
        # A single value: spread the bounds around it, otherwise we divide by
        # a zero range.
        step = abs(hi) * share or 1.0
        return lo - step, hi + step
    margin = (hi - lo) * share
    return lo - margin, hi + margin


# --- ticks -----------------------------------------------------------------


def _nice_step(span: float, count: int) -> float:
    """A round step from the 1-2-5 series giving about `count` ticks.

    The 1-2-5 series is what sets axis ticks apart from home-made ones:
    without it the labels come out like 0.0037 and 0.0074, and the chart at
    once looks machine-made.

    The step taken is the **nearest** by tick count, not the first fitting one
    from above. Rounding up looks logical, but on a range of 137 MiB it jumps
    from 20 straight to 50: instead of seven ticks two are left, and the axis
    stops saying anything about the values in between.
    """
    raw = span / max(count, 1)
    if raw <= 0:
        return 1.0
    magnitude = 10.0 ** math.floor(math.log10(raw))
    steps = [factor * magnitude for factor in (1.0, 2.0, 5.0, 10.0)]
    return min(steps, key=lambda step: abs(span / step - count))


def _byte_step(span: float, count: int, unit: Unit) -> tuple[float, Unit]:
    """The step of a byte axis and the unit to compute the labels in.

    The step is sought in fractions of a binary unit, not in decimal numbers:
    ticks every 4 MiB read well, and every 5 000 000 B do not, although the
    number is rounder.
    """
    resolved = resolve_unit(int(span) if span >= 1 else 1, unit)
    factor = float(resolved.factor or 1)
    return _nice_step(span / factor, count) * factor, resolved


def _decimals(step: float) -> int:
    """How many decimal places keep the step from collapsing in the label."""
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
    # By multiplication, not accumulation: adding the step forty times drifts
    # the value so far that a "0.30000000000000004" label reaches the eye.
    return [index * step for index in range(int(first), int(last) + 1)]


def _log_base(axis: Axis) -> float:
    """The base of a logarithmic axis.

    For bytes it is two: all sizes here are multiples of a power of two, and
    decimal decades would put the labels past them — "1 000 000 000 B" instead
    of "1 GiB". For counts it is ten: a file count is never a power of two, and
    the series 1, 4, 16, 64 reads worse than 1, 10, 100.
    """
    return 2.0 if axis.kind == AXIS_BYTES else 10.0


def _log_values(lo: float, hi: float, base: float, limit: int) -> list[float]:
    """Powers of the base within the range, thinned to a number that fits."""
    low = math.floor(math.log(max(lo, 1e-9), base))
    high = math.ceil(math.log(max(hi, 2e-9), base))
    exponents = [e for e in range(int(low), int(high) + 1) if lo <= base**e <= hi]
    if not exponents:
        return []
    stride = max(1, math.ceil(len(exponents) / max(limit, 1)))
    return [base**e for e in exponents[::stride]]


def _group(value: float, digits: int) -> str:
    """A number with digit group separators and a decimal point.

    A point, as in the tables (`formatting.fmt_mib`): the chart and the table
    beside it must not write one number two ways.
    """
    return format(value, f",.{digits}f").replace(",", GROUP_SEPARATOR)


def _unit_digits(step: float, resolved: Unit) -> int:
    """How many decimal places a label needs for the step to be visible.

    A step of exactly four MiB does not need "4.00": trailing zeros on an axis
    are noise that makes the labels run into each other.
    """
    units = step / float(resolved.factor or 1)
    if abs(units - round(units)) < 1e-9:
        return 0
    return _decimals(units)


def _count_tick(value: float) -> str:
    """A tick label of a logarithmic axis that is **not** a byte axis.

    An integer is printed as an integer, a fraction with the number of decimal
    places at which it is visible at all: the ticks there go by decades, and
    "0.01" without decimal places turns into zero. The share of the volume
    taken by metadata is exactly such a value — from 0.013 % at a terabyte to
    16 % at sixty-four megabytes.
    """
    if value >= 1:
        return fmt_bytes(int(round(value)))
    digits = min(6, int(math.ceil(-math.log10(max(value, 1e-9)))))
    return _group(value, digits)


def _size_tick(value: float) -> str:
    """A tick label of a logarithmic byte axis.

    Its values are powers of two, and in the fitting binary unit each of them
    is an integer: "1 GiB", not "1.000 GiB". Trailing zeros here are not just
    noise — they make the labels run into each other.
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
    """Axis ticks with finished labels."""
    if axis.log:
        values = _log_values(lo, hi, _log_base(axis), max(count, 2) * 2)
        if axis.kind == AXIS_BYTES:
            # On a logarithmic axis each tick has its own unit. A shared one
            # does not fit by definition: the range here is wider than one
            # binary unit — from 512 MiB to a terabyte — and half of the
            # labels would become "0.001".
            return [Tick(value, _size_tick(value)) for value in values]
        return [Tick(value, _count_tick(value)) for value in values]

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


def value_label(
    axis: Axis,
    value: float,
    lo: float,
    hi: float,
    unit: Unit = DEFAULT_UNIT,
    categories: tuple[str, ...] = (),
    count: int = TICK_TARGET,
) -> str:
    """The label of an arbitrary axis value — the one next to the crosshair.

    Computed with the same step as the ticks: the crosshair and the nearest
    tick must have the same number of decimal places, otherwise the same spot
    on the axis is labelled two different ways.
    """
    if categories:
        index = int(round(value))
        return categories[index] if 0 <= index < len(categories) else ""

    if axis.log:
        if axis.kind == AXIS_BYTES:
            return _size_tick(value)
        return _count_tick(value)

    span = hi - lo
    if axis.kind == AXIS_BYTES:
        step, resolved = _byte_step(span, count, unit)
        factor = float(resolved.factor or 1)
        return _group(value / factor, _unit_digits(step, resolved))
    return _group(value, _decimals(_nice_step(span, count)))


def axis_caption(axis: Axis, lo: float, hi: float, unit: Unit = DEFAULT_UNIT) -> str:
    """The axis title together with the unit the ticks are labelled in.

    On a logarithmic byte axis the title has no unit: each tick has its own,
    and a shared label would lie about half of them.
    """
    if axis.kind == AXIS_BYTES and not axis.log:
        resolved = resolve_unit(int(abs(hi)) or 1, unit)
        return f"{axis.title}, {resolved.label}"
    if axis.kind == AXIS_MIB:
        return f"{axis.title}, MiB"
    return axis.title


# --- mapping to pixels -----------------------------------------------------


@dataclass(frozen=True)
class Span:
    """A range of values along one axis and the way to traverse it."""

    lo: float
    hi: float
    log: bool = False

    def share(self, value: float) -> float:
        """Share from the range start: 0 is the left edge, 1 the right."""
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
        """The reverse: from share to value. Needed for rubber-band zoom."""
        if self.log:
            lo, hi = max(self.lo, 1e-9), max(self.hi, 1e-9)
            return 2.0 ** (math.log2(lo) + share * (math.log2(hi) - math.log2(lo)))
        return self.lo + share * (self.hi - self.lo)

    def zoomed(self, first: float, second: float) -> "Span":
        """A new range from two shares — those the selection band gave."""
        low, high = sorted((first, second))
        return Span(self.value(low), self.value(high), self.log)

    def scaled(self, factor: float, anchor: float = 0.5) -> "Span":
        """Widen or shrink around the share `anchor` — this is wheel zoom."""
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
        """Shift by a share of the range — this is pan by dragging."""
        if self.log:
            low, high = math.log2(max(self.lo, 1e-9)), math.log2(max(self.hi, 1e-9))
            step = (high - low) * share
            return Span(2.0 ** (low + step), 2.0 ** (high + step), True)
        step = (self.hi - self.lo) * share
        return Span(self.lo + step, self.hi + step, False)


@dataclass(frozen=True)
class Frame:
    """The chart rectangle in pixels plus the ranges along both axes."""

    left: float
    top: float
    width: float
    height: float
    x: Span
    y: Span

    def px(self, value: float) -> float:
        return self.left + self.width * self.x.share(value)

    def py(self, value: float) -> float:
        # Screen zero is at the top, and the Y axis grows from bottom to top.
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
    """What the cursor hit."""

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
    """The point of the visible series nearest to the cursor — or nothing.

    Distance is measured in pixels, not values: by values the nearest would
    always be a point along the axis whose range is smaller.
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
    """The bar segment under the cursor. The bar lies flat across the width.

    Separate from `nearest`, because a bar has no points — it has extents,
    and hitting it means hitting an area.

    A segment thinner than a pixel cannot be pointed at, and that is not fixed
    here on purpose. Widening its hit zone at the neighbours' expense means
    splitting what is shown from what is queried: the cursor would stand on
    the NTFS metadata while the tooltip spoke about the VeraCrypt header. Such
    segments are explained by the legend, where each component has its exact
    number.
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
