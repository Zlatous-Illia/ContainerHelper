"""Немодальные окна с графиками.

Окнами, а не пятой вкладкой: вкладки идут порядком работы — «Расчёт», «Записи»,
«Модель», «Калибровка», — и «Графики» в этот порядок не встают. Отдельное окно
к тому же растягивается на весь экран, а вкладке пришлось бы делить высоту с
таблицей.

Окно ничего не знает о хранилище: графики ему приносят готовыми, а собирает их
`charts.py`. Поэтому здесь нет ни одной величины в байтах.
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

#: Ключи окон. Ими же именуется сохранённая геометрия, поэтому строки, а не
#: номера: от перестановки список номеров разъехался бы молча — то же правило,
#: что и для активной вкладки.
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

#: Размер окна при первом показе. Ширина одна на все окна, высота считается
#: по тому, что внутри: панели разной высоты не бывают редкостью — лента
#: покрытия вдвое ниже настоящего графика.
WINDOW_WIDTH = 760
PANEL_HEIGHT = 260
#: Кнопки сверху, строка о точке и подсказка снизу.
CHROME_HEIGHT = 140

#: Сколько графиков в ряду сетки. Двойка, а не «сколько влезет»: третий в ряду
#: сжимает график до ширины, на которой подписи делений сходятся друг к другу,
#: и экономия высоты перестаёт окупаться.
GRID_COLUMNS = 2

#: Подписи переключателя раскладки.
GRID_TEXT = "Сеткой"
COLUMN_TEXT = "Столбцом"

Builder = Callable[[], Chart]


class EvenHandle(QSplitterHandle):
    """Ручка разделителя, возвращающая равные доли двойным щелчком.

    Без неё вернуть съехавшие высоты можно только на глаз: разделитель тянут
    мышью, и попасть обратно в «поровну» руками нельзя.
    """

    def __init__(self, orientation, parent) -> None:
        super().__init__(orientation, parent)
        self.setToolTip("Двойной щелчок делит высоту поровну")

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 — имя от Qt
        splitter = self.splitter()
        if isinstance(splitter, EvenSplitter):
            splitter.even_out()
        event.accept()


class EvenSplitter(QSplitter):
    """Разделитель, чьи ручки умеют возвращать равные доли.

    «Поровну» знает не он: полоса разложения и лента покрытия по вертикали
    ничего не откладывают, и равная доля им не нужна. Кто это знает —
    подставляет свой расчёт в `equalizer`.
    """

    def __init__(self, orientation, parent=None) -> None:
        super().__init__(orientation, parent)
        self.equalizer = None
        # Схлопнуть график в ноль нельзя: он не сворачивается в заголовок, он
        # просто исчезает, и вернуть его можно только попав мышью в ручку
        # шириной в пять пикселей. Минимум у графика свой, разделитель его
        # уважает — если ему не разрешать схлопывание.
        self.setChildrenCollapsible(False)

    def createHandle(self) -> QSplitterHandle:  # noqa: N802 — имя от Qt
        return EvenHandle(self.orientation(), self)

    def even_out(self) -> None:
        count = self.count()
        if count < 2:
            return
        total = sum(self.sizes()) or self.height()
        sizes = self.equalizer(total) if callable(self.equalizer) else None
        self.setSizes(sizes or [max(total // count, 1)] * count)


class DetachedChart(QWidget):
    """Окно одного отсоединённого графика.

    Виджет графика переезжает сюда целиком, а не рисуется заново: вместе с ним
    переезжают и масштаб, и спрятанные серии — их не пришлось бы восстанавливать
    только потому, что окно сменилось.
    """

    #: Вернуть график в общее окно. Закрытие окна значит то же самое: график —
    #: не документ, закрывать его отдельно от группы бессмысленно.
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
        # Две галочки, а не одна: масштаб и перекрестье связывают разное.
        # Общий масштаб заставляет окна показывать один и тот же участок;
        # общее перекрестье остаётся полезным и при разных участках — линия
        # ставится по значению, и каждый рисует её в своём масштабе.
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
        # Кнопки возврата тут нет: она стоит на самом графике, в углу, и
        # вторая такая же внизу была бы про то же самое.
        layout.addLayout(row)

        #: Сколько в окне занято не графиком. Нужно, чтобы подогнать высоту
        #: окна под сам график, а не под него плюс неизвестно что.
        self.chrome_height = (
            (row.sizeHint().height() if link_x else 0)
            + layout.spacing()
            + layout.contentsMargins().top()
            + layout.contentsMargins().bottom()
        )

    def closeEvent(self, event) -> None:  # noqa: N802 — имя от Qt
        """Закрыли окно — график возвращается, а не исчезает.

        Иначе он пропал бы вместе с окном, и вернуть его было бы нечем: список
        графиков окна задан при сборке и пополняться не умеет.
        """
        self.returned.emit()
        event.accept()


class ChartWindow(QWidget):
    """Одно окно с одним или несколькими графиками."""

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
        #: Ключ точки, показанной в строке под графиками. По нему описание
        #: обновляется вместе с данными: замер мог измениться, а строка
        #: рассказывала бы о вчерашних числах.
        self._picked: object = None
        self.setWindowTitle(title)

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_actions())

        self.splitter = EvenSplitter(Qt.Vertical)
        self.splitter.equalizer = self._even_sizes
        self.splitter.splitterMoved.connect(self._on_outer_moved)
        self.views: list[ChartView] = []
        #: Отсоединённые графики по номеру в списке — и их окна.
        self._windows: dict[int, DetachedChart] = {}
        #: Держать ли общий масштаб. У отсоединённого — по галочке в его окне,
        #: у стоящего в общем окне — всегда: он и так стоит рядом.
        self._sync: list[bool] = []
        #: Связь перекрестья — отдельно от связи масштаба.
        self._cross: list[bool] = []
        #: Высота графика в общем окне и геометрия его отдельного окна — две
        #: разные величины, и путать их нельзя: в общем окне график делит
        #: высоту с соседями, а в своём занимает всё окно. Обе запоминаются,
        #: чтобы отсоединение и возврат не переставляли ни ту, ни другую.
        self._sections: list[int] = []
        self._geometry: dict[int, QByteArray] = {}
        #: Порядок графиков в окне — номерами. Переставляется кнопками на
        #: самих графиках: какой из них нужнее сверху, знает только тот, кто
        #: смотрит.
        self._order: list[int] = []
        #: Раскладка: столбцом или сеткой. У каждой свой размер окна — как у
        #: графика своя высота в окне и свой размер в отдельном окне: сетке
        #: нужна ширина, столбцу высота, и одним числом их не описать.
        self._grid = False
        self._sizes: dict[bool, QSize] = {}
        #: Сетка: высоты рядов и **общие** ширины столбцов. Ширины одни на все
        #: ряды: два ряда с разными границами столбцов — это уже не сетка, а
        #: две отдельные пары, и выравнивание, ради которого сетку и включают,
        #: пропадает.
        self._rows: list[int] = []
        self._columns: list[int] = []
        #: Идёт рассылка ширин по рядам — чужие сигналы в это время не слушаем.
        self._syncing = False
        #: Как собран **нынешний** разделитель: раскладкой и порядком. Не то
        #: же, что `_grid` и `_order`: между сменой этих полей и пересборкой в
        #: разделителе ещё стоит прежнее, и снимать с него размеры надо по
        #: тому, что там есть, а не по тому, что задумано. Иначе высоты
        #: приписываются уже переставленным графикам — и оба меняются местами
        #: разом, то есть не меняются вовсе.
        self._built_grid = False
        self._built_order: list[int] = []
        #: Высота общего окна до того, как из него что-то ушло, и высота,
        #: которую оно получило взамен. Вторая нужна, чтобы отличить «окно
        #: осталось как мы его ужали» от «человек потянул за край сам»:
        #: во втором случае возвращать прежнюю высоту нельзя.
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
        """Размер под все панели — но не больше экрана.

        Высоту нельзя считать числом панелей: полоса разложения занимает своё,
        а не долю окна, и окно выходило бы длиннее, чем нужно. Экран тоже
        спрашивается: окно выше него Qt всё равно ужмёт, а разделитель раздаст
        эту нехватку панелям как придётся.
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

    # --- раскладка ---------------------------------------------------------

    def _visible_order(self) -> list[int]:
        """Номера графиков, стоящих в окне, — в том порядке, в каком они там."""
        return [index for index in self._order if index not in self._windows]

    def _rebuild_layout(self) -> None:
        """Сложить графики в разделитель заново — столбцом или сеткой.

        Пересобирается всё целиком, а не правится по месту: отсоединение,
        возврат, перестановка и смена раскладки меняют состав одинаково, и
        четыре разных способа поправить одно и то же разошлись бы на первой же
        правке. Виджеты при этом переезжают, а не создаются: масштаб и
        спрятанные серии живут в них.
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
                    # Нечётный последний растягивается на всю ширину: половина
                    # ряда пустой — это просто выброшенное место.
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
        """Ряды сетки. Одиночный график, вставленный без ряда, сюда не идёт."""
        return [
            widget
            for place in range(self.splitter.count())
            if isinstance(widget := self.splitter.widget(place), EvenSplitter)
        ]

    def _apply_rows(self) -> None:
        if self._rows and len(self._rows) == self.splitter.count():
            self.splitter.setSizes(self._rows)

    def _apply_columns(self, skip: EvenSplitter | None = None) -> None:
        """Разослать общие ширины столбцов по всем рядам."""
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
        """Границу столбца потянули в одном ряду — сдвинуть её во всех.

        Иначе ряды разъезжаются и сетка перестаёт быть сеткой: графики одного
        столбца оказываются разной ширины, и сравнивать их глазом больше
        нельзя.
        """
        if self._syncing or row.count() != GRID_COLUMNS:
            return
        self._columns = row.sizes()
        self._apply_columns(skip=row)

    def _even_columns(self, total: int, row: EvenSplitter) -> list[int]:
        """«Поровну» по горизонтали — сразу во всех рядах."""
        count = max(row.count(), 1)
        sizes = [max(total // count, 1)] * count
        if count == GRID_COLUMNS:
            self._columns = list(sizes)
            self._apply_columns(skip=row)
        return sizes

    def _on_outer_moved(self, *_args) -> None:
        """Высоты потянули мышью: в сетке это ряды, в столбце — графики."""
        if self._grid:
            self._rows = self.splitter.sizes()
        else:
            self._remember_sections()

    def set_grid(self, grid: bool) -> None:
        """Переключить раскладку, поменяв заодно размер окна.

        Размеры у раскладок разные и запоминаются порознь: сетке нужна ширина,
        столбцу высота. Не менять размер нельзя — в сетке при высоте столбца
        графики растянутся вдвое, а в столбце при ширине сетки половина уедет
        за нижний край.
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
        """Поменять график местами с соседним по порядку.

        Соседним **в окне**: отсоединённый через это перешагивается, иначе
        нажатие выглядело бы несработавшим — переставить-то переставили, но
        видимого порядка это не изменило.
        """
        order = self._visible_order()
        if index not in order:
            return
        place = order.index(index) + step
        if not 0 <= place < len(order):
            return
        other = order[place]
        # Меняются местами графики, а не размеры: высота остаётся при своём
        # графике, ширины столбцов и высоты рядов — при сетке. Обмен размерами
        # вдобавок к обмену местами оставлял в памяти одно, на экране другое, и
        # следующая же смена раскладки раздавала графикам чужие размеры.
        here, there = self._order.index(index), self._order.index(other)
        self._order[here], self._order[there] = self._order[there], self._order[here]
        self._rebuild_layout()

    # --- данные ------------------------------------------------------------

    def refresh(self) -> None:
        """Пересобрать графики. Зовётся, когда изменились записи или модели.

        Без этого окно молча показывало бы вчерашнюю картинку: замер сняли,
        модель поехала, а на графике всё по-старому — и отличить свежее от
        несвежего на глаз нечем.

        Масштаб, спрятанные серии и выбранная точка при этом остаются: окно
        открывают, чтобы разглядывать участок, а сбор идёт шагами, и на каждом
        шаге картинка прыгала бы к полному виду.
        """
        for view, builder in zip(self.views, self._builders):
            view.set_chart(builder(), keep_view=True)
            view.set_unit(self._unit)
        self._refresh_stretch()
        self._refresh_detail()

    def _compact(self, view: ChartView) -> bool:
        """Занимает ли график ровно свою высоту, а не долю окна.

        Полоса разложения по вертикали ничего не откладывает: там слагаемые
        одной строкой. Растягивать нечего.
        """
        chart = view.chart()
        return chart is not None and chart.layout == LAYOUT_STACK

    def _even_sizes(self, total: int) -> list[int]:
        """Высоты «поровну»: компактным — их собственная, прочим — остаток.

        В сетке считать нечего: в разделителе лежат ряды, и ряды равны по
        определению.
        """
        if self._grid:
            count = self.splitter.count()
            if count < 1:
                return []
            # Заодно выровнять столбцы по первому ряду: «поровну» по вертикали
            # жмут, когда сетка расползлась, и оставить её кривой по
            # горизонтали значит не сделать того, о чём просили. По первому
            # ряду, а не поровну: ширины человек мог настроить нарочно, и
            # ровнять надо ряды между собой, а не всё к середине.
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
        """Лишнюю высоту окна забирают графики, а не полоса и лента.

        Своего минимума им мало: он мешает их **ужать**, а растянуть на треть
        окна разделитель волен и без спроса. Поэтому высота задаётся явно — но
        только пока она не та: дальше правка высот принадлежит человеку.
        """
        if self._grid:
            # В разделителе лежат ряды: они делят высоту поровну, и компактным
            # графикам внутри ряда это не мешает — у них свой минимум.
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
        """Обновить описание выбранной точки по свежим данным.

        По ключу, а не по сохранённому тексту: замер мог измениться, и строка
        рассказывала бы о вчерашних числах, ничем себя не выдавая. Точка
        исчезла — строка пустеет.
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

    # --- отсоединение ------------------------------------------------------

    def detached_indexes(self) -> list[int]:
        return sorted(self._windows)

    def toggle_detached(self, index: int) -> None:
        if index in self._windows:
            self.attach(index)
        else:
            self.detach(index)

    def detach(self, index: int, geometry: QByteArray | None = None) -> None:
        """Вынести график в своё окно, забрав у общего его высоту.

        Виджет переезжает целиком, поэтому масштаб, спрятанные серии и
        перекрестье остаются при нём. В списке `views` он остаётся на своём
        месте: обновление, единица измерения и общая ось X ходят по списку, а
        не по тому, кто где живёт.
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
            # Первый раз — по высоте самого графика, а не по высоте общего
            # окна: в общем он делил её с соседями, а тут занимает всё.
            window.resize(max(self.width(), 480), height + window.chrome_height)
        window.show()
        self._rebuild_layout()

        # Общее окно ужимается ровно на то, что из него ушло: иначе на месте
        # отсоединённого графика остаётся пустая полоса в треть экрана.
        if len(self._windows) == 1:
            self._height_before = self.height()
        if self._windows.keys() != set(range(len(self.views))):
            # Раскладку надо пересчитать до `resize`: пока она думает, что
            # график всё ещё в окне, её минимум держит прежнюю высоту, и окно
            # не ужимается вовсе.
            self.layout().activate()
            self.resize(
                self.width(),
                max(self.height() - height, self.minimumSizeHint().height()),
            )
            self._shrunk_to = self.height()

    def attach(self, index: int) -> None:
        """Вернуть график на своё место и вернуть окну его высоту."""
        window = self._windows.pop(index, None)
        if window is None:
            return
        # Размер отдельного окна запоминается: следующее отсоединение откроет
        # его таким же, а не заново подогнанным под общее окно.
        self._geometry[index] = window.saveGeometry()
        view = self.views[index]
        view.set_detached(False)
        # Спрашивать надо до вставки: вставленный график поднимает минимум
        # раскладки, и окно вырастает само — а нам надо знать, наша ли это
        # высота или человек потянул за край сам.
        ours = bool(self._shrunk_to) and self.height() == self._shrunk_to
        # На своё место, а не «в конец»: порядок графиков задан списком, и
        # вернувшийся встаёт туда, откуда уходил.
        self._rebuild_layout()
        self.layout().activate()
        if not self._windows and ours and self._height_before:
            # Вернулось всё, и высоту окна с тех пор никто не трогал: вернуть
            # надо ровно ту, что была до первого отсоединения. Считать её
            # сложением нельзя — ужать окно мешает минимум раскладки, а расти
            # обратно ничто не мешает, и за три круга окно уезжало на треть
            # экрана вниз.
            self.resize(self.width(), self._height_before)
            self._height_before = self._shrunk_to = 0
        window.returned.disconnect()
        window.close()
        window.deleteLater()

    def _remember_layout(self) -> None:
        """Снять размеры с той раскладки, которая стоит прямо сейчас.

        Делается перед каждой пересборкой, а не по сигналу разделителя:
        `splitterMoved` приходит только от мыши, и всё, что переставлено
        программно, до памяти не доезжало вовсе.
        """
        if self._built_grid:
            self._remember_grid()
        else:
            self._remember_sections()

    def _remember_grid(self) -> None:
        """Высоты рядов и общие ширины столбцов.

        Ширины берутся у первого полного ряда: они одни на все ряды, и в
        остальных лежит то же самое.
        """
        sizes = self.splitter.sizes()
        if sizes and all(sizes):
            self._rows = sizes
        for row in self._rows_in_grid():
            if row.count() == GRID_COLUMNS and all(row.sizes()):
                self._columns = row.sizes()
                return

    def _remember_sections(self) -> None:
        """Запомнить, какой высоты каждый график в общем окне.

        По номерам графиков, а не списком разделителя: в списке лежат только
        те, что сейчас в окне, и после возврата он разъехался бы.
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
            # Только что связали — подтянуть к общему масштабу сразу, а не
            # ждать следующего движения: иначе галочка выглядит несработавшей.
            for other in self.views:
                if other is not self.views[index] and self._is_synced(other):
                    self.views[index].apply_x(other.x_span())
                    return

    def _is_synced(self, view: ChartView) -> bool:
        index = self.views.index(view)
        return index not in self._windows or self._sync[index]

    def _is_tracked(self, view: ChartView) -> bool:
        """Связано ли перекрестье. У стоящего в общем окне — всегда."""
        index = self.views.index(view)
        return index not in self._windows or self._cross[index]

    def _synced_views(self) -> list[ChartView]:
        return [view for view in self.views if self._is_synced(view)]

    def _tracked_views(self) -> list[ChartView]:
        return [view for view in self.views if self._is_tracked(view)]

    # --- связь между графиками ---------------------------------------------

    def active_view(self) -> ChartView:
        """Тот график, на который смотрят. По умолчанию — верхний."""
        for view in self.views:
            if view.hasFocus() or view.underMouse():
                return view
        return self.views[0]

    def _on_range_changed(self) -> None:
        """Держать общую ось X, если графики её делят.

        Общая ось — не украшение: горб на остатках должен стоять ровно под
        своей ступенькой наклона, иначе два графика читаются порознь и связь
        между ними теряется.
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
        """Повторить перекрестье на соседях с общей осью.

        Одна вертикаль через оба графика — единственный способ увидеть, что
        горб остатков стоит ровно на том размере тома, где сменился наклон.
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

    # --- сохранение расположения -------------------------------------------

    def save_layout(self, settings) -> None:
        """Сложить в настройки геометрию окна и всё про отсоединённые.

        Именем ключа окна, а не номером: то же правило, что и для активной
        вкладки — от перестановки список номеров разъехался бы молча.
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
        # Проверка на состав, а не на длину: в сохранённом порядке могут
        # оказаться номера от прежнего числа графиков, и раскладка молча
        # потеряла бы один из них.
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

    def closeEvent(self, event) -> None:  # noqa: N802 — имя от Qt
        """Закрыли общее окно — отсоединённые уходят вместе с ним.

        Иначе они остались бы висеть без хозяина: обновляет их всё равно это
        окно, а показать его снова нечем — кнопка вкладки поднимет то же
        самое, уже открытое.
        """
        for window in self._windows.values():
            window.hide()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 — имя от Qt
        for window in self._windows.values():
            window.show()
        super().showEvent(event)
