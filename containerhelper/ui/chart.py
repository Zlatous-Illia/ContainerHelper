"""Виджет графика на QPainter. Вся арифметика — в `plot.py`, здесь только холст.

Своё рисование, а не библиотека, выбрано ради трёх вещей, которых у готовых
нет: подписи следуют выбранной в окне единице (B/KiB/MiB/GiB), цвета берутся из
палитры окна, то есть тема получается та же, что у всей программы, а сама
арифметика графика лежит слоем ниже и проверяется без Qt — как и всё остальное,
что считает числа.

Один и тот же `_render` рисует и на экран, и в файл: иначе сохранённая картинка
однажды разошлась бы с показанной, и заметить это было бы нечем.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QFileDialog, QMenu, QRubberBand, QToolTip, QWidget

from ..formatting import DEFAULT_UNIT, Unit
from ..plot import (
    AXIS_BYTES,
    KIND_BARS,
    KIND_DOTS,
    KIND_LINE,
    KIND_LINE_DOTS,
    KIND_STACK,
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
)

#: Тона серий. Палитра окна тут не годится: различимых цветов в ней нет — там
#: фон, текст и выделение, а серий бывает до шести. Поэтому берутся оттенки, а
#: насыщенность и яркость подбираются под тему, чтобы линии не светились на
#: тёмном фоне и не выцветали на светлом.
TONE_HUES = (210, 25, 145, 275, 45, 320)

#: Поля вокруг поля графика сверх места под подписи.
PADDING = 10
DOT_RADIUS = 3.5
LEGEND_BOX = 11
LEGEND_GAP = 16

#: Насколько зумит одна ступенька колеса.
WHEEL_STEP = 0.82

#: Меньше этого рамка считается промахом, а не выделением: иначе любой щелчок
#: по точке заодно зумил бы в неё до предела.
DRAG_THRESHOLD = 8


class ChartView(QWidget):
    """Один график: оси, серии, легенда, подсказка и зум."""

    #: Точка, по которой щёлкнули: её ключ и готовое описание. Ключ — чтобы
    #: окно знало, о какой записи речь; описание — чтобы не собирать его
    #: второй раз там, где данных уже нет.
    pointPicked = Signal(object, str)

    #: Масштаб изменился: зумом, панорамой или сбросом. По нему графики с
    #: общей осью X держатся вместе.
    rangeChanged = Signal()

    def __init__(self, chart: Chart | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._chart = chart
        self._unit: Unit = DEFAULT_UNIT
        #: Ручной масштаб. None — по данным; так же выглядит и сброс.
        self._x: Span | None = None
        self._y: Span | None = None
        self._hidden: set[str] = set()
        self._legend_boxes: list[tuple[QRectF, str]] = []
        self._band: QRubberBand | None = None
        self._press: QPointF | None = None
        self._pan: QPointF | None = None

        self.setMinimumSize(320, 220)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)

    # --- содержимое --------------------------------------------------------

    def set_chart(self, chart: Chart | None) -> None:
        """Заменить данные. Масштаб сбрасывается: он был про прежние числа.

        Спрятанные серии берутся у самого графика: он один знает, какая из них
        по умолчанию мешает. Пересчитывать их тут заново — значит помнить про
        каждый график в двух местах.
        """
        self._chart = chart
        self._x = self._y = None
        self._hidden = {
            item.name for item in (chart.series if chart else ()) if not item.visible
        }
        self.updateGeometry()
        self.update()

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
        """Ручной масштаб по X. None — по данным."""
        return self._x

    def apply_x(self, span: Span | None) -> None:
        """Взять масштаб по X у соседнего графика, не отвечая своим сигналом.

        Без молчания графики с общей осью зациклились бы: первый сообщил бы
        второму, второй первому, и так до переполнения стека.
        """
        if self._x == span:
            return
        self._x = span
        self.update()

    def series(self) -> tuple[Series, ...]:
        """Серии с учётом выключенных щелчком по легенде."""
        if self._chart is None:
            return ()
        return tuple(
            item.with_visible(item.name not in self._hidden)
            for item in self._chart.series
        )

    # --- геометрия ---------------------------------------------------------

    def _metrics(self) -> QFontMetricsF:
        return QFontMetricsF(self.font())

    def frame(self) -> Frame | None:
        """Поле графика в пикселях вместе с диапазонами по осям.

        Левое поле меряется по самой широкой подписи, а не берётся на глаз:
        подобранное на глаз обрежет «1 099 511 627 776» ровно тогда, когда оно
        появится.
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

        left = PADDING + widest + 6 + line
        right = PADDING
        top = PADDING + (line if chart.title else 0)
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

    def _spans(self, chart: Chart, series: tuple[Series, ...]) -> tuple[Span, Span]:
        """Диапазоны по осям: ручные, если зумили, иначе по данным."""
        if self._x is not None and self._y is not None:
            return self._x, self._y

        x_lo, x_hi = bounds(series, "x")
        y_lo, y_hi = bounds(series, "y")
        if chart.categories:
            x_lo, x_hi = -0.5, max(len(chart.categories) - 0.5, 0.5)
        else:
            x_lo, x_hi = padded(x_lo, x_hi, chart.x.log)
        # Столбики растут от нуля, и обрезать его — соврать о их длине.
        with_zero = chart.zero_line or any(s.kind == KIND_BARS for s in series)
        y_lo, y_hi = padded(y_lo, y_hi, chart.y.log, include_zero=with_zero)
        return (
            self._x or Span(x_lo, x_hi, chart.x.log),
            self._y or Span(y_lo, y_hi, chart.y.log),
        )

    # --- цвета -------------------------------------------------------------

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

    # --- рисование ---------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 — имя от Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), self.palette().window())
        self._render(painter)
        painter.end()

    def _render(self, painter: QPainter) -> None:
        chart = self._chart
        frame = self.frame()
        if chart is None or frame is None:
            return

        metrics = self._metrics()
        painter.setPen(self._ink())
        if chart.title:
            painter.drawText(
                QRectF(0, PADDING - metrics.height(), self.width(), metrics.height()),
                Qt.AlignHCenter | Qt.AlignVCenter,
                chart.title,
            )

        if chart.empty:
            painter.setPen(self._ink(150))
            painter.drawText(
                QRectF(0, 0, self.width(), self.height()),
                Qt.AlignCenter,
                "Замеров пока нет — рисовать нечего.",
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
        self._render_legend(painter, chart, frame)
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
            # Ноль на остатках — это и есть модель, поэтому он заметнее сетки.
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
        painter.save()
        painter.translate(PADDING, frame.top + frame.height / 2)
        painter.rotate(-90)
        painter.drawText(
            QRectF(-frame.height / 2, -line, frame.height, line),
            Qt.AlignCenter,
            axis_caption(chart.y, frame.y.lo, frame.y.hi, self._unit),
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

        painter.setPen(QPen(colour.darker(140)))
        painter.setBrush(colour)
        for spot in spots:
            painter.drawEllipse(spot, DOT_RADIUS, DOT_RADIUS)
        painter.setBrush(Qt.NoBrush)

    def sizeHint(self):  # noqa: N802 — имя от Qt
        """Полосе высота не нужна: у неё нет оси, есть строка и легенда.

        Без этого разделитель отдаёт ей половину окна, и под полосой висит
        пустое поле в две трети экрана.
        """
        chart = self._chart
        if chart is None or chart.layout != LAYOUT_STACK:
            return super().sizeHint()
        line = self._metrics().height()
        parts = len(chart.series[0].points) if chart.series else 0
        height = line * (3.2 + parts) + self._note_height(chart) + PADDING * 2
        return QSize(self.minimumWidth(), int(height))

    def minimumSizeHint(self):  # noqa: N802 — имя от Qt
        chart = self._chart
        if chart is not None and chart.layout == LAYOUT_STACK:
            return self.sizeHint()
        return super().minimumSizeHint()

    def _render_stack(self, painter: QPainter, chart: Chart, frame: Frame) -> None:
        """Одна полоса во всю ширину: слагаемые в долях от целого.

        Легенда тут не украшение, а единственный способ прочитать тонкие
        слагаемые: заголовок VeraCrypt на терабайтном контейнере занимает
        меньше пикселя, и навести на него курсор нельзя никак.
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
            hidden = item.name in self._hidden
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
            self._legend_boxes.append((QRectF(x, y, width, line + 4), item.name))
            x += width + LEGEND_GAP

    def _note_height(self, chart: Chart | None) -> float:
        """Сколько строк займёт подпись при этой ширине окна.

        Меряется, а не считается за одну строку: подписи тут длинные, и та,
        что объясняет спрятанную серию, на узком окне обрезалась ровно на
        полуслове — «и промах там на порядки бо».
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

    # --- мышь --------------------------------------------------------------

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
        """Текст подсказки для этой точки экрана. Пусто — показывать нечего."""
        point = self._hit(QPointF(x, y))
        return point.tip if point is not None else ""

    def legend_at(self, x: float, y: float) -> str:
        """Имя серии, на чью запись в легенде показывают. Пусто — мимо."""
        for box, name in self._legend_boxes:
            if box.contains(QPointF(x, y)):
                return name
        return ""

    def toggle_series(self, name: str) -> None:
        """Спрятать серию или вернуть её. Легенда — единственный переключатель."""
        if name in self._hidden:
            self._hidden.discard(name)
        else:
            self._hidden.add(name)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        position = event.position()
        if event.button() == Qt.MiddleButton:
            self._pan = position
            return
        if event.button() != Qt.LeftButton:
            return
        name = self.legend_at(position.x(), position.y())
        if name:
            self.toggle_series(name)
            return
        frame = self.frame()
        if frame is not None and frame.contains(position.x(), position.y()):
            self._press = position

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        position = event.position()

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

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MiddleButton:
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
            # Не рамка, а щелчок: отдать наружу точку, если в неё попали.
            point = self._hit(position)
            if point is not None:
                self.pointPicked.emit(point.key, point.tip)
            return
        self._zoom_to(start, position)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.reset_zoom()

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

    # --- вывод наружу ------------------------------------------------------

    def image(self, scale: float = 2.0) -> QPixmap:
        """Картинка графика. Удвоенный масштаб — чтобы не мылилась при вставке."""
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
        """Сохранить картинку. Пустой путь — спросить у человека.

        SVG и PDF рисуются тем же `_render`, что и экран: у QPainter один
        интерфейс на растр и на вектор, и расходиться им негде.
        """
        if not path:
            path, _filter = QFileDialog.getSaveFileName(
                self,
                "Сохранить график",
                f"{(self._chart.title if self._chart else 'график')}.png",
                "Картинка PNG (*.png);;Вектор SVG (*.svg);;Документ PDF (*.pdf)",
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
        # Растянуть на страницу тем же кодом: масштабирование делает QPainter,
        # а не отдельная ветка рисования.
        scale = min(
            writer.width() / max(self.width(), 1), writer.height() / max(self.height(), 1)
        )
        painter.scale(scale, scale)
        self._render(painter)
        painter.end()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        reset = menu.addAction("Сбросить масштаб")
        reset.setEnabled(self.zoomed)
        reset.triggered.connect(self.reset_zoom)
        menu.addSeparator()
        menu.addAction("Копировать картинку").triggered.connect(self.copy_image)
        menu.addAction("Сохранить картинку…").triggered.connect(lambda: self.save_image())
        menu.exec(event.globalPos())
