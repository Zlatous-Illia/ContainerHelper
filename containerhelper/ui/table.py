"""Общее поведение таблиц, прокрутки и ширины полей ввода.

Живёт отдельно, потому что нужно всем вкладкам, а заводить между ними
зависимость ради нескольких помощников не за чем.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCursor, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QHeaderView,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

#: Сколько строк таблица обязана показывать всегда. Меньше двух — и шапка
#: наезжает на первую строку: у QTableWidget нет своего минимума, он честно
#: ужимается до нуля вместе с содержимым.
MIN_TABLE_ROWS = 2

#: Потолок на автоматическую высоту. На сотне записей таблица во всю высоту
#: сделала бы прокрутку вкладки бесконечной.
MAX_AUTO_ROWS = 40

#: Ниже этого таблица перестаёт быть таблицей: столбцы схлопываются в кашу.
MIN_TABLE_WIDTH = 320

#: Высота полоски, за которую таблицу тянут по вертикали.
GRIP_HEIGHT = 7


class SortableItem(QTableWidgetItem):
    """Ячейка, которая сортируется по значению, а не по показанному тексту.

    Без этого «9 000» вставало бы после «10 000 000», а в режиме «Авто» —
    ещё и «100.00 GiB» рядом с «1 023.75 MiB».
    """

    def __init__(self, text: str, key=None) -> None:
        super().__init__(text)
        self._key = key

    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine = self._key
        theirs = getattr(other, "_key", None)
        # Незаполненные величины уходят в хвост при сортировке по возрастанию:
        # интерес представляют снятые замеры, а не прочерки. При убывании Qt
        # разворачивает сравнение, и прочерки оказываются сверху — пришпилить
        # их к одному краю в обоих направлениях можно только своей моделью,
        # а ради этого её заводить не за чем.
        if mine is None:
            return False
        if theirs is None:
            return True
        return mine < theirs


def height_for_rows(table: QTableWidget, rows: int) -> int:
    """Высота, при которой видно ровно столько строк и шапка целиком.

    Считается по фактическим метрикам виджета, а не подбирается числом: тема
    оформления и размер шрифта меняют и шапку, и строку.
    """
    header = table.horizontalHeader().sizeHint().height()
    row = table.verticalHeader().defaultSectionSize()
    frame = 2 * table.frameWidth()
    # Горизонтальная полоса появляется, когда столбцы шире окна, и съедает
    # высоту у последней строки, если её не заложить.
    scrollbar = table.horizontalScrollBar().sizeHint().height()
    return header + rows * row + frame + scrollbar


class TableGrip(QWidget):
    """Полоска под таблицей, за которую её тянут по высоте.

    Разделитель для этого не годится: он делит фиксированную высоту между
    соседями и вырасти за неё не может. Высоту вкладки внутри прокрутки
    задаёт минимум её содержимого, поэтому тянуть надо именно minimumHeight
    таблицы — тогда вкладка становится выше окна и прокрутка удлиняется.
    """

    resized = Signal()

    def __init__(self, table: QTableWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._table = table
        self._origin = 0
        self._start_height = 0
        self.setFixedHeight(GRIP_HEIGHT)
        self.setCursor(QCursor(Qt.SizeVerCursor))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setToolTip("Потяните, чтобы изменить высоту таблицы")

    def floor_height(self) -> int:
        rows = self._table.property("min_rows") or MIN_TABLE_ROWS
        return height_for_rows(self._table, rows)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        colour = self.palette().color(QPalette.Mid)
        middle = self.height() // 2
        painter.setPen(colour)
        painter.drawLine(2, middle - 1, self.width() - 3, middle - 1)
        painter.drawLine(2, middle + 1, self.width() - 3, middle + 1)

    def mousePressEvent(self, event) -> None:
        self._origin = event.globalPosition().toPoint().y()
        self._start_height = self._table.height()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if not self._origin:
            return
        delta = event.globalPosition().toPoint().y() - self._origin
        wanted = max(self.floor_height(), self._start_height + delta)
        self._table.setMinimumHeight(wanted)
        self._table.setMaximumHeight(wanted)
        self.resized.emit()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._origin = 0
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        """Двойной щелчок возвращает таблицу к минимальной высоте."""
        apply_table_height(self._table, expand=False)
        self.resized.emit()
        event.accept()


def with_grip(table: QTableWidget) -> QWidget:
    """Обернуть таблицу вместе с ручкой изменения высоты."""
    box = QWidget()
    column = QVBoxLayout(box)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(0)
    column.addWidget(table)
    grip = TableGrip(table, box)
    column.addWidget(grip)
    box.grip = grip
    return box


def setup_table(
    table: QTableWidget,
    sortable: bool = True,
    min_rows: int = MIN_TABLE_ROWS,
) -> None:
    """Сортировка по клику, свободная ширина столбцов, честный минимум высоты.

    Ни один столбец не растягивается по ширине окна. Растянутый столбец
    съедает весь остаток места, и тогда ширина таблицы намертво равна ширине
    окна: потянув за край одного столбца, пользователь двигает соседний, а
    край растянутого не двигается вообще. Здесь сумма столбцов живёт своей
    жизнью, а если она вылезла за окно — появляется горизонтальная прокрутка.
    """
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setStretchLastSection(False)
    header.setSectionsClickable(True)
    # Иначе перетаскивание одного края тянет за собой соседние секции.
    header.setCascadingSectionResizes(False)
    header.setMinimumSectionSize(40)

    table.setHorizontalScrollMode(QTableWidget.ScrollPerPixel)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    table.setSizeAdjustPolicy(QAbstractScrollArea.AdjustIgnored)

    if sortable:
        table.setSortingEnabled(True)
        header.setSortIndicator(-1, Qt.AscendingOrder)
        _install_sort_cycle(table)
    else:
        table.setSortingEnabled(False)

    table.setProperty("min_rows", min_rows)
    table.setMinimumWidth(MIN_TABLE_WIDTH)
    apply_table_height(table, expand=False)
    table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)


def _install_sort_cycle(table: QTableWidget) -> None:
    """Три щелчка по заголовку: по возрастанию, по убыванию, без сортировки.

    Qt сам умеет только первые два и гоняет их по кругу. Третье состояние —
    исходный порядок строк, тот же, что при запуске: записи лежат в порядке,
    в котором их снимали, и это осмысленно само по себе.
    """
    header = table.horizontalHeader()

    def on_clicked(section: int) -> None:
        state = table.property("sort_state") or {}
        previous_section = state.get("section")
        clicks = state.get("clicks", 0) + 1 if previous_section == section else 1

        if clicks >= 3:
            header.setSortIndicator(-1, Qt.AscendingOrder)
            table.setProperty("sort_state", {})
            # Снять индикатор мало: строки остались в последнем порядке.
            # Исходный знает только владелец таблицы — он и перезаполняет.
            restore = getattr(table, "restore_order", None)
            if callable(restore):
                restore()
            return
        table.setProperty("sort_state", {"section": section, "clicks": clicks})

    header.sectionClicked.connect(on_clicked)


def set_restore_order(table: QTableWidget, callback) -> None:
    """Кто умеет вернуть исходный порядок строк на третьем щелчке."""
    table.restore_order = callback


def sort_state(table: QTableWidget) -> tuple[int, int]:
    """Столбец и направление для сохранения. -1 — сортировки нет."""
    header = table.horizontalHeader()
    section = header.sortIndicatorSection()
    if not table.isSortingEnabled() or section < 0:
        return -1, 0
    state = table.property("sort_state") or {}
    if not state:
        return -1, 0
    return section, int(header.sortIndicatorOrder().value)


def restore_sort(table: QTableWidget, section: int, order: int) -> None:
    if section < 0 or section >= table.columnCount():
        return
    direction = Qt.AscendingOrder if order == 0 else Qt.DescendingOrder
    table.setProperty("sort_state", {"section": section, "clicks": 1 if order == 0 else 2})
    table.sortItems(section, direction)


def apply_table_height(table: QTableWidget, expand: bool) -> None:
    """Переключить таблицу между «минимум строк» и «во всю высоту»."""
    min_rows = table.property("min_rows") or MIN_TABLE_ROWS
    rows = max(min_rows, min(table.rowCount(), MAX_AUTO_ROWS)) if expand else min_rows
    height = height_for_rows(table, rows)
    table.setMinimumHeight(height)
    table.setMaximumHeight(height)


def table_height(table: QTableWidget) -> int:
    return table.minimumHeight()


def set_table_height(table: QTableWidget, height: int) -> None:
    """Вернуть высоту, натянутую руками в прошлый раз."""
    rows = table.property("min_rows") or MIN_TABLE_ROWS
    height = max(height, height_for_rows(table, rows))
    table.setMinimumHeight(height)
    table.setMaximumHeight(height)


def set_header_tooltips(table: QTableWidget, tips) -> None:
    """Подсказка на каждый заголовок столбца.

    Заголовки короткие по необходимости — иначе столбцы не влезают, — а из
    двух слов не всегда видно, что именно в столбце лежит и откуда оно
    берётся. Вызывать после setHorizontalHeaderLabels: тот заводит элементы
    заголовка заново и подсказки вместе с ними стирает.
    """
    for column, tip in enumerate(tips):
        if column >= table.columnCount():
            break
        item = table.horizontalHeaderItem(column)
        if item is None:
            item = QTableWidgetItem()
            table.setHorizontalHeaderItem(column, item)
        item.setToolTip(tip)


def fit_columns(table: QTableWidget, padding: int = 16) -> None:
    """Подогнать ширину столбцов под содержимое — один раз, не насовсем.

    Дальше ширина принадлежит пользователю: resizeColumnsToContents на каждом
    обновлении затирал бы то, что он натянул руками.
    """
    table.resizeColumnsToContents()
    header = table.horizontalHeader()
    for column in range(header.count()):
        header.resizeSection(column, header.sectionSize(column) + padding)


def column_widths(table: QTableWidget) -> list[int]:
    header = table.horizontalHeader()
    return [header.sectionSize(index) for index in range(header.count())]


def set_column_widths(table: QTableWidget, widths: list[int]) -> bool:
    """Вернуть сохранённые ширины. False — не подошли, надо подгонять заново."""
    header = table.horizontalHeader()
    if len(widths) != header.count() or not all(width > 0 for width in widths):
        return False
    for index, width in enumerate(widths):
        header.resizeSection(index, width)
    return True


def fit_field(field: QLineEdit, sample: str, padding: int = 24) -> None:
    """Ширина поля под самое длинное допустимое значение, не под всю форму.

    Поле кластера шириной в пол-экрана врёт о том, что туда можно вписать:
    больше 65536 не бывает.
    """
    metrics = QFontMetrics(field.font())
    width = metrics.horizontalAdvance(sample) + padding
    field.setMaximumWidth(width)
    field.setMinimumWidth(min(width, 80))


def scrollable(content: QWidget, min_width: int = 0) -> QScrollArea:
    """Обернуть вкладку прокруткой.

    Когда окно ниже или уже содержимого, элементы должны уезжать под
    прокрутку, а не сплющиваться до нечитаемого состояния.
    """
    if min_width:
        content.setMinimumWidth(min_width)
    area = QScrollArea()
    area.setWidget(content)
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    return area
