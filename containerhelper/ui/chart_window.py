"""Modeless chart windows.

Windows, not a fifth tab: the tabs follow the order of work — Calculation,
Records, Model, Calibration — and "Charts" does not fit into that order. A
separate window can also be stretched to the full screen, while a tab would
have to share its height with a table.

The window knows nothing about the store: charts are brought to it
ready-made, and `charts.py` builds them. So there is not a single byte
quantity here.
"""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import QByteArray, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QSplitterHandle,
    QVBoxLayout,
    QWidget,
)

from ..formatting import DEFAULT_UNIT, Unit
from ..plot import LAYOUT_STACK, Chart
from .chart import ChartView

#: Window keys. The saved geometry is named by them too, hence strings, not
#: numbers: after a reordering a list of numbers would drift silently — the
#: same rule as for the active tab.
CHART_NTFS = "ntfs"
CHART_SLACK = "slack"
CHART_FORECAST = "forecast"
CHART_CALC = "calc"

HINT = (
    "Рамка левой кнопкой — приблизить, колесо — масштаб, правая кнопка "
    "зажатой — сдвиг, двойной правой или Esc — сброс. Меню — двойным левым "
    "щелчком или средней кнопкой. Щелчок по легенде прячет серию, щелчок по "
    "точке показывает её целиком."
)

#: Window size on first show. The width is the same for all windows; the
#: height is computed from what is inside: a chart gets PANEL_HEIGHT, while
#: the breakdown bar takes only its own height.
WINDOW_WIDTH = 760
PANEL_HEIGHT = 260
#: Buttons at the top, the point line and the hint at the bottom.
CHROME_HEIGHT = 140

#: How many charts per grid row. Two, not "as many as fit": a third in the row
#: squeezes a chart to a width where tick labels run into each other, and the
#: height saved stops paying off.
GRID_COLUMNS = 2

#: Labels of the layout toggle.
GRID_TEXT = "Сеткой"
COLUMN_TEXT = "Столбцом"

Builder = Callable[[], Chart]


class EvenHandle(QSplitterHandle):
    """A splitter handle that restores equal shares on a double click.

    Without it, heights that have drifted can only be restored by eye: the
    splitter is dragged with the mouse, and hitting "equal" again by hand is
    impossible.
    """

    def __init__(self, orientation, parent) -> None:
        super().__init__(orientation, parent)
        self.setToolTip("Двойной щелчок делит высоту поровну")

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 — Qt's name
        splitter = self.splitter()
        if isinstance(splitter, EvenSplitter):
            splitter.even_out()
        event.accept()


class EvenSplitter(QSplitter):
    """A splitter whose handles can restore equal shares.

    It is not the one that knows what "equal" means: the breakdown bar plots
    nothing vertically, and it needs no equal share. Whoever knows puts their
    own calculation into `equalizer`.
    """

    def __init__(self, orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self.equalizer = None
        # A chart must not collapse to zero: it does not fold into its title,
        # it simply disappears, and the only way to get it back is to hit a
        # handle five pixels wide with the mouse. The chart has its own
        # minimum, and the splitter respects it — as long as collapsing is
        # not allowed.
        self.setChildrenCollapsible(False)

    def createHandle(self) -> QSplitterHandle:  # noqa: N802 — Qt's name
        return EvenHandle(self.orientation(), self)

    def even_out(self) -> None:
        count = self.count()
        if count < 2:
            return
        total = sum(self.sizes()) or self.height()
        sizes = self.equalizer(total) if callable(self.equalizer) else None
        self.setSizes(sizes or [max(total // count, 1)] * count)


class DetachedChart(QWidget):
    """The window of one detached chart.

    The chart widget moves here whole, not redrawn anew: the zoom and the
    hidden series move along with it — so they need not be restored merely
    because the window changed.
    """

    #: Return the chart to the shared window. Closing the window means the
    #: same: a chart is not a document, and closing it apart from its group
    #: makes no sense.
    returned = Signal()
    syncToggled = Signal(bool)
    crossToggled = Signal(bool)

    def __init__(
        self,
        view: ChartView,
        title: str,
        link_x: bool,
        sync: bool,
        cross: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(title)
        self.view = view

        layout = QVBoxLayout(self)
        layout.addWidget(view, 1)

        row = QHBoxLayout()
        # Two check boxes, not one: the zoom and the crosshair link different
        # things. A shared zoom makes the windows show the same stretch; a
        # shared crosshair stays useful even with different stretches — the
        # line is placed by value, and each chart draws it in its own zoom.
        self.sync_check = QCheckBox("Общий масштаб по X")
        self.sync_check.setToolTip(
            "Держать по оси X тот же участок, что и у графиков той же "
            "группы.\n"
            "Выключено — окно живёт своим масштабом."
        )
        self.sync_check.setChecked(sync)
        self.sync_check.setVisible(link_x)
        self.sync_check.toggled.connect(self.syncToggled.emit)
        row.addWidget(self.sync_check)

        self.cross_check = QCheckBox("Общее перекрестье")
        self.cross_check.setToolTip(
            "Показывать на соседних графиках вертикаль под курсором — на том "
            "же значении по X, каждый в своём масштабе.\n"
            "Ради этого общая ось и заведена: горб на остатках стоит ровно "
            "под своей ступенью наклона."
        )
        self.cross_check.setChecked(cross)
        self.cross_check.setVisible(link_x)
        self.cross_check.toggled.connect(self.crossToggled.emit)
        row.addWidget(self.cross_check)
        row.addStretch(1)
        # There is no return button here: it sits on the chart itself, in the
        # corner, and a second one at the bottom would say the same thing.
        layout.addLayout(row)

        #: How much of the window is taken by things other than the chart.
        #: Needed to fit the window height to the chart itself, not to the
        #: chart plus who knows what.
        self.chrome_height = (
            (row.sizeHint().height() if link_x else 0)
            + layout.spacing()
            + layout.contentsMargins().top()
            + layout.contentsMargins().bottom()
        )

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt's name
        """The window was closed — the chart returns instead of disappearing.

        Otherwise it would vanish with the window, and there would be nothing
        to bring it back with: the window's list of charts is fixed at
        construction and cannot grow.
        """
        self.returned.emit()
        event.accept()


class ChartWindow(QWidget):
    """One window with one or several charts."""

    def __init__(
        self,
        key: str,
        title: str,
        builders: Sequence[Builder],
        link_x: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.Window)
        self.key = key
        self.title = title
        self._builders = list(builders)
        self._link_x = link_x
        self._unit: Unit = DEFAULT_UNIT
        #: Key of the point shown in the line under the charts. By it the
        #: description is updated along with the data: the measurement may
        #: have changed, and the line would be telling about yesterday's
        #: numbers.
        self._picked: object = None
        self.setWindowTitle(title)

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_actions())

        self.splitter = EvenSplitter(Qt.Vertical)
        self.splitter.equalizer = self._even_sizes
        self.splitter.splitterMoved.connect(self._on_outer_moved)
        self.views: list[ChartView] = []
        #: Detached charts by their number in the list — and their windows.
        self._windows: dict[int, DetachedChart] = {}
        #: Whether to keep the shared zoom. For a detached chart, per the check
        #: box in its window; for one in the shared window, always: it stands
        #: alongside anyway.
        self._sync: list[bool] = []
        #: The crosshair link — separate from the zoom link.
        self._cross: list[bool] = []
        #: A chart's height in the shared window and the geometry of its own
        #: window are two different quantities and must not be confused: in
        #: the shared window the chart shares the height with its neighbours,
        #: in its own it takes the whole window. Both are remembered so that
        #: detaching and returning disturb neither.
        self._sections: list[int] = []
        self._geometry: dict[int, QByteArray] = {}
        #: The order of charts in the window, as numbers. Changed by the
        #: buttons on the charts themselves: only the person looking knows
        #: which one is more needed on top.
        self._order: list[int] = []
        #: Layout: column or grid. Each has its own window size — just as a
        #: chart has its own height in the window and its own size in a
        #: separate window: the grid needs width, the column height, and one
        #: number can't describe both.
        self._grid = False
        self._sizes: dict[bool, QSize] = {}
        #: Grid: row heights and the **shared** column widths. The widths are
        #: one set for all rows: two rows with different column boundaries are
        #: no longer a grid but two separate pairs, and the alignment the grid
        #: is switched on for is lost.
        self._rows: list[int] = []
        self._columns: list[int] = []
        #: Widths are being sent out to the rows — other rows' signals are
        #: ignored meanwhile.
        self._syncing = False
        #: How the **current** splitter is built: layout and order. Not the
        #: same as `_grid` and `_order`: between a change of those fields and
        #: the rebuild the splitter still holds the previous arrangement, and
        #: sizes must be read off it by what is there, not by what is
        #: intended. Otherwise the heights are credited to charts that have
        #: already been swapped — and both swap at once, which means they do
        #: not swap at all.
        self._built_grid = False
        self._built_order: list[int] = []
        #: The shared window's height before something left it, and the height
        #: it got instead. The second is needed to tell "the window is still
        #: as we shrank it" from "the person dragged the edge themselves": in
        #: the second case the old height must not be restored.
        self._height_before = 0
        self._shrunk_to = 0
        for index, _builder in enumerate(self._builders):
            view = ChartView()
            view.set_detachable(len(self._builders) > 1)
            view.pointPicked.connect(self._on_picked)
            view.rangeChanged.connect(self._on_range_changed)
            view.cursorMoved.connect(self._on_cursor_moved)
            view.cursorLeft.connect(self._on_cursor_left)
            view.detachRequested.connect(
                lambda number=index: self.toggle_detached(number)
            )
            view.moveRequested.connect(
                lambda step, number=index: self.move_view(number, step)
            )
            self.views.append(view)
            self._sync.append(True)
            self._cross.append(True)
            self._sections.append(0)
            self._order.append(index)
        layout.addWidget(self.splitter, 1)
        self._rebuild_layout()

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setToolTip(
            "Что за точка под курсором. Заполняется щелчком по ней — таблица "
            "с числами живёт в главном окне, и подсвечивать в ней строку "
            "из-под другого окна было бы некуда смотреть."
        )
        layout.addWidget(self.detail)

        hint = QLabel(HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(hint)

        self._refresh_grid_button()
        self.refresh()
        self.resize(self._natural_size())

    def _natural_size(self) -> QSize:
        """A size that fits all panels — but no bigger than the screen.

        The height can't be counted by the number of panels: the breakdown
        bar takes its own height, not a share of the window, and the window
        would come out taller than needed. The screen is asked too: Qt will
        shrink a window taller than it anyway, and the splitter will hand the
        shortfall out to the panels any which way.
        """
        heights = [
            view.sizeHint().height() if self._compact(view) else PANEL_HEIGHT
            for view in self.views
        ]
        if self._grid:
            rows = [
                max(heights[start : start + GRID_COLUMNS])
                for start in range(0, len(heights), GRID_COLUMNS)
            ]
            width, height = WINDOW_WIDTH * GRID_COLUMNS, CHROME_HEIGHT + sum(rows)
        else:
            width, height = WINDOW_WIDTH, CHROME_HEIGHT + sum(heights)

        screen = self.screen()
        if screen is not None:
            room = screen.availableGeometry()
            width = min(width, room.width() - 40)
            height = min(height, room.height() - 60)
        return QSize(int(width), int(max(height, PANEL_HEIGHT)))

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self.reset_button = QPushButton("Сбросить масштаб")
        self.reset_button.setToolTip(
            "Вернуть все графики окна к полному виду. То же делает двойной "
            "щелчок правой кнопкой по графику или Esc."
        )
        self.reset_button.clicked.connect(self.reset_zoom)
        row.addWidget(self.reset_button)

        self.grid_button = QPushButton(GRID_TEXT)
        self.grid_button.clicked.connect(lambda: self.set_grid(not self._grid))
        row.addWidget(self.grid_button)

        self.copy_button = QPushButton("Копировать картинку")
        self.copy_button.setToolTip(
            "Положить график в буфер обмена. Копируется тот, на который "
            "наводили последним."
        )
        self.copy_button.clicked.connect(lambda: self.active_view().copy_image())
        row.addWidget(self.copy_button)

        self.save_button = QPushButton("Сохранить картинку…")
        self.save_button.setToolTip(
            "PNG, SVG или PDF — по расширению в имени файла. SVG и PDF "
            "векторные: их можно увеличивать без потери."
        )
        self.save_button.clicked.connect(lambda: self.active_view().save_image())
        row.addWidget(self.save_button)

        row.addStretch(1)
        return row

    # --- layout ------------------------------------------------------------

    def _visible_order(self) -> list[int]:
        """Numbers of the charts in the window, in the order they stand."""
        return [index for index in self._order if index not in self._windows]

    def _rebuild_layout(self) -> None:
        """Lay the charts out in the splitter anew — as a column or a grid.

        Everything is rebuilt whole, not patched in place: detaching,
        returning, moving and switching the layout change the composition in
        the same way, and four different ways of patching the same thing
        would drift apart at the very first edit. The widgets are moved, not
        created: the zoom and the hidden series live in them.
        """
        self._remember_layout()
        order = self._visible_order()
        for index, view in enumerate(self.views):
            if index not in self._windows:
                view.setParent(None)
        while self.splitter.count():
            row = self.splitter.widget(0)
            row.setParent(None)
            row.deleteLater()

        if self._grid:
            for start in range(0, len(order), GRID_COLUMNS):
                pack = order[start : start + GRID_COLUMNS]
                if len(pack) == 1:
                    # An odd last chart stretches to the full width: half a
                    # row left empty is just wasted space.
                    self.splitter.addWidget(self.views[pack[0]])
                    continue
                row = EvenSplitter(Qt.Horizontal)
                row.equalizer = lambda total, item=row: self._even_columns(total, item)
                for index in pack:
                    row.addWidget(self.views[index])
                row.splitterMoved.connect(
                    lambda _pos=0, _index=0, item=row: self._on_row_moved(item)
                )
                self.splitter.addWidget(row)
            self._apply_rows()
            self._apply_columns()
        else:
            for index in order:
                self.splitter.addWidget(self.views[index])

        columns = GRID_COLUMNS if self._grid else 1
        for place, index in enumerate(order):
            view = self.views[index]
            view.show()
            view.set_place(place, len(order), columns)
        for index in self._windows:
            self.views[index].set_place(0, 1)
        self._built_grid = self._grid
        self._built_order = list(order)
        self._refresh_stretch()
        self._restore_sections()

    def _rows_in_grid(self) -> list[EvenSplitter]:
        """The grid rows. A single chart inserted without a row is left out."""
        return [
            widget
            for place in range(self.splitter.count())
            if isinstance(widget := self.splitter.widget(place), EvenSplitter)
        ]

    def _apply_rows(self) -> None:
        if self._rows and len(self._rows) == self.splitter.count():
            self.splitter.setSizes(self._rows)

    def _apply_columns(self, skip: EvenSplitter | None = None) -> None:
        """Send the shared column widths out to all rows."""
        if not self._columns:
            return
        self._syncing = True
        try:
            for row in self._rows_in_grid():
                if row is not skip and row.count() == len(self._columns):
                    row.setSizes(self._columns)
        finally:
            self._syncing = False

    def _on_row_moved(self, row: EvenSplitter) -> None:
        """A column boundary was dragged in one row — move it in all of them.

        Otherwise the rows drift apart and the grid stops being a grid: charts
        in one column end up with different widths, and comparing them by eye
        is no longer possible.
        """
        if self._syncing or row.count() != GRID_COLUMNS:
            return
        self._columns = row.sizes()
        self._apply_columns(skip=row)

    def _even_columns(self, total: int, row: EvenSplitter) -> list[int]:
        """Equal shares horizontally — in all rows at once."""
        count = max(row.count(), 1)
        sizes = [max(total // count, 1)] * count
        if count == GRID_COLUMNS:
            self._columns = list(sizes)
            self._apply_columns(skip=row)
        return sizes

    def _on_outer_moved(self, *_args) -> None:
        """Heights dragged by mouse: rows in the grid, charts in a column."""
        if self._grid:
            self._rows = self.splitter.sizes()
        else:
            self._remember_sections()

    def set_grid(self, grid: bool) -> None:
        """Switch the layout, changing the window size along the way.

        The layouts have different sizes, remembered separately: the grid
        needs width, the column height. Keeping the size is not an option — in
        the grid at the column's height the charts would stretch twofold, and
        in the column at the grid's width half of them would slide past the
        bottom edge.
        """
        if grid == self._grid:
            return
        self._sizes[self._grid] = self.size()
        self._grid = grid
        self._rebuild_layout()
        self._apply_size()
        self._refresh_grid_button()

    def _apply_size(self) -> None:
        stored = self._sizes.get(self._grid)
        size = stored if stored is not None else self._natural_size()
        self.resize(size)

    def _refresh_grid_button(self) -> None:
        self.grid_button.setText(COLUMN_TEXT if self._grid else GRID_TEXT)
        self.grid_button.setToolTip(
            "Поставить графики в столбец, один под другим."
            if self._grid
            else "Разложить графики по два в ряд. Окно станет ниже и шире — "
            "по вертикали место дороже."
        )
        self.grid_button.setVisible(len(self.views) > 1)

    def move_view(self, index: int, step: int) -> None:
        """Swap the chart with its neighbour in the order.

        The neighbour **in the window**: a detached chart is stepped over,
        otherwise the press would look as if it failed — the swap did happen,
        but it did not change the visible order.
        """
        order = self._visible_order()
        if index not in order:
            return
        place = order.index(index) + step
        if not 0 <= place < len(order):
            return
        other = order[place]
        # The charts swap places, not sizes: a height stays with its chart,
        # column widths and row heights with the grid. Swapping sizes on top
        # of swapping places left one thing in memory and another on screen,
        # and the very next layout switch handed the charts each other's
        # sizes.
        here, there = self._order.index(index), self._order.index(other)
        self._order[here], self._order[there] = self._order[there], self._order[here]
        self._rebuild_layout()

    # --- data --------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the charts. Called when the records or the models changed.

        Without this the window would silently show yesterday's picture: a
        measurement was taken, the model shifted, and the chart is still the
        same — and there is no way to tell fresh from stale by eye.

        The zoom, the hidden series and the selected point stay: the window is
        opened to examine a stretch, while collection goes in steps, and on
        every step the picture would jump to the full view.
        """
        for view, builder in zip(self.views, self._builders):
            view.set_chart(builder(), keep_view=True)
            view.set_unit(self._unit)
        self._refresh_stretch()
        self._refresh_detail()

    def _compact(self, view: ChartView) -> bool:
        """Whether the chart takes its own height exactly, not a window share.

        The breakdown bar plots nothing vertically: its components are in one
        line. There is nothing to stretch.
        """
        chart = view.chart()
        return chart is not None and chart.layout == LAYOUT_STACK

    def _even_sizes(self, total: int) -> list[int]:
        """Equal heights: compact charts get their own, others share the rest.

        In the grid there is nothing to compute: the splitter holds rows, and
        rows are equal by definition.
        """
        if self._grid:
            count = self.splitter.count()
            if count < 1:
                return []
            # Also align the columns to the first row: "equal" vertically is
            # pressed when the grid has sprawled, and leaving it crooked
            # horizontally would mean not doing what was asked. To the first
            # row, not equally: the person may have set the widths on
            # purpose, and the rows must be aligned with each other, not
            # everything pulled to the middle.
            for row in self._rows_in_grid():
                if row.count() == GRID_COLUMNS and all(row.sizes()):
                    self._columns = row.sizes()
                    break
            self._apply_columns()
            self._rows = [max(total // count, 1)] * count
            return list(self._rows)
        places = self._attached_indexes()
        if not places:
            return []
        fixed = {
            place: self.views[index].sizeHint().height()
            for place, index in enumerate(places)
            if self._compact(self.views[index])
        }
        rest = max(total - sum(fixed.values()), len(places) - len(fixed))
        share = max(rest // max(len(places) - len(fixed), 1), 1)
        return [fixed.get(place, share) for place in range(len(places))]

    def _refresh_stretch(self) -> None:
        """Spare window height goes to the charts, not to the breakdown bar.

        Its own minimum is not enough: it stops the bar from being **shrunk**,
        but the splitter is free to stretch it to a third of the window
        without asking. So the height is set explicitly — but only while it
        is wrong: after that, adjusting the heights belongs to the person.
        """
        if self._grid:
            # The splitter holds rows: they share the height equally, and that
            # does not hurt compact charts inside a row — they have their own
            # minimum.
            for place in range(self.splitter.count()):
                self.splitter.setStretchFactor(place, 1)
            return
        sizes = self.splitter.sizes()
        places = self._attached_indexes()
        if len(sizes) != len(places):
            return
        for place, index in enumerate(places):
            view = self.views[index]
            self.splitter.setStretchFactor(place, 0 if self._compact(view) else 1)
            if self._compact(view) and sizes[place] != view.sizeHint().height():
                self.splitter.setSizes(self._even_sizes(sum(sizes)))
                return

    def _refresh_detail(self) -> None:
        """Update the description of the selected point from fresh data.

        By key, not by the saved text: the measurement may have changed, and
        the line would tell about yesterday's numbers without giving itself
        away. If the point is gone, the line empties.
        """
        if self._picked is None:
            return
        for view in self.views:
            chart = view.chart()
            for series in chart.series if chart else ():
                for point in series.points:
                    if point.key is not None and point.key == self._picked:
                        self.detail.setText(point.tip.replace("\n", "   ·   "))
                        return
        self._picked = None
        self.detail.setText("")

    def set_unit(self, unit: Unit) -> None:
        self._unit = unit
        for view in self.views:
            view.set_unit(unit)

    def reset_zoom(self) -> None:
        for view in self.views:
            view.reset_zoom()

    # --- detaching ---------------------------------------------------------

    def detached_indexes(self) -> list[int]:
        return sorted(self._windows)

    def toggle_detached(self, index: int) -> None:
        if index in self._windows:
            self.attach(index)
        else:
            self.detach(index)

    def detach(self, index: int, geometry: QByteArray | None = None) -> None:
        """Move the chart to its own window, taking its height from this one.

        The widget moves whole, so the zoom, the hidden series and the
        crosshair stay with it. In the `views` list it keeps its place:
        updates, the display unit and the shared X axis walk the list, not
        whoever lives where.
        """
        if index in self._windows or not 0 <= index < len(self.views):
            return
        view = self.views[index]
        height = max(view.height(), view.sizeHint().height())
        chart = view.chart()
        window = DetachedChart(
            view,
            f"{self.title} — {chart.title if chart else ''}".strip(" —"),
            self._link_x,
            self._sync[index],
            self._cross[index],
            self,
        )
        view.set_detached(True)
        window.returned.connect(lambda number=index: self.attach(number))
        window.syncToggled.connect(lambda flag, number=index: self._set_sync(number, flag))
        window.crossToggled.connect(
            lambda flag, number=index: self._cross.__setitem__(number, flag)
        )
        self._windows[index] = window

        remembered = geometry if geometry is not None else self._geometry.get(index)
        if remembered is not None and not remembered.isEmpty():
            window.restoreGeometry(remembered)
        else:
            # The first time — by the chart's own height, not the shared
            # window's: there it shared the height with its neighbours, here
            # it takes all of it.
            window.resize(max(self.width(), 480), height + window.chrome_height)
        window.show()
        self._rebuild_layout()

        # The shared window shrinks by exactly what left it: otherwise an
        # empty band a third of the screen tall remains where the detached
        # chart was.
        if len(self._windows) == 1:
            self._height_before = self.height()
        if self._windows.keys() != set(range(len(self.views))):
            # The layout must be recomputed before `resize`: while it thinks
            # the chart is still in the window, its minimum holds the old
            # height, and the window does not shrink at all.
            self.layout().activate()
            self.resize(
                self.width(),
                max(self.height() - height, self.minimumSizeHint().height()),
            )
            self._shrunk_to = self.height()

    def attach(self, index: int) -> None:
        """Return the chart to its place and the window its height."""
        window = self._windows.pop(index, None)
        if window is None:
            return
        # The separate window's size is remembered: the next detach opens it
        # the same, not refitted to the shared window.
        self._geometry[index] = window.saveGeometry()
        view = self.views[index]
        view.set_detached(False)
        # Ask before inserting: an inserted chart raises the layout minimum,
        # and the window grows by itself — while we need to know whether this
        # is our height or the person dragged the edge themselves.
        ours = bool(self._shrunk_to) and self.height() == self._shrunk_to
        # Into its own place, not "at the end": the chart order is given by
        # the list, and a returning chart stands where it left from.
        self._rebuild_layout()
        self.layout().activate()
        if not self._windows and ours and self._height_before:
            # Everything is back, and nobody has touched the window height
            # since: restore exactly the one it had before the first detach.
            # It can't be computed by addition — the layout minimum keeps the
            # window from shrinking, but nothing keeps it from growing back,
            # and over three rounds the window crept a third of the screen
            # downwards.
            self.resize(self.width(), self._height_before)
            self._height_before = self._shrunk_to = 0
        window.returned.disconnect()
        window.close()
        window.deleteLater()

    def _remember_layout(self) -> None:
        """Read the sizes off the layout that is in place right now.

        Done before every rebuild, not on a splitter signal: `splitterMoved`
        comes only from the mouse, and anything rearranged programmatically
        never reached memory at all.
        """
        if self._built_grid:
            self._remember_grid()
        else:
            self._remember_sections()

    def _remember_grid(self) -> None:
        """Row heights and the shared column widths.

        The widths are taken from the first full row: they are one set for
        all rows, and the others hold the same.
        """
        sizes = self.splitter.sizes()
        if sizes and all(sizes):
            self._rows = sizes
        for row in self._rows_in_grid():
            if row.count() == GRID_COLUMNS and all(row.sizes()):
                self._columns = row.sizes()
                return

    def _remember_sections(self) -> None:
        """Remember how tall each chart is in the shared window.

        By chart number, not as the splitter's list: the list holds only the
        charts now in the window, and after a return it would drift out of
        line.
        """
        sizes = self.splitter.sizes()
        for place, index in enumerate(self._built_order):
            if place < len(sizes) and sizes[place]:
                self._sections[index] = sizes[place]

    def _restore_sections(self) -> None:
        if self._grid:
            return
        sizes = [self._sections[index] for index in self._attached_indexes()]
        if sizes and all(sizes):
            self.splitter.setSizes(sizes)

    def _attached_indexes(self) -> list[int]:
        return self._visible_order()

    def _set_sync(self, index: int, enabled: bool) -> None:
        self._sync[index] = enabled
        if enabled:
            # Just linked — pull it to the shared zoom right away, not on the
            # next move: otherwise the check box looks as if it did not work.
            for other in self.views:
                if other is not self.views[index] and self._is_synced(other):
                    self.views[index].apply_x(other.x_span())
                    return

    def _is_synced(self, view: ChartView) -> bool:
        index = self.views.index(view)
        return index not in self._windows or self._sync[index]

    def _is_tracked(self, view: ChartView) -> bool:
        """Whether the crosshair is linked. Always so in the shared window."""
        index = self.views.index(view)
        return index not in self._windows or self._cross[index]

    def _synced_views(self) -> list[ChartView]:
        return [view for view in self.views if self._is_synced(view)]

    def _tracked_views(self) -> list[ChartView]:
        return [view for view in self.views if self._is_tracked(view)]

    # --- links between charts ----------------------------------------------

    def active_view(self) -> ChartView:
        """The chart being looked at. By default, the top one."""
        for view in self.views:
            if view.hasFocus() or view.underMouse():
                return view
        return self.views[0]

    def _on_range_changed(self) -> None:
        """Keep the shared X axis, if the charts share it.

        The shared axis is not decoration: the hump in the residuals must
        stand exactly under its step in the slope, otherwise the two charts
        read separately and the link between them is lost.
        """
        if not self._link_x:
            return
        source = self.sender()
        if not isinstance(source, ChartView) or not self._is_synced(source):
            return
        span = source.x_span()
        for view in self._synced_views():
            if view is not source:
                view.apply_x(span)

    def _on_cursor_moved(self, value: float) -> None:
        """Repeat the crosshair on the neighbours with a shared axis.

        One vertical line through both charts is the only way to see that the
        hump in the residuals stands exactly at the volume size where the
        slope changed.
        """
        if not self._link_x:
            return
        source = self.sender()
        if not isinstance(source, ChartView) or not self._is_tracked(source):
            return
        for view in self._tracked_views():
            if view is not source:
                view.set_linked_cursor(value)

    def _on_cursor_left(self) -> None:
        if not self._link_x:
            return
        for view in self.views:
            view.set_linked_cursor(None)

    def _on_picked(self, key: object, tip: str) -> None:
        self._picked = key
        self.detail.setText(tip.replace("\n", "   ·   "))

    # --- saving the layout -------------------------------------------------

    def save_layout(self, settings) -> None:
        """Save the window geometry and all about detached charts to settings.

        By the window's key name, not a number: the same rule as for the
        active tab — after a reordering a list of numbers would drift
        silently.
        """
        self._remember_layout()
        settings.setValue(f"chart_{self.key}/geometry", self.saveGeometry())
        settings.setValue(f"chart_{self.key}/detached", self.detached_indexes())
        for index, window in self._windows.items():
            self._geometry[index] = window.saveGeometry()
        for index, geometry in self._geometry.items():
            settings.setValue(f"chart_{self.key}/detached_{index}/geometry", geometry)
        for index, sync in enumerate(self._sync):
            settings.setValue(f"chart_{self.key}/sync_{index}", sync)
        for index, cross in enumerate(self._cross):
            settings.setValue(f"chart_{self.key}/cross_{index}", cross)
        settings.setValue(f"chart_{self.key}/sections", self._sections)
        settings.setValue(f"chart_{self.key}/order", self._order)
        settings.setValue(f"chart_{self.key}/grid", self._grid)
        settings.setValue(f"chart_{self.key}/rows", self._rows)
        settings.setValue(f"chart_{self.key}/columns", self._columns)

    def restore_layout(self, settings) -> None:
        geometry = settings.value(f"chart_{self.key}/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        for index in range(len(self.views)):
            self._sync[index] = settings.value(
                f"chart_{self.key}/sync_{index}", True, type=bool
            )
            self._cross[index] = settings.value(
                f"chart_{self.key}/cross_{index}", True, type=bool
            )
        sections = settings.value(f"chart_{self.key}/sections", [], type=list)
        if len(sections) == len(self._sections):
            self._sections = [int(value) for value in sections]
        order = [int(value) for value in settings.value(f"chart_{self.key}/order", [], type=list)]
        # Check the composition, not the length: the saved order may hold
        # numbers from a previous chart count, and the layout would silently
        # lose one of them.
        if sorted(order) == list(range(len(self.views))):
            self._order = order
        self._rows = [
            int(value) for value in settings.value(f"chart_{self.key}/rows", [], type=list)
        ]
        self._columns = [
            int(value)
            for value in settings.value(f"chart_{self.key}/columns", [], type=list)
        ]
        if settings.value(f"chart_{self.key}/grid", False, type=bool):
            self._grid = True
            self._refresh_grid_button()
        self._rebuild_layout()
        for index in range(len(self.views)):
            geometry = settings.value(f"chart_{self.key}/detached_{index}/geometry")
            if geometry:
                self._geometry[index] = QByteArray(geometry)
        stored = settings.value(f"chart_{self.key}/detached", [], type=list)
        for value in stored:
            index = int(value)
            if 0 <= index < len(self.views):
                self.detach(index)
        self._restore_sections()

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt's name
        """The shared window was closed — the detached charts go with it.

        Otherwise they would stay hanging without an owner: this window
        updates them anyway, and nothing would show it again — the tab button
        raises the same window, the one already open.
        """
        for window in self._windows.values():
            window.hide()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 — Qt's name
        for window in self._windows.values():
            window.show()
        super().showEvent(event)
