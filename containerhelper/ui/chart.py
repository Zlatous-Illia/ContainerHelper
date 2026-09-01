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
from PySide6.QtWidgets import (
    QFileDialog,
    QMenu,
    QRubberBand,
    QToolButton,
    QToolTip,
    QWidget,
)

from ..formatting import DEFAULT_UNIT, Unit
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

#: Подписи кнопки отсоединения. Словами, а не значком: значок пришлось бы
#: объяснять подсказкой, а подсказку — навести. Коротко, потому что кнопки
#: стоят в одной строке с заголовком и отнимают у него ширину: «Отсоединить»
#: съедало у графика в сетке треть заголовка. Полная фраза — в подсказке и в
#: меню, где места сколько угодно.
DETACH_TEXT = "В окно"
RETURN_TEXT = "Вернуть"

#: Кнопки перестановки — стрелками: слов «раньше» и «позже» в углу не
#: разместить, а стрелка на месте, куда график и правда уедет, объясняет себя
#: сама. В столбце их две, в сетке четыре: там «выше» и «левее» — разные
#: движения, и одной парой стрелок их не выразить.
MOVE_LEFT = "←"
MOVE_RIGHT = "→"
MOVE_UP = "↑"
MOVE_DOWN = "↓"

#: Меньше этого поле графика перестаёт быть полем: подписи делений сходятся
#: друг к другу, а кривая становится штрихом. Ленте и полосе этот минимум не
#: годится — им по вертикали откладывать нечего, и он не даёт им ужаться до
#: собственной высоты.
MIN_WIDTH = 320
MIN_HEIGHT = 220

#: Сколько строк отводится под подпись оси Y. Две, а не одна: «Измерено минус
#: модель, B» — это 288 пикселей, а поле графика в окне на три графика высотой
#: 186, и подпись обрезалась ровно посередине слова. Три строки съели бы уже
#: заметную часть ширины.
Y_CAPTION_LINES = 2


class ChartView(QWidget):
    """Один график: оси, серии, легенда, подсказка и зум."""

    #: Точка, по которой щёлкнули: её ключ и готовое описание. Ключ — чтобы
    #: окно знало, о какой записи речь; описание — чтобы не собирать его
    #: второй раз там, где данных уже нет.
    pointPicked = Signal(object, str)

    #: Масштаб изменился: зумом, панорамой или сбросом. По нему графики с
    #: общей осью X держатся вместе.
    rangeChanged = Signal()

    #: Курсор встал на значение по X — или ушёл с графика (второй сигнал).
    #: По ним перекрестье повторяется на соседних графиках с общей осью:
    #: горб остатков и ступень наклона стоят на одном и том же размере тома, и
    #: увидеть это можно только одной линией через оба графика.
    cursorMoved = Signal(float)
    cursorLeft = Signal()

    #: Просьба отсоединить график в своё окно или вернуть обратно. Решает
    #: окно: виджет не знает, где он живёт и куда его возвращать.
    detachRequested = Signal()

    #: Просьба переставить график: −1 — раньше, +1 — позже. Порядок держит
    #: окно, график знает только, что его просят подвинуть.
    moveRequested = Signal(int)

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
        #: Где стоит курсор. None — он ушёл с виджета, перекрестья нет.
        self._cursor: QPointF | None = None
        #: Значение по X, пришедшее от соседнего графика с общей осью. Рисуется
        #: одной вертикальной линией: своего курсора здесь нет.
        self._linked_x: float | None = None
        #: Сколько точек осталось за кадром после обновления данных. Ноль —
        #: сказать нечего.
        self._outside = 0
        #: Живёт ли график в своём окне и можно ли его туда отправить. Ставит
        #: окно; здесь — только текст кнопки и пункта меню.
        self.detached = False
        self.detachable = False

        # Кнопки, а не только пункты меню: меню надо сначала найти — двойным
        # щелчком или средней кнопкой, — а переставить и отсоединить график
        # это первое, что с ним делают в окне на четыре штуки.
        #: Куда двигать и на сколько мест. Шаг ставит окно: оно одно знает,
        #: сколько графиков в ряду, — а «вверх» в сетке это два места назад,
        #: а не одно.
        self._steps: dict[str, int] = {}
        self.left_button = self._move_button(MOVE_LEFT, "left")
        self.right_button = self._move_button(MOVE_RIGHT, "right")
        self.up_button = self._move_button(MOVE_UP, "up")
        self.down_button = self._move_button(MOVE_DOWN, "down")
        self.detach_button = self._corner_button(
            DETACH_TEXT, "", self.detachRequested.emit
        )
        #: Порядок в углу слева направо. Отсоединение крайнее справа: оно
        #: уводит график из окна, а перестановка оставляет его в нём.
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
        # Правая кнопка занята сдвигом, и меню по ней открываться не должно:
        # иначе оно выскакивает поверх графика на каждой панораме.
        self.setContextMenuPolicy(Qt.PreventContextMenu)
        # График может прийти и в конструктор, а не только через set_chart:
        # минимум высоты зависит от разметки, и ставить его надо в обоих путях.
        self._apply_minimum()

    # --- кнопки в углу -----------------------------------------------------

    def _corner_button(self, text: str, tip: str, slot) -> QToolButton:
        button = QToolButton(self)
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.NoFocus)
        # Поля по умолчанию рассчитаны на панель инструментов: одна стрелка
        # занимала 44 пикселя, а три кнопки — треть ширины графика в сетке.
        button.setStyleSheet("QToolButton { padding: 0px 4px; }")
        button.setText(text)
        if tip:
            button.setToolTip(tip)
        button.clicked.connect(slot)
        button.hide()
        return button

    def _move_button(self, text: str, where: str) -> QToolButton:
        # `clicked` приходит с булевым «нажата ли», и без пустого первого
        # параметра он подставляется в `where`: кнопка нажимается, а не
        # делает ничего — молча, потому что направления «False» просто нет.
        button = self._corner_button(
            text, "", lambda _checked=False, name=where: self._move(name)
        )
        # Ширина по самой стрелке, а не по тому, что QToolButton считает
        # приличным для панели инструментов: четыре стрелки по её мерке
        # съедали у заголовка в сетке двести пикселей.
        metrics = QFontMetricsF(button.font())
        button.setFixedWidth(int(metrics.horizontalAdvance(text)) + 10)
        return button

    def _move(self, where: str) -> None:
        step = self._steps.get(where, 0)
        if step:
            self.moveRequested.emit(step)

    def set_place(self, place: int, count: int, columns: int = 1) -> None:
        """Где график стоит в окне и как оно разложено.

        Отсюда видно, куда его можно двигать. В столбце это «выше» и «ниже» на
        одно место; в сетке «выше» — это на целый ряд назад, а «левее» — на
        одно, и путать их нельзя: пара стрелок, ходившая по порядку, в сетке
        меняла график то с соседом справа, то с концом прошлого ряда.

        Отсоединённый не двигается вовсе — в окне его нет.
        """
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
            "left": "Поменять местами с графиком слева.",
            "right": "Поменять местами с графиком справа.",
            "up": "Поменять местами с графиком выше." if grid else "Переставить выше.",
            "down": "Поменять местами с графиком ниже." if grid else "Переставить ниже.",
        }
        for where, button in (
            ("left", self.left_button),
            ("right", self.right_button),
            ("up", self.up_button),
            ("down", self.down_button),
        ):
            # Влево и вправо в столбце не показываются вовсе: там нет соседей
            # по горизонтали, и выключенная кнопка обещала бы движение,
            # которого не бывает.
            button.setVisible(movable and (grid or where in ("up", "down")))
            button.setEnabled(allowed[where])
            button.setToolTip(wording[where])
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
        self.detach_button.setText(RETURN_TEXT if self.detached else DETACH_TEXT)
        self.detach_button.setToolTip(
            "Поставить график обратно на своё место в общем окне. То же "
            "делает закрытие этого окна."
            if self.detached
            else "Показать этот график в отдельном окне. Масштаб и спрятанные "
            "серии переезжают вместе с ним."
        )
        self.detach_button.setVisible(self.detachable or self.detached)
        self._place_buttons()
        self.updateGeometry()
        self.update()

    def _place_buttons(self) -> None:
        """Кнопки стоят в правом верхнем углу, в полосе заголовка.

        `isVisibleTo`, а не `isVisible`: у невидимого пока виджета скрыты и
        дети, и кнопка, выставленная до первого показа, осталась бы в углу
        (0, 0) — переставить её было бы уже нечем, размер-то не менялся.
        """
        right = self.width() - PADDING // 2
        for button in reversed(self._corner):
            button.adjustSize()
            if not button.isVisibleTo(self):
                continue
            right -= button.width()
            button.move(max(right, 0), 1)

    def _button_width(self) -> float:
        """Сколько ширины занято кнопками. Ноль — их нет."""
        return float(
            sum(
                button.width()
                for button in self._corner
                if button.isVisibleTo(self)
            )
        )

    def _head_height(self) -> float:
        """Высота полосы над полем графика: заголовок и кнопки стоят в ней."""
        line = self._metrics().height() if (self._chart and self._chart.title) else 0.0
        buttons = [
            float(button.height())
            for button in self._corner
            if button.isVisibleTo(self)
        ]
        return max([line, *buttons])

    def resizeEvent(self, event) -> None:  # noqa: N802 — имя от Qt
        self._place_buttons()
        super().resizeEvent(event)

    # --- содержимое --------------------------------------------------------

    def set_chart(self, chart: Chart | None, keep_view: bool = False) -> None:
        """Заменить данные.

        Спрятанные серии берутся у самого графика: он один знает, какая из них
        по умолчанию мешает. Пересчитывать их тут заново — значит помнить про
        каждый график в двух местах.

        `keep_view` — обновление теми же данными, только свежими: масштаб и
        спрятанные серии остаются. Без этого каждый новый замер сбрасывал
        окно к полному виду, и разглядывать участок кривой во время сбора
        было нельзя вовсе — картинка прыгала на каждом шаге.
        """
        previous = tuple(self._chart.series) if self._chart else ()
        self._chart = chart
        if keep_view:
            self._keep_hidden(previous, chart)
        else:
            self._x = self._y = None
            self._hidden = {
                item.name for item in (chart.series if chart else ()) if not item.visible
            }
        self._apply_minimum()
        self._outside = self._count_outside()
        if self._outside and not self._points_in_view():
            # В кадре не осталось ни одной точки: держать масштаб больше не за
            # что — он показывает пустое поле, и отличить это от «замеров нет»
            # нечем.
            self._x = self._y = None
            self._outside = 0
        self.updateGeometry()
        self.update()

    def _apply_minimum(self) -> None:
        """Ленте и полосе — своя высота, остальным — общий минимум поля.

        Явный минимум виджета сильнее `minimumSizeHint`, и с общими 220
        пикселями лента не могла ужаться до нужных ей 156: разделитель отдавал
        ей ровно столько же, сколько настоящему графику.
        """
        chart = self._chart
        compact = chart is not None and chart.layout == LAYOUT_STACK
        self.setMinimumHeight(self.sizeHint().height() if compact else MIN_HEIGHT)

    def _keep_hidden(self, previous: tuple[Series, ...], chart: Chart | None) -> None:
        """Перенести спрятанные серии на новые данные.

        Спрятанное щелчком остаётся спрятанным; появившаяся серия приходит с
        тем, что о ней думает сам график. Иначе «край диапазона» вылезал бы
        обратно на каждом обновлении — или, наоборот, спрятанная руками серия
        не пряталась бы, будь она у графика видимой по умолчанию.
        """
        known = {item.name for item in previous}
        fresh = tuple(chart.series) if chart else ()
        names = {item.name for item in fresh}
        self._hidden = {name for name in self._hidden if name in names} | {
            item.name
            for item in fresh
            if not item.visible and item.name not in known
        }

    def _count_outside(self) -> int:
        """Сколько точек не попадает в нынешний масштаб."""
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
        """Ручной масштаб по X. None — по данным."""
        return self._x

    def set_linked_cursor(self, value: float | None) -> None:
        """Показать перекрестье соседа: одну вертикаль на его значении X."""
        if self._linked_x == value:
            return
        self._linked_x = value
        self.update()

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

        # Межстрочный интервал, а не высота шрифта: в две строки подпись
        # занимает на пару пикселей больше, и на них она налезала бы на
        # подписи делений.
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
        """Сколько места по вертикали есть у повёрнутой подписи оси Y.

        Меряется по всему виджету, а не по полю графика: подпись стоит сбоку
        и полю не принадлежит, а поле в окне на три графика вдвое ниже своей
        же подписи.
        """
        return max(self.height() - PADDING * 2, 1.0)

    def _y_caption_lines(self, chart: Chart, y_span: Span) -> int:
        """В сколько строк ляжет подпись оси Y: одна строка или две."""
        caption = axis_caption(chart.y, y_span.lo, y_span.hi, self._unit)
        if not caption:
            return 0
        if self._metrics().horizontalAdvance(caption) <= self._y_caption_room():
            return 1
        return Y_CAPTION_LINES

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
        self._render(painter, live=True)
        painter.end()

    def _render(self, painter: QPainter, live: bool = False) -> None:
        """Нарисовать график. `live` — то, чего не должно быть в файле.

        Перекрестье и подсветка точки под курсором — состояние мыши, а не
        график: в скопированной картинке они означали бы, что там что-то
        измерено, хотя это просто место, где стоял курсор.
        """
        chart = self._chart
        frame = self.frame()
        if chart is None or frame is None:
            return

        metrics = self._metrics()
        painter.setPen(self._ink())
        if chart.title:
            # Полоса заголовка отсчитывается от верхнего края виджета, а не
            # вверх от отступа: с `PADDING - height` она начиналась на два
            # пикселя выше нуля, и у букв срезало верх — на глаз это выглядело
            # как наползающий сверху разделитель между графиками.
            #
            # Заголовок стоит по центру того, что осталось от кнопок, а не
            # по центру виджета: ужимать ширину с обеих сторон значило бы
            # отдать кнопкам вдвое больше места, чем они занимают, — в сетке
            # графики вдвое уже, и заголовок начинал прятаться под многоточие
            # на ровном месте.
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
        caption = axis_caption(chart.y, frame.y.lo, frame.y.hi, self._unit)
        lines = self._y_caption_lines(chart, frame.y)
        room = self._y_caption_room()
        if lines > 1:
            # Не влезает в строку — переносим по словам. Обрезка была бы
            # честнее молчания, но «Измерено минус моде» не объясняет ничего.
            caption = metrics.elidedText(caption, Qt.ElideRight, room * lines)
        painter.save()
        # Центр виджета, а не поля: подпись стоит сбоку от поля и его высотой
        # не ограничена, а поля сверху и снизу заняты заголовком и легендой.
        painter.translate(PADDING, self.height() / 2)
        painter.rotate(-90)
        spacing = metrics.lineSpacing() * lines
        # Прямоугольник растёт от точки поворота **вправо**, а не влево:
        # после rotate(-90) координата y уходит в экранный x, и с `-spacing`
        # подпись рисовалась левее отступа — в одну строку у неё срезало
        # край, а вторая строка не попадала в виджет вовсе.
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
            # Стебель до нуля, а не столбик: ноль тут не край шкалы, а сама
            # модель, и от неё отсчитывается промах. Толщина меньше столбика —
            # величина остаётся точкой, стебель только показывает, куда она
            # отклонилась.
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

    def sizeHint(self):  # noqa: N802 — имя от Qt
        """Полосе и ленте высота не нужна: у них нет величины по вертикали.

        Без этого разделитель отдаёт им половину окна, и под полосой висит
        пустое поле в две трети экрана. У ленты покрытия по вертикали отложено
        состояние — три ряда, — и делить с ней высоту поровну незачем.
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

    def _render_crosshair(self, painter: QPainter, chart: Chart, frame: Frame) -> None:
        """Линии от курсора до осей и значения на них.

        Своё перекрестье рисуется полностью, пришедшее от соседа — одной
        вертикалью: горизонталь означала бы, что у соседнего графика такое же
        значение по Y, а у него и величина другая, и размах.
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
            # Кольцо, а не заливка: точка под ним остаётся своего цвета, и
            # видно, какой она серии.
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
        """Значение на оси под линией перекрестья, на подложке.

        Подложка нужна: без неё подпись ложится поверх делений оси и читается
        как ещё одно деление.
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
        """Сказать, что часть свежих данных не попала в удержанный масштаб.

        Молчать нельзя: масштаб держится нарочно, и без подписи «ничего не
        изменилось» после нового замера выглядит поломкой, а не тем, что
        замер лёг за кадром.
        """
        if not self._outside:
            return
        metrics = self._metrics()
        text = f"вне кадра: {self._outside}"
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
        """Левая — рамка и легенда, правая — сдвиг, средняя — меню."""
        position = event.position()
        if event.button() == Qt.RightButton:
            self._pan = position
            return
        if event.button() == Qt.MiddleButton:
            self._show_menu(event.globalPosition().toPoint())
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
        """Запомнить курсор и сказать о нём соседям с общей осью."""
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
        """Курсор ушёл — перекрестья быть не должно ни здесь, ни у соседей."""
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
            # Не рамка, а щелчок: отдать наружу точку, если в неё попали.
            point = self._hit(position)
            if point is not None:
                self.pointPicked.emit(point.key, point.tip)
            return
        self._zoom_to(start, position)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """Двойной правой — полный вид, двойной левой — меню.

        Сброс ушёл с левой кнопки на правую вслед за сдвигом: одной рукой
        держат правую и возят по графику, ей же и возвращают полный вид.
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

    def build_menu(self) -> QMenu:
        """Меню графика. Собирается заново: пункты зависят от состояния."""
        menu = QMenu(self)
        reset = menu.addAction("Сбросить масштаб")
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
                "Вернуть в общее окно" if self.detached else "Отсоединить в своё окно"
            )
            detach.setToolTip(self.detach_button.toolTip())
            detach.triggered.connect(self.detachRequested.emit)
        menu.addSeparator()
        menu.addAction("Копировать картинку").triggered.connect(self.copy_image)
        menu.addAction("Сохранить картинку…").triggered.connect(lambda: self.save_image())
        return menu

    def _show_menu(self, where) -> None:
        self.build_menu().exec(where)
