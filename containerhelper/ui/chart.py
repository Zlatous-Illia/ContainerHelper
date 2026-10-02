"""QPainter chart widget: the canvas only. All arithmetic is in `plot.py`.

Own drawing, not a library, was chosen for three things ready-made ones lack:
labels follow the unit chosen in the window (B/KiB/MiB/GiB), colours come from
the window palette, so the theme is the same as in the rest of the program,
and the chart arithmetic itself lives one layer below and is tested without
Qt — like everything else that computes numbers.

One and the same `_render` draws both to the screen and to a file: otherwise
the saved image would one day drift from the one shown, and there would be
nothing to notice it by.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QMenu,
    QRubberBand,
    QToolButton,
    QToolTip,
    QWidget,
)

from ..formatting import DEFAULT_UNIT, Unit
from ..i18n import tr
from ..plot import (
    KIND_BARS,
    KIND_LINE,
    KIND_LINE_DOTS,
    KIND_STEMS,
    KIND_STEPS,
    LAYOUT_STACK,
    Chart,
    Frame,
    Series,
    Span,
    axis_caption,
    bounds,
    category_ticks,
    nearest,
    padded,
    stack_hit,
    ticks,
    value_label,
)

#: Series tones. The window palette won't do here: it has no distinguishable
#: colours — it holds the background, the text and the highlight, and there
#: can be up to six series. So hues are taken, and saturation and brightness
#: are matched to the theme, so that lines don't glow on a dark background and
#: don't fade on a light one.
TONE_HUES = (210, 25, 145, 275, 45, 320)

#: Margins around the plot area on top of the room for labels.
PADDING = 10
DOT_RADIUS = 3.5
LEGEND_BOX = 11
LEGEND_GAP = 16

#: How much one wheel notch zooms.
WHEEL_STEP = 0.82

#: Below this the rubber band counts as a slip, not a selection: otherwise any
#: click on a point would also zoom into it all the way.
DRAG_THRESHOLD = 8

#: Labels of the detach button. Words, not an icon: an icon would need a
#: tooltip to explain it, and the tooltip would need hovering over. Short,
#: because the buttons sit on one line with the title and take width from it:
#: «Отсоединить» ate a third of the title of a chart in the grid. The full
#: phrase is in the tooltip and in the menu, where there is room to spare.
#: Catalog keys, translated where shown.
DETACH_TEXT = "chart.detach"
RETURN_TEXT = "chart.return"

#: Reorder buttons are arrows: the words "earlier" and "later" don't fit in the
#: corner, and an arrow pointing where the chart will actually go explains
#: itself. The column layout has two of them, the grid four: there "up" and
#: "left" are different moves, and one pair of arrows can't express them.
MOVE_LEFT = "←"
MOVE_RIGHT = "→"
MOVE_UP = "↑"
MOVE_DOWN = "↓"

#: Below this the plot area stops being an area: tick labels run into each
#: other, and the curve turns into a short stroke. This minimum does not suit
#: the breakdown bar — it has nothing to plot vertically, and the minimum
#: would keep it from shrinking to its own height.
MIN_WIDTH = 320
MIN_HEIGHT = 220

#: How many lines the Y axis label gets. Two, not one:
#: «Измерено минус модель, B» is 288 pixels, while the plot area in a
#: three-chart window is 186 high, and the label was cut off right in the
#: middle of a word. Three lines would already eat a noticeable part of the
#: width.
Y_CAPTION_LINES = 2


class ChartView(QWidget):
    """One chart: axes, series, legend, tooltip and zoom."""

    #: The point that was clicked: its key and its ready-made description. The
    #: key, so the window knows which record this is about; the description, so
    #: it need not be built a second time where the data is no longer at hand.
    pointPicked = Signal(object, str)

    #: The zoom changed: by zooming, panning or a reset. Charts with a shared X
    #: axis keep together through it.
    rangeChanged = Signal()

    #: The cursor settled on an X value — or left the chart (the second
    #: signal). Through them the crosshair is repeated on neighbouring charts
    #: with a shared axis: the hump in the residuals and the step in the slope
    #: sit at the same volume size, and the only way to see that is one line
    #: through both charts.
    cursorMoved = Signal(float)
    cursorLeft = Signal()

    #: A request to detach the chart into its own window or to return it. The
    #: window decides: the widget doesn't know where it lives or where to
    #: return it to.
    detachRequested = Signal()

    #: A request to move the chart: −1 is earlier, +1 is later. The window
    #: keeps the order; the chart only knows it is being asked to move.
    moveRequested = Signal(int)

    def __init__(self, chart: Chart | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chart = chart
        self._unit: Unit = DEFAULT_UNIT
        #: Manual zoom. None means fitted to the data; a reset looks the same.
        self._x: Span | None = None
        self._y: Span | None = None
        self._hidden: set[str] = set()
        self._legend_boxes: list[tuple[QRectF, str]] = []
        self._band: QRubberBand | None = None
        self._press: QPointF | None = None
        self._pan: QPointF | None = None
        #: Where the cursor is. None means it left the widget and there is no
        #: crosshair.
        self._cursor: QPointF | None = None
        #: The X value that came from a neighbouring chart with a shared axis.
        #: Drawn as a single vertical line: there is no cursor of its own here.
        self._linked_x: float | None = None
        #: How many points ended up out of frame after a data update. Zero
        #: means there is nothing to say.
        self._outside = 0
        #: Whether the chart lives in its own window and whether it can be sent
        #: there. The window sets these; here they only drive the button text
        #: and the menu item.
        self.detached = False
        self.detachable = False
        #: The last `set_place` arguments: the move tooltips depend on the
        #: layout, and a language switch has to word them again.
        self._place: tuple[int, int, int] | None = None

        # Buttons, not only menu items: the menu has to be found first — by a
        # double click or the middle button — while moving and detaching a
        # chart is the first thing done with it in a window of four.
        #: Where to move and by how many places. The window sets the step: it
        #: alone knows how many charts are in a row — and "up" in the grid is
        #: two places back, not one.
        self._steps: dict[str, int] = {}
        self.left_button = self._move_button(MOVE_LEFT, "left")
        self.right_button = self._move_button(MOVE_RIGHT, "right")
        self.up_button = self._move_button(MOVE_UP, "up")
        self.down_button = self._move_button(MOVE_DOWN, "down")
        self.detach_button = self._corner_button(
            tr(DETACH_TEXT), "", self.detachRequested.emit
        )
        #: Order in the corner, left to right. Detach is rightmost: it takes
        #: the chart out of the window, while moving keeps it there.
        self._corner = (
            self.left_button,
            self.right_button,
            self.up_button,
            self.down_button,
            self.detach_button,
        )

        self.setMinimumSize(MIN_WIDTH, MIN_HEIGHT)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        # The right button is taken by panning, and the menu must not open on
        # it: otherwise it pops up over the chart on every pan.
        self.setContextMenuPolicy(Qt.PreventContextMenu)
        # The chart can also come into the constructor, not only through
        # set_chart: the minimum height depends on the chart's layout, and it
        # has to be set on both paths.
        self._apply_minimum()

    # --- corner buttons ----------------------------------------------------

    def _corner_button(self, text: str, tip: str, slot) -> QToolButton:
        button = QToolButton(self)
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.NoFocus)
        # The default padding is meant for a toolbar: one arrow took 44
        # pixels, and three buttons a third of a chart's width in the grid.
        button.setStyleSheet("QToolButton { padding: 0px 4px; }")
        button.setText(text)
        if tip:
            button.setToolTip(tip)
        button.clicked.connect(slot)
        button.hide()
        return button

    def _move_button(self, text: str, where: str) -> QToolButton:
        # `clicked` comes with a boolean "is it checked", and without an empty
        # first parameter it lands in `where`: the button gets pressed but
        # does nothing — silently, because there is simply no direction
        # "False".
        button = self._corner_button(
            text, "", lambda _checked=False, name=where: self._move(name)
        )
        # Width fitted to the arrow itself, not to what QToolButton considers
        # decent for a toolbar: four arrows by its measure ate two hundred
        # pixels of the title in the grid.
        metrics = QFontMetricsF(button.font())
        button.setFixedWidth(int(metrics.horizontalAdvance(text)) + 10)
        return button

    def _move(self, where: str) -> None:
        step = self._steps.get(where, 0)
        if step:
            self.moveRequested.emit(step)

    def set_place(self, place: int, count: int, columns: int = 1) -> None:
        """Where the chart stands in the window and how that is laid out.

        From this it follows where the chart can move. In the column layout
        that is "up" and "down" by one place; in the grid "up" is a whole row
        back, while "left" is one place, and the two must not be confused: a
        pair of arrows that walked the order swapped a chart in the grid now
        with its right-hand neighbour, now with the end of the previous row.

        A detached chart does not move at all — it is not in the window.
        """
        self._place = (place, count, columns)
        row_place = place % columns
        self._steps = {
            "left": -1,
            "right": 1,
            "up": -columns,
            "down": columns,
        }
        grid = columns > 1
        movable = count > 1 and not self.detached
        allowed = {
            "left": grid and row_place > 0,
            "right": grid and row_place < columns - 1 and place + 1 < count,
            "up": place - columns >= 0,
            "down": place + columns < count,
        }
        wording = {
            "left": "chart.move.left",
            "right": "chart.move.right",
            "up": "chart.move.up.grid" if grid else "chart.move.up.column",
            "down": "chart.move.down.grid" if grid else "chart.move.down.column",
        }
        for where, button in (
            ("left", self.left_button),
            ("right", self.right_button),
            ("up", self.up_button),
            ("down", self.down_button),
        ):
            # Left and right are not shown in the column layout at all: there
            # are no horizontal neighbours there, and a disabled button would
            # promise a move that never happens.
            button.setVisible(movable and (grid or where in ("up", "down")))
            button.setEnabled(allowed[where])
            button.setToolTip(tr(wording[where]))
        self._place_buttons()
        self.updateGeometry()
        self.update()

    def set_detachable(self, detachable: bool) -> None:
        self.detachable = detachable
        self._refresh_button()

    def set_detached(self, detached: bool) -> None:
        self.detached = detached
        self._refresh_button()

    def _refresh_button(self) -> None:
        self.detach_button.setText(tr(RETURN_TEXT if self.detached else DETACH_TEXT))
        self.detach_button.setToolTip(
            tr("chart.return.tip" if self.detached else "chart.detach.tip")
        )
        self.detach_button.setVisible(self.detachable or self.detached)
        self._place_buttons()
        self.updateGeometry()
        self.update()

    def _place_buttons(self) -> None:
        """The buttons sit in the top right corner, in the title band.

        `isVisibleTo`, not `isVisible`: while the widget is not yet visible,
        its children are hidden too, and a button placed before the first
        show would stay in the corner at (0, 0) — nothing would move it
        afterwards, since the size never changed.
        """
        right = self.width() - PADDING // 2
        for button in reversed(self._corner):
            button.adjustSize()
            if not button.isVisibleTo(self):
                continue
            right -= button.width()
            button.move(max(right, 0), 1)

    def _button_width(self) -> float:
        """How much width the buttons take. Zero means there are none."""
        return float(
            sum(
                button.width()
                for button in self._corner
                if button.isVisibleTo(self)
            )
        )

    def _head_height(self) -> float:
        """Band height above the plot area: the title and buttons sit in it."""
        line = self._metrics().height() if (self._chart and self._chart.title) else 0.0
        buttons = [
            float(button.height())
            for button in self._corner
            if button.isVisibleTo(self)
        ]
        return max([line, *buttons])

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt's name
        self._place_buttons()
        super().resizeEvent(event)

    def retranslate(self) -> None:
        """Word the corner buttons again; the painted text follows on the
        next paint, and the menu is built anew each time anyway.

        The chart itself — title, series, axes — is data: the window brings
        a fresh one built in the new language.
        """
        self._refresh_button()
        if self._place is not None:
            self.set_place(*self._place)
        self.update()

    def changeEvent(self, event) -> None:  # noqa: N802 — Qt's name
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    # --- content -----------------------------------------------------------

    def set_chart(self, chart: Chart | None, keep_view: bool = False) -> None:
        """Replace the data.

        Hidden series are taken from the chart itself: it alone knows which
        of them gets in the way by default. Working them out again here would
        mean remembering every chart in two places.

        `keep_view` is an update with the same data, only fresher: the zoom
        and the hidden series stay. Without it every new measurement reset
        the window to the full view, and examining a stretch of the curve
        during collection was impossible altogether — the picture jumped on
        every step.
        """
        previous = tuple(self._chart.series) if self._chart else ()
        self._chart = chart
        if keep_view:
            self._keep_hidden(previous, chart)
        else:
            self._x = self._y = None
            self._hidden = {
                item.key for item in (chart.series if chart else ()) if not item.visible
            }
        self._apply_minimum()
        self._outside = self._count_outside()
        if self._outside and not self._points_in_view():
            # Not a single point is left in frame: there is nothing to hold the
            # zoom to any more — it shows an empty area, and there is no way
            # to tell that from "no measurements".
            self._x = self._y = None
            self._outside = 0
        self.updateGeometry()
        self.update()

    def _apply_minimum(self) -> None:
        """Breakdown bar: own height; the rest: the common plot-area minimum.

        An explicit widget minimum is stronger than `minimumSizeHint`: with
        the common 220 pixels a compact chart cannot shrink to its own height,
        and the splitter gives it exactly as much as a real chart. That showed
        on the coverage strip, since removed, which needed only 156.
        """
        chart = self._chart
        compact = chart is not None and chart.layout == LAYOUT_STACK
        self.setMinimumHeight(self.sizeHint().height() if compact else MIN_HEIGHT)

    def _keep_hidden(self, previous: tuple[Series, ...], chart: Chart | None) -> None:
        """Carry the hidden series over to the new data.

        What was hidden by a click stays hidden; a series that appears comes
        with what the chart itself thinks of it. Otherwise «край диапазона»
        would crawl back out on every update — or, the other way round, a
        series hidden by hand would not stay hidden if the chart showed it by
        default.

        By the series key, not its name: the name changes with the language,
        and a switch would bring every hidden series back.
        """
        known = {item.key for item in previous}
        fresh = tuple(chart.series) if chart else ()
        keys = {item.key for item in fresh}
        self._hidden = {key for key in self._hidden if key in keys} | {
            item.key
            for item in fresh
            if not item.visible and item.key not in known
        }

    def _count_outside(self) -> int:
        """How many points fall outside the current zoom."""
        if self._x is None and self._y is None:
            return 0
        x_span, y_span = self._x, self._y
        outside = 0
        for item in self.series():
            if not item.visible:
                continue
            for point in item.points:
                if x_span is not None and not x_span.lo <= point.x <= x_span.hi:
                    outside += 1
                elif y_span is not None and not y_span.lo <= point.y <= y_span.hi:
                    outside += 1
        return outside

    def _points_in_view(self) -> bool:
        total = sum(len(item.points) for item in self.series() if item.visible)
        return total > self._outside

    def set_unit(self, unit: Unit) -> None:
        self._unit = unit
        self.update()

    def chart(self) -> Chart | None:
        return self._chart

    def reset_zoom(self) -> None:
        self._x = self._y = None
        self.update()
        self.rangeChanged.emit()

    @property
    def zoomed(self) -> bool:
        return self._x is not None or self._y is not None

    def x_span(self) -> Span | None:
        """Manual zoom on X. None means fitted to the data."""
        return self._x

    def set_linked_cursor(self, value: float | None) -> None:
        """Show a neighbour's crosshair: one vertical line at its X value."""
        if self._linked_x == value:
            return
        self._linked_x = value
        self.update()

    def apply_x(self, span: Span | None) -> None:
        """Take the X zoom from a neighbour without emitting a signal back.

        Without the silence, charts with a shared axis would loop: the first
        would tell the second, the second the first, and so on until the
        stack overflows.
        """
        if self._x == span:
            return
        self._x = span
        self.update()

    def series(self) -> tuple[Series, ...]:
        """Series, accounting for those switched off by a legend click."""
        if self._chart is None:
            return ()
        return tuple(
            item.with_visible(item.key not in self._hidden)
            for item in self._chart.series
        )

    # --- geometry ----------------------------------------------------------

    def _metrics(self) -> QFontMetricsF:
        return QFontMetricsF(self.font())

    def frame(self) -> Frame | None:
        """The plot area in pixels together with the axis ranges.

        The left margin is measured by the widest label, not chosen by eye:
        one chosen by eye will cut off "1 099 511 627 776" exactly when that
        number appears.
        """
        chart = self._chart
        if chart is None:
            return None

        metrics = self._metrics()
        line = metrics.height()
        series = self.series()

        x_span, y_span = self._spans(chart, series)
        y_ticks = ticks(chart.y, y_span.lo, y_span.hi, self._unit)
        widest = max((metrics.horizontalAdvance(t.text) for t in y_ticks), default=0.0)

        # Line spacing, not font height: on two lines the label takes a couple
        # of pixels more, and by those it would run over the tick labels.
        left = (
            PADDING
            + widest
            + 6
            + metrics.lineSpacing() * self._y_caption_lines(chart, y_span)
        )
        right = PADDING
        top = PADDING + self._head_height()
        note = self._note_height(chart)
        bottom = PADDING + line * 2 + 6 + note
        if chart.series:
            bottom += line + 6

        if chart.layout == LAYOUT_STACK:
            left = PADDING
            bottom = PADDING + line + 6 + note
            if chart.series:
                bottom += (line + 4) * len(chart.series[0].points)

        width = max(self.width() - left - right, 1.0)
        height = max(self.height() - top - bottom, 1.0)
        return Frame(left, top, width, height, x_span, y_span)

    def _y_caption_room(self) -> float:
        """How much vertical room the rotated Y axis label has.

        Measured against the whole widget, not the plot area: the label
        stands to the side and does not belong to the area, and in a
        three-chart window the area is half as tall as its own label.
        """
        return max(self.height() - PADDING * 2, 1.0)

    def _y_caption_lines(self, chart: Chart, y_span: Span) -> int:
        """How many lines the Y axis label takes: one line or two."""
        caption = axis_caption(chart.y, y_span.lo, y_span.hi, self._unit)
        if not caption:
            return 0
        if self._metrics().horizontalAdvance(caption) <= self._y_caption_room():
            return 1
        return Y_CAPTION_LINES

    def _spans(self, chart: Chart, series: tuple[Series, ...]) -> tuple[Span, Span]:
        """Axis ranges: manual if zoomed, otherwise from the data."""
        if self._x is not None and self._y is not None:
            return self._x, self._y

        x_lo, x_hi = bounds(series, "x")
        y_lo, y_hi = bounds(series, "y")
        if chart.categories:
            x_lo, x_hi = -0.5, max(len(chart.categories) - 0.5, 0.5)
        else:
            x_lo, x_hi = padded(x_lo, x_hi, chart.x.log)
        # Bars grow from zero, and cutting zero off would lie about their
        # length.
        with_zero = chart.zero_line or any(s.kind == KIND_BARS for s in series)
        y_lo, y_hi = padded(y_lo, y_hi, chart.y.log, include_zero=with_zero)
        return (
            self._x or Span(x_lo, x_hi, chart.x.log),
            self._y or Span(y_lo, y_hi, chart.y.log),
        )

    # --- colours -----------------------------------------------------------

    def _dark(self) -> bool:
        return self.palette().window().color().lightness() < 128

    def _tone(self, index: int) -> QColor:
        hue = TONE_HUES[index % len(TONE_HUES)]
        return (
            QColor.fromHsv(hue, 150, 235)
            if self._dark()
            else QColor.fromHsv(hue, 205, 165)
        )

    def _ink(self, alpha: int = 255) -> QColor:
        colour = QColor(self.palette().text().color())
        colour.setAlpha(alpha)
        return colour

    # --- drawing -----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 — Qt's name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().window())
        self._render(painter, live=True)
        painter.end()

    def _render(self, painter: QPainter, live: bool = False) -> None:
        """Draw the chart. `live` covers what must not end up in a file.

        The crosshair and the highlight of the point under the cursor are
        mouse state, not the chart: in a copied image they would mean that
        something was measured there, though it is just where the cursor
        happened to be.
        """
        chart = self._chart
        frame = self.frame()
        if chart is None or frame is None:
            return

        metrics = self._metrics()
        painter.setPen(self._ink())
        if chart.title:
            # The title band is measured from the top edge of the widget, not
            # upwards from the padding: with `PADDING - height` it started two
            # pixels above zero, and the tops of the letters were cut off — by
            # eye it looked like the splitter between charts creeping over it
            # from above.
            #
            # The title is centred in what the buttons leave, not in the
            # widget: narrowing the width on both sides would give the buttons
            # twice the room they take — in the grid the charts are half as
            # wide, and the title started hiding behind an ellipsis for no
            # reason.
            room = max(self.width() - self._button_width() - PADDING, 40.0)
            painter.drawText(
                QRectF(0, 0, room, PADDING + self._head_height()),
                Qt.AlignHCenter | Qt.AlignVCenter,
                metrics.elidedText(chart.title, Qt.ElideRight, room),
            )

        if chart.empty:
            painter.setPen(self._ink(150))
            painter.drawText(
                QRectF(0, 0, self.width(), self.height()),
                Qt.AlignCenter,
                tr("chart.empty"),
            )
            return

        if chart.layout == LAYOUT_STACK:
            self._render_stack(painter, chart, frame)
            return

        self._render_axes(painter, chart, frame)
        painter.save()
        painter.setClipRect(
            QRectF(frame.left, frame.top, frame.width, frame.height).adjusted(-1, -1, 1, 1)
        )
        for item in self.series():
            if item.visible and item.points:
                self._render_series(painter, item, frame)
        painter.restore()
        if live:
            self._render_crosshair(painter, chart, frame)
        self._render_legend(painter, chart, frame)
        self._render_outside(painter, frame)
        self._render_note(painter, chart)

    def _render_axes(self, painter: QPainter, chart: Chart, frame: Frame) -> None:
        metrics = self._metrics()
        line = metrics.height()
        bottom = frame.top + frame.height

        x_ticks = (
            category_ticks(chart.categories, frame.x.lo, frame.x.hi)
            if chart.categories
            else ticks(chart.x, frame.x.lo, frame.x.hi, self._unit)
        )
        y_ticks = ticks(chart.y, frame.y.lo, frame.y.hi, self._unit)

        grid = QPen(self._ink(40))
        grid.setWidthF(1.0)
        painter.setPen(grid)
        for tick in y_ticks:
            y = frame.py(tick.value)
            painter.drawLine(QPointF(frame.left, y), QPointF(frame.left + frame.width, y))
        for tick in x_ticks:
            x = frame.px(tick.value)
            painter.drawLine(QPointF(x, frame.top), QPointF(x, bottom))

        if chart.zero_line and frame.y.lo <= 0 <= frame.y.hi:
            # Zero on the residuals is the model itself, so it stands out more
            # than the grid lines.
            zero = QPen(self._ink(170))
            zero.setWidthF(1.4)
            painter.setPen(zero)
            y = frame.py(0.0)
            painter.drawLine(QPointF(frame.left, y), QPointF(frame.left + frame.width, y))

        painter.setPen(self._ink(120))
        painter.drawLine(QPointF(frame.left, bottom), QPointF(frame.left + frame.width, bottom))
        painter.drawLine(QPointF(frame.left, frame.top), QPointF(frame.left, bottom))

        painter.setPen(self._ink())
        for tick in y_ticks:
            y = frame.py(tick.value)
            painter.drawText(
                QRectF(0, y - line / 2, frame.left - 6, line),
                Qt.AlignRight | Qt.AlignVCenter,
                tick.text,
            )
        for tick in x_ticks:
            x = frame.px(tick.value)
            painter.drawText(
                QRectF(x - 60, bottom + 4, 120, line),
                Qt.AlignHCenter | Qt.AlignTop,
                tick.text,
            )

        painter.drawText(
            QRectF(frame.left, bottom + 4 + line, frame.width, line),
            Qt.AlignHCenter | Qt.AlignTop,
            axis_caption(chart.x, frame.x.lo, frame.x.hi, self._unit),
        )
        caption = axis_caption(chart.y, frame.y.lo, frame.y.hi, self._unit)
        lines = self._y_caption_lines(chart, frame.y)
        room = self._y_caption_room()
        if lines > 1:
            # Doesn't fit on one line — wrap at words. Cutting it would be more
            # honest than silence, but «Измерено минус моде» explains nothing.
            caption = metrics.elidedText(caption, Qt.ElideRight, room * lines)
        painter.save()
        # The centre of the widget, not of the plot area: the label stands
        # beside the area and is not limited by its height, while the margins
        # above and below are taken by the title and the legend.
        painter.translate(PADDING, self.height() / 2)
        painter.rotate(-90)
        spacing = metrics.lineSpacing() * lines
        # The rectangle grows from the rotation point to the **right**, not
        # to the left: after rotate(-90) the y coordinate becomes screen x,
        # and with `-spacing` the label was drawn left of the padding — on one
        # line its edge was cut off, and a second line did not land in the
        # widget at all.
        painter.drawText(
            QRectF(-room / 2, 0, room, spacing),
            Qt.TextWordWrap | Qt.AlignCenter,
            caption,
        )
        painter.restore()

    def _render_series(self, painter: QPainter, item: Series, frame: Frame) -> None:
        colour = self._tone(item.tone)
        spots = [QPointF(frame.px(p.x), frame.py(p.y)) for p in item.points]

        if item.kind in (KIND_LINE, KIND_STEPS, KIND_LINE_DOTS):
            pen = QPen(colour)
            pen.setWidthF(1.8)
            painter.setPen(pen)
            path = QPainterPath(spots[0])
            for previous, spot in zip(spots, spots[1:]):
                if item.kind == KIND_STEPS:
                    path.lineTo(QPointF(spot.x(), previous.y()))
                path.lineTo(spot)
            painter.drawPath(path)
            if item.kind != KIND_LINE_DOTS:
                return
            painter.setBrush(colour)
            for spot in spots:
                painter.drawEllipse(spot, DOT_RADIUS, DOT_RADIUS)
            painter.setBrush(Qt.NoBrush)
            return

        if item.kind == KIND_BARS:
            width = max(frame.width / max(len(spots) * 2, 1), 3.0)
            painter.setPen(QPen(colour.darker(130)))
            painter.setBrush(colour)
            base = frame.py(0.0)
            for spot in spots:
                top, height = min(spot.y(), base), abs(spot.y() - base)
                painter.drawRect(QRectF(spot.x() - width / 2, top, width, max(height, 1.0)))
            painter.setBrush(Qt.NoBrush)
            return

        if item.kind == KIND_STEMS:
            # A stem down to zero, not a bar: zero here is not the edge of the
            # scale but the model itself, and the miss is counted from it.
            # Thinner than a bar — the value stays a point, and the stem only
            # shows which way it deviated.
            stem = QPen(colour)
            stem.setWidthF(1.4)
            painter.setPen(stem)
            base = frame.py(0.0)
            for spot in spots:
                painter.drawLine(QPointF(spot.x(), base), spot)

        painter.setPen(QPen(colour.darker(140)))
        painter.setBrush(colour)
        for spot in spots:
            painter.drawEllipse(spot, DOT_RADIUS, DOT_RADIUS)
        painter.setBrush(Qt.NoBrush)

    def sizeHint(self):  # noqa: N802 — Qt's name
        """The breakdown bar needs no height: it has no quantity vertically.

        Without this the splitter gives it half the window, and under the
        bar hangs an empty area two thirds of the screen tall.
        """
        chart = self._chart
        if chart is None:
            return super().sizeHint()
        line = self._metrics().height()
        if chart.layout == LAYOUT_STACK:
            parts = len(chart.series[0].points) if chart.series else 0
            height = line * (3.2 + parts) + self._note_height(chart) + PADDING * 2
            return QSize(self.minimumWidth(), int(height))
        return super().sizeHint()

    def minimumSizeHint(self):  # noqa: N802 — Qt's name
        chart = self._chart
        if chart is not None and chart.layout == LAYOUT_STACK:
            return self.sizeHint()
        return super().minimumSizeHint()

    def _render_stack(self, painter: QPainter, chart: Chart, frame: Frame) -> None:
        """One full-width bar: the components as shares of the whole.

        The legend here is not decoration but the only way to read the thin
        components: the VeraCrypt header on a terabyte container takes less
        than a pixel, and there is no way at all to put the cursor on it.
        """
        series = chart.series[0]
        total = sum(max(point.y, 0.0) for point in series.points)
        if total <= 0:
            return

        metrics = self._metrics()
        line = metrics.height()
        height = min(frame.height, line * 2.2)
        offset = frame.left
        for index, point in enumerate(series.points):
            span = frame.width * max(point.y, 0.0) / total
            colour = self._tone(index)
            painter.setPen(QPen(colour.darker(140)))
            painter.setBrush(colour)
            painter.drawRect(QRectF(offset, frame.top, max(span, 0.5), height))
            offset += span
        painter.setBrush(Qt.NoBrush)

        self._legend_boxes = []
        y = frame.top + height + 8
        for index, point in enumerate(series.points):
            box = QRectF(frame.left, y + 2, LEGEND_BOX, LEGEND_BOX)
            painter.setPen(Qt.NoPen)
            painter.setBrush(self._tone(index))
            painter.drawRect(box)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(self._ink())
            painter.drawText(
                QRectF(frame.left + LEGEND_BOX + 6, y, frame.width, line),
                Qt.AlignLeft | Qt.AlignVCenter,
                point.tip,
            )
            y += line + 4
        self._render_note(painter, chart)

    def _render_legend(self, painter: QPainter, chart: Chart, frame: Frame) -> None:
        metrics = self._metrics()
        line = metrics.height()
        y = frame.top + frame.height + line * 2 + 10
        widths = [
            LEGEND_BOX + 6 + metrics.horizontalAdvance(item.name) for item in chart.series
        ]
        x = frame.left + max((frame.width - sum(widths) - LEGEND_GAP * (len(widths) - 1)) / 2, 0)

        self._legend_boxes = []
        for item, width in zip(chart.series, widths):
            hidden = item.key in self._hidden
            box = QRectF(x, y + 2, LEGEND_BOX, LEGEND_BOX)
            colour = self._tone(item.tone)
            painter.setPen(QPen(self._ink(90)))
            painter.setBrush(Qt.NoBrush if hidden else colour)
            painter.drawRect(box)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(self._ink(90 if hidden else 255))
            painter.drawText(
                QRectF(x + LEGEND_BOX + 6, y, width, line),
                Qt.AlignLeft | Qt.AlignVCenter,
                item.name,
            )
            self._legend_boxes.append((QRectF(x, y, width, line + 4), item.key))
            x += width + LEGEND_GAP

    def _render_crosshair(self, painter: QPainter, chart: Chart, frame: Frame) -> None:
        """Lines from the cursor to the axes, and the values on them.

        The chart's own crosshair is drawn in full; one that came from a
        neighbour, as a single vertical line: a horizontal one would mean the
        neighbouring chart has the same Y value, while it has both a different
        quantity and a different range.
        """
        if chart.layout == LAYOUT_STACK:
            return

        position = self._cursor
        own = position is not None and frame.contains(position.x(), position.y())
        if not own and self._linked_x is None:
            return

        pen = QPen(self._ink(120))
        pen.setWidthF(1.0)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        bottom = frame.top + frame.height

        x_px = position.x() if own else frame.px(self._linked_x)
        if frame.left <= x_px <= frame.left + frame.width:
            painter.drawLine(QPointF(x_px, frame.top), QPointF(x_px, bottom))
            self._axis_plaque(
                painter,
                value_label(
                    chart.x,
                    frame.at_px(x_px),
                    frame.x.lo,
                    frame.x.hi,
                    self._unit,
                    chart.categories,
                ),
                x_px,
                bottom + 3,
                centred=True,
            )
        if not own:
            return

        y_px = position.y()
        painter.setPen(pen)
        painter.drawLine(
            QPointF(frame.left, y_px), QPointF(frame.left + frame.width, y_px)
        )
        self._axis_plaque(
            painter,
            value_label(
                chart.y, frame.at_py(y_px), frame.y.lo, frame.y.hi, self._unit
            ),
            frame.left - 3,
            y_px,
            centred=False,
        )

        found = nearest(frame, self.series(), position.x(), position.y())
        if found is not None:
            # A ring, not a fill: the point under it keeps its colour, and you
            # can see which series it belongs to.
            ring = QPen(self._tone(found.series.tone).darker(120))
            ring.setWidthF(1.6)
            painter.setPen(ring)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(
                QPointF(frame.px(found.point.x), frame.py(found.point.y)),
                DOT_RADIUS + 3,
                DOT_RADIUS + 3,
            )

    def _axis_plaque(
        self,
        painter: QPainter,
        text: str,
        x: float,
        y: float,
        centred: bool,
    ) -> None:
        """The axis value under the crosshair line, on a backing.

        The backing is needed: without it the label lies over the axis ticks
        and reads as one more tick.
        """
        if not text:
            return
        metrics = self._metrics()
        width = metrics.horizontalAdvance(text) + 8
        height = metrics.height() + 2
        left = x - width / 2 if centred else x - width
        top = y if centred else y - height / 2
        left = min(max(left, 0.0), max(self.width() - width, 0.0))
        top = min(max(top, 0.0), max(self.height() - height, 0.0))
        box = QRectF(left, top, width, height)

        painter.setPen(QPen(self._ink(90)))
        painter.setBrush(self.palette().window())
        painter.drawRect(box)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(self._ink())
        painter.drawText(box, Qt.AlignCenter, text)

    def _render_outside(self, painter: QPainter, frame: Frame) -> None:
        """Say that part of the fresh data fell outside the zoom being held.

        Silence is not an option: the zoom is held on purpose, and without a
        label "nothing changed" after a new measurement looks like a breakage,
        not like the measurement landing out of frame.
        """
        if not self._outside:
            return
        metrics = self._metrics()
        text = tr("chart.outside", n=self._outside)
        painter.setPen(self._ink(150))
        painter.drawText(
            QRectF(
                frame.left,
                frame.top + 2,
                frame.width - 4,
                metrics.height(),
            ),
            Qt.AlignRight | Qt.AlignTop,
            text,
        )

    def _note_height(self, chart: Chart | None) -> float:
        """How tall the note is, in pixels, wrapped to this widget's width.

        Measured, not assumed to be one line: the notes here are long, and
        the one explaining the hidden series was cut off mid-word in a narrow
        window — «и промах там на порядки бо».
        """
        if chart is None or not chart.note:
            return 0.0
        width = max(self.width() - PADDING * 2, 1)
        box = self._metrics().boundingRect(
            QRectF(0, 0, width, 0), Qt.TextWordWrap | Qt.AlignHCenter, chart.note
        )
        return box.height()

    def _render_note(self, painter: QPainter, chart: Chart) -> None:
        if not chart.note:
            return
        height = self._note_height(chart)
        painter.setPen(self._ink(150))
        painter.drawText(
            QRectF(
                PADDING,
                self.height() - height - PADDING / 2,
                self.width() - PADDING * 2,
                height,
            ),
            Qt.TextWordWrap | Qt.AlignHCenter | Qt.AlignBottom,
            chart.note,
        )

    # --- mouse -------------------------------------------------------------

    def _hit(self, position: QPointF):
        frame = self.frame()
        if frame is None or self._chart is None:
            return None
        if self._chart.layout == LAYOUT_STACK:
            series = self._chart.series[0] if self._chart.series else None
            if series is None:
                return None
            return stack_hit(frame, series, position.x(), position.y())
        found = nearest(frame, self.series(), position.x(), position.y())
        return found.point if found else None

    def tip_at(self, x: float, y: float) -> str:
        """Tooltip text for this screen point. Empty means nothing to show."""
        point = self._hit(QPointF(x, y))
        return point.tip if point is not None else ""

    def legend_at(self, x: float, y: float) -> str:
        """Key of the series whose legend entry is pointed at. Empty: none."""
        for box, key in self._legend_boxes:
            if box.contains(QPointF(x, y)):
                return key
        return ""

    def toggle_series(self, key: str) -> None:
        """Hide a series or bring it back. The legend is the only toggle."""
        if key in self._hidden:
            self._hidden.discard(key)
        else:
            self._hidden.add(key)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Left: rubber band and legend; right: pan; middle: menu."""
        position = event.position()
        if event.button() == Qt.RightButton:
            self._pan = position
            return
        if event.button() == Qt.MiddleButton:
            self._show_menu(event.globalPosition().toPoint())
            return
        if event.button() != Qt.LeftButton:
            return
        key = self.legend_at(position.x(), position.y())
        if key:
            self.toggle_series(key)
            return
        frame = self.frame()
        if frame is not None and frame.contains(position.x(), position.y()):
            self._press = position

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        position = event.position()
        self._track(position)

        if self._pan is not None:
            self._drag_view(position - self._pan)
            self._pan = position
            return

        if self._press is not None:
            self._show_band(self._press, position)
            return

        tip = self.tip_at(position.x(), position.y())
        if tip:
            QToolTip.showText(event.globalPosition().toPoint(), tip, self)
        else:
            QToolTip.hideText()

    def _track(self, position: QPointF | None) -> None:
        """Remember the cursor and tell the neighbours with a shared axis."""
        self._cursor = position
        frame = self.frame()
        inside = (
            position is not None
            and frame is not None
            and frame.contains(position.x(), position.y())
        )
        if inside:
            self.cursorMoved.emit(frame.at_px(position.x()))
        else:
            self.cursorLeft.emit()
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        """The cursor left — no crosshair must remain here or on neighbours."""
        self._track(None)
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.RightButton:
            self._pan = None
            return
        if event.button() != Qt.LeftButton or self._press is None:
            return
        start, self._press = self._press, None
        if self._band is not None:
            self._band.hide()

        position = event.position()
        far = abs(position.x() - start.x()) + abs(position.y() - start.y())
        if far < DRAG_THRESHOLD:
            # Not a rubber band but a click: hand the point out, if one was
            # hit.
            point = self._hit(position)
            if point is not None:
                self.pointPicked.emit(point.key, point.tip)
            return
        self._zoom_to(start, position)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """Right double click: full view; left double click: menu.

        Reset moved from the left button to the right one, following panning:
        the same hand holds the right button to pan the chart and uses it to
        bring the full view back.
        """
        if event.button() == Qt.RightButton:
            self.reset_zoom()
            return
        if event.button() == Qt.LeftButton:
            self._show_menu(event.globalPosition().toPoint())

    def wheelEvent(self, event) -> None:  # noqa: N802
        frame = self.frame()
        position = event.position()
        if frame is None or not frame.contains(position.x(), position.y()):
            return
        step = event.angleDelta().y()
        if not step:
            return
        factor = WHEEL_STEP if step > 0 else 1 / WHEEL_STEP
        x_share = (position.x() - frame.left) / frame.width
        y_share = 1.0 - (position.y() - frame.top) / frame.height
        self._x = frame.x.scaled(factor, x_share)
        self._y = frame.y.scaled(factor, y_share)
        self.update()
        self.rangeChanged.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape and self.zoomed:
            self.reset_zoom()
            return
        super().keyPressEvent(event)

    def _show_band(self, start: QPointF, current: QPointF) -> None:
        if self._band is None:
            self._band = QRubberBand(QRubberBand.Rectangle, self)
        self._band.setGeometry(
            QRectF(start, current).normalized().toRect()
        )
        self._band.show()

    def _zoom_to(self, start: QPointF, end: QPointF) -> None:
        frame = self.frame()
        if frame is None:
            return
        x_from = (start.x() - frame.left) / frame.width
        x_to = (end.x() - frame.left) / frame.width
        y_from = 1.0 - (start.y() - frame.top) / frame.height
        y_to = 1.0 - (end.y() - frame.top) / frame.height
        self._x = frame.x.zoomed(x_from, x_to)
        self._y = frame.y.zoomed(y_from, y_to)
        self.update()
        self.rangeChanged.emit()

    def _drag_view(self, delta: QPointF) -> None:
        frame = self.frame()
        if frame is None:
            return
        self._x = frame.x.shifted(-delta.x() / frame.width)
        self._y = frame.y.shifted(delta.y() / frame.height)
        self.update()
        self.rangeChanged.emit()

    # --- export ------------------------------------------------------------

    def image(self, scale: float = 2.0) -> QPixmap:
        """Image of the chart. Double scale, so it doesn't blur when pasted."""
        pixmap = QPixmap(int(self.width() * scale), int(self.height() * scale))
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(self.palette().window().color())
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        self._render(painter)
        painter.end()
        return pixmap

    def copy_image(self) -> None:
        QGuiApplication.clipboard().setPixmap(self.image())

    def save_image(self, path: str = "") -> str:
        """Save the image. An empty path means ask the person.

        SVG and PDF are drawn by the same `_render` as the screen: QPainter
        has one interface for raster and vector, and there is nowhere for
        them to diverge.
        """
        if not path:
            path, _filter = QFileDialog.getSaveFileName(
                self,
                tr("chart.save.title"),
                f"{(self._chart.title if self._chart else tr('chart.save.name'))}.png",
                tr("chart.save.filter"),
            )
        if not path:
            return ""
        lowered = path.lower()
        if lowered.endswith(".svg"):
            self._save_vector_svg(path)
        elif lowered.endswith(".pdf"):
            self._save_vector_pdf(path)
        else:
            self.image().save(path, "PNG")
        return path

    def _save_vector_svg(self, path: str) -> None:
        from PySide6.QtSvg import QSvgGenerator

        generator = QSvgGenerator()
        generator.setFileName(path)
        generator.setSize(self.size())
        generator.setViewBox(self.rect())
        generator.setTitle(self._chart.title if self._chart else "")
        painter = QPainter(generator)
        painter.fillRect(self.rect(), self.palette().window())
        self._render(painter)
        painter.end()

    def _save_vector_pdf(self, path: str) -> None:
        from PySide6.QtGui import QPageSize, QPdfWriter

        writer = QPdfWriter(path)
        writer.setPageSize(QPageSize(QPageSize.A5))
        writer.setResolution(300)
        painter = QPainter(writer)
        # Stretch to the page with the same code: QPainter does the scaling,
        # not a separate drawing branch.
        scale = min(
            writer.width() / max(self.width(), 1), writer.height() / max(self.height(), 1)
        )
        painter.scale(scale, scale)
        self._render(painter)
        painter.end()

    def build_menu(self) -> QMenu:
        """The chart menu. Built anew each time: the items depend on state."""
        menu = QMenu(self)
        reset = menu.addAction(tr("chart.reset"))
        reset.setEnabled(self.zoomed)
        reset.triggered.connect(self.reset_zoom)
        for where, button in (
            ("up", self.up_button),
            ("down", self.down_button),
            ("left", self.left_button),
            ("right", self.right_button),
        ):
            if not button.isVisibleTo(self):
                continue
            action = menu.addAction(button.toolTip().rstrip("."))
            action.setEnabled(button.isEnabled())
            action.triggered.connect(lambda _=False, name=where: self._move(name))
        if self.detachable or self.detached:
            detach = menu.addAction(
                tr("chart.menu.return" if self.detached else "chart.menu.detach")
            )
            detach.setToolTip(self.detach_button.toolTip())
            detach.triggered.connect(self.detachRequested.emit)
        menu.addSeparator()
        menu.addAction(tr("chart.copy")).triggered.connect(self.copy_image)
        menu.addAction(tr("chart.save")).triggered.connect(lambda: self.save_image())
        return menu

    def _show_menu(self, where) -> None:
        self.build_menu().exec(where)
