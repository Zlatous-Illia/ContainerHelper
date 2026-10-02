"""Shared behaviour of tables, scrolling and input fields.

Lives on its own because every tab needs it, and there is no point in making
the tabs depend on each other for the sake of a few helpers.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRegularExpression, Qt, Signal
from PySide6.QtGui import (
    QCursor,
    QFontMetrics,
    QPainter,
    QPalette,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QComboBox,
    QLabel,
    QHeaderView,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..formatting import IGNORED_IN_INPUT
from ..i18n import tr

#: How many rows a table must always show. Fewer than two, and the header
#: runs over the first row: QTableWidget has no minimum of its own, it
#: honestly shrinks to zero together with its contents.
MIN_TABLE_ROWS = 2

#: Ceiling on the automatic height. With a hundred records a full-height
#: table would make the tab's scrolling endless.
MAX_AUTO_ROWS = 40

#: Below this a table stops being a table: the columns collapse into a mess.
MIN_TABLE_WIDTH = 320

#: Height of the strip by which the table is dragged vertically.
GRIP_HEIGHT = 7

#: What a size field accepts: digits and the digit-group separators that
#: `parse_bytes` throws away anyway. A letter, a minus or a comma in a bytes
#: field is not "a value that failed to parse" but a slip of the finger, and
#: it has to be caught on input. `parse_bytes` used to return None silently,
#: the field kept the typed garbage, and Container init turned into a dash —
#: without a single word about what exactly was wrong.
BYTES_PATTERN = "[0-9" + "".join(IGNORED_IN_INPUT) + "]*"

#: What the name and note fields accept: anything except control characters.
#: Those get in by pasting someone else's text, travel into JSON escaped, and
#: afterwards cannot be found by eye either in the file or in the table.
TEXT_PATTERN = r"[^\x00-\x1f\x7f]*"


class SortableItem(QTableWidgetItem):
    """A cell that sorts by value, not by the displayed text.

    Without this "9 000" would land after "10 000 000", and in Auto mode
    "100.00 GiB" would also land next to "1 023.75 MiB".
    """

    def __init__(self, text: str, key=None) -> None:
        super().__init__(text)
        self._key = key

    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine = self._key
        theirs = getattr(other, "_key", None)
        # Empty values go to the tail when sorting ascending: what matters is
        # the measurements taken, not the dashes. When descending, Qt reverses
        # the comparison and the dashes end up on top — pinning them to one
        # end in both directions takes a model of our own, and it is not worth
        # creating one for that.
        if mine is None:
            return False
        if theirs is None:
            return True
        return mine < theirs


def height_for_rows(table: QTableWidget, rows: int) -> int:
    """The height at which exactly this many rows and the whole header show.

    Computed from the widget's actual metrics, not tuned by a number: the
    theme and the font size change both the header and the row.
    """
    header = table.horizontalHeader().sizeHint().height()
    row = table.verticalHeader().defaultSectionSize()
    frame = 2 * table.frameWidth()
    # The horizontal scroll bar appears when the columns are wider than the
    # window, and eats the last row's height unless it is allowed for.
    scrollbar = table.horizontalScrollBar().sizeHint().height()
    return header + rows * row + frame + scrollbar


class TableGrip(QWidget):
    """The height grip: a strip under a table, dragged to change its height.

    A splitter will not do here: it divides a fixed height between its
    neighbours and cannot grow beyond it. The height of a tab inside a scroll
    area is set by the minimum of its contents, so it is the table's
    minimumHeight that has to be dragged — then the tab grows taller than the
    window and the scrolling gets longer.
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
        self.retranslate()

    def retranslate(self) -> None:
        # Nothing about dragging: the cursor itself says that. The double
        # click has to be mentioned — there is nowhere else to learn of it.
        self.setToolTip(tr("table.grip.tip"))

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

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
        """A double click returns the table to its minimum height."""
        apply_table_height(self._table, expand=False)
        self.resized.emit()
        event.accept()


def with_grip(table: QTableWidget) -> QWidget:
    """Wrap the table together with its height grip."""
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
    """Sorting on click, free column widths, an honest minimum height.

    No column stretches to the window width. A stretched column eats all the
    remaining space, and then the table width is locked to the window width:
    dragging the edge of one column, the user moves the neighbouring one, and
    the edge of the stretched one does not move at all. Here the sum of the
    columns lives its own life, and if it spills past the window, a
    horizontal scroll bar appears.
    """
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setStretchLastSection(False)
    header.setSectionsClickable(True)
    # Otherwise dragging one edge pulls the neighbouring sections along.
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
    """Three clicks on a header: ascending, descending, unsorted.

    Qt on its own knows only the first two and cycles through them. The third
    state is the original row order, the same as at startup: the records lie
    in the order they were taken, and that is meaningful in itself.
    """
    header = table.horizontalHeader()

    def on_clicked(section: int) -> None:
        state = table.property("sort_state") or {}
        previous_section = state.get("section")
        clicks = state.get("clicks", 0) + 1 if previous_section == section else 1

        if clicks >= 3:
            header.setSortIndicator(-1, Qt.AscendingOrder)
            table.setProperty("sort_state", {})
            # Clearing the indicator is not enough: the rows stay in the last
            # order. Only the table's owner knows the original one — so the
            # owner refills the table.
            restore = getattr(table, "restore_order", None)
            if callable(restore):
                restore()
            return
        table.setProperty("sort_state", {"section": section, "clicks": clicks})

    header.sectionClicked.connect(on_clicked)


def set_restore_order(table: QTableWidget, callback) -> None:
    """Who can bring back the original row order on the third click."""
    table.restore_order = callback


def sort_state(table: QTableWidget) -> tuple[int, int]:
    """Column and direction to save. -1 means no sorting."""
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
    """Switch the table between "minimum rows" and "full height"."""
    min_rows = table.property("min_rows") or MIN_TABLE_ROWS
    rows = max(min_rows, min(table.rowCount(), MAX_AUTO_ROWS)) if expand else min_rows
    height = height_for_rows(table, rows)
    table.setMinimumHeight(height)
    table.setMaximumHeight(height)


def table_height(table: QTableWidget) -> int:
    return table.minimumHeight()


def set_table_height(table: QTableWidget, height: int) -> None:
    """Restore the height dragged by hand last time."""
    rows = table.property("min_rows") or MIN_TABLE_ROWS
    height = max(height, height_for_rows(table, rows))
    table.setMinimumHeight(height)
    table.setMaximumHeight(height)


def set_header_tooltips(table: QTableWidget, tips) -> None:
    """A tooltip on every column header.

    Headers are short by necessity — otherwise the columns do not fit — and
    two words do not always make clear what exactly the column holds and
    where it comes from. Call after setHorizontalHeaderLabels: it creates the
    header items anew and wipes the tooltips along with them.
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
    """Fit the column widths to the contents — once, not for good.

    After that the width belongs to the user: resizeColumnsToContents on
    every refresh would wipe out what they dragged by hand.
    """
    table.resizeColumnsToContents()
    header = table.horizontalHeader()
    for column in range(header.count()):
        header.resizeSection(column, header.sectionSize(column) + padding)


def fit_widget_columns(table: QTableWidget, padding: int = 8) -> None:
    """Widen the columns to fit the widgets in their cells. Only widen.

    `resizeColumnsToContents` measures items, and a widget placed with
    `setCellWidget` does not exist for it at all: the column with the
    Re-measure and Use factory buttons came out at 97 px instead of the 284
    needed, and both buttons showed three letters each.

    Only upward, and on every refresh: a width dragged by hand must not be
    narrowed, while one saved by an earlier version may be smaller than the
    button — then it has to be corrected without asking anything.
    """
    header = table.horizontalHeader()
    for column in range(header.count()):
        widest = 0
        for row in range(table.rowCount()):
            widget = table.cellWidget(row, column)
            if widget is not None:
                widest = max(widest, widget.sizeHint().width())
        if widest:
            header.resizeSection(column, max(header.sectionSize(column), widest + padding))


def column_widths(table: QTableWidget) -> list[int]:
    header = table.horizontalHeader()
    return [header.sectionSize(index) for index in range(header.count())]


def set_column_widths(table: QTableWidget, widths: list[int]) -> bool:
    """Restore the saved widths.

    False: they do not match the table, so fit the columns anew.
    """
    header = table.horizontalHeader()
    if len(widths) != header.count() or not all(width > 0 for width in widths):
        return False
    for index, width in enumerate(widths):
        header.resizeSection(index, width)
    return True


def wrapped(label: QLabel) -> QLabel:
    """A wrapped label: wraps at word boundaries and gets the height for it.

    `setWordWrap` alone is not enough: QLabel's default size policy does not
    tell the layout that the height depends on the width, and the layout
    gives the label a single line. Inside a scroll area this shows at once —
    the second line is simply not there, the text breaks off in the middle.
    """
    label.setWordWrap(True)
    policy = label.sizePolicy()
    policy.setHeightForWidth(True)
    label.setSizePolicy(policy)
    return label


def digits_only(field: QLineEdit | QComboBox) -> None:
    """Allow only digits and digit-group separators in the field.

    By a validator, not by a check on save: a field that refuses a letter
    explains the rule itself, at the very moment it is broken. A paste from
    the clipboard goes through the same validator as a whole — corrupted text
    will not get into the field that way either.
    """
    validator = QRegularExpressionValidator(
        QRegularExpression(BYTES_PATTERN), field
    )
    field.setValidator(validator)


def plain_text(field: QLineEdit, limit: int) -> None:
    """Free text without control characters and no longer than the limit."""
    field.setValidator(
        QRegularExpressionValidator(QRegularExpression(TEXT_PATTERN), field)
    )
    field.setMaxLength(limit)


def fit_field(field: QLineEdit, sample: str, padding: int = 24) -> None:
    """Field width for the longest valid value, not for the whole form.

    A cluster field half a screen wide lies about what can go into it:
    nothing larger than 65536 exists.
    """
    metrics = QFontMetrics(field.font())
    width = metrics.horizontalAdvance(sample) + padding
    field.setMaximumWidth(width)
    field.setMinimumWidth(min(width, 80))


def scrollable(content: QWidget, min_width: int = 0) -> QScrollArea:
    """Wrap a tab in a scroll area.

    When the window is shorter or narrower than the contents, the elements
    must slide out of view under the scrolling, not be squashed until they
    are unreadable.
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
