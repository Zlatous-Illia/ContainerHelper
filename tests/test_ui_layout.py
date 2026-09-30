"""Window scrolling, table heights and free column widths.

A separate module, because it tests the behaviour of the shell, not the data.
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtCore import QPointF  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QHeaderView,
    QScrollArea,
)

_app = QApplication.instance() or QApplication([])

_settings_dir = tempfile.mkdtemp(prefix="containerhelper-settings-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from containerhelper.records import Record, Store  # noqa: E402
from containerhelper.ui.app import MainWindow  # noqa: E402
from tests.reference import HEADERS_AND_TAIL  # noqa: E402
from containerhelper.ui.table import TableGrip  # noqa: E402


def _wheel_event(global_y):
    """A mouse event with the wanted global coordinate — for the grip."""
    return QMouseEvent(
        QMouseEvent.MouseMove,
        QPointF(0, 0),
        QPointF(0, global_y),
        Qt.NoButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )


def seeded(path):
    Store(
        path=path,
        records=[
            Record(
                id=f"Test {gib} GiB",
                container_mib=gib * 1024,
                mounted_bytes=gib * 1024 * 1024 * 1024 - HEADERS_AND_TAIL,
                empty_free_bytes=gib * 1024 * 1024 * 1024 - HEADERS_AND_TAIL - gib * 3_000_000,
            )
            for gib in (1, 2, 4, 8, 16, 32, 64)
        ],
    ).save()


class LayoutFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "records.json"
        seeded(path)
        self.window = MainWindow(data_dir=Path(tempfile.mkdtemp()))
        self.window.records_tab.report_error = lambda *_: None
        self.window.records_tab.load_from(path)
        self.window.resize(820, 900)
        self.window.show()
        _app.processEvents()

    def tearDown(self):
        self.window.close()
        self._dir.cleanup()

    def tabs(self):
        return (self.window.calc_tab, self.window.records_tab, self.window.model_tab)

    def scroll_area_of(self, tab):
        widget = tab.parentWidget()
        while widget is not None and not isinstance(widget, QScrollArea):
            widget = widget.parentWidget()
        return widget


class ScrollTests(LayoutFixture):
    def test_every_tab_lives_in_a_scroll_area(self):
        for tab in self.tabs():
            with self.subTest(type(tab).__name__):
                self.assertIsNotNone(self.scroll_area_of(tab))

    def show_tab(self, tab):
        """Make the tab current: an invisible one is not laid out."""
        area = self.scroll_area_of(tab)
        self.tab_widget().setCurrentWidget(area)
        _app.processEvents()
        return area

    def tab_widget(self):
        from PySide6.QtWidgets import QTabWidget

        return self.window.centralWidget().findChild(QTabWidget)

    def test_a_short_window_scrolls_instead_of_squashing(self):
        for tab in self.tabs():
            with self.subTest(type(tab).__name__):
                area = self.show_tab(tab)
                self.window.resize(820, 250)
                _app.processEvents()
                self.assertGreater(area.verticalScrollBar().maximum(), 0)

    def test_content_never_shrinks_below_what_it_needs(self):
        """The point of scrolling is that the content is not squashed."""
        for tab in self.tabs():
            with self.subTest(type(tab).__name__):
                self.show_tab(tab)
                self.window.resize(820, 250)
                _app.processEvents()
                self.assertGreaterEqual(
                    tab.height(), tab.minimumSizeHint().height()
                )


class TableHeightTests(LayoutFixture):
    def test_every_table_has_its_own_grip(self):
        """A splitter shares a fixed height; a table must change its own."""
        for table in self.window.all_tables().values():
            with self.subTest(table.objectName()):
                self.assertTrue(
                    any(isinstance(child, TableGrip) for child in table.parent().children())
                )

    def test_the_grip_changes_the_table_height(self):
        table = self.window.model_tab.ntfs_table
        grip = next(
            child for child in table.parent().children() if isinstance(child, TableGrip)
        )
        before = table.minimumHeight()
        grip._origin = 100
        grip._start_height = before
        grip.mouseMoveEvent(_wheel_event(180))
        self.assertGreater(table.minimumHeight(), before)

    def test_the_grip_never_goes_below_the_floor(self):
        table = self.window.model_tab.ntfs_table
        grip = next(
            child for child in table.parent().children() if isinstance(child, TableGrip)
        )
        grip._origin = 400
        grip._start_height = table.minimumHeight()
        grip.mouseMoveEvent(_wheel_event(0))
        self.assertEqual(table.minimumHeight(), grip.floor_height())


class ColumnWidthTests(LayoutFixture):
    def header(self):
        return self.window.records_tab.table.horizontalHeader()

    def total_width(self):
        header = self.header()
        return sum(header.sectionSize(c) for c in range(header.count()))

    def test_no_column_is_pinned_to_the_window(self):
        """A stretched column ate the rest and would not let its edge move."""
        header = self.header()
        self.assertFalse(header.stretchLastSection())
        for column in range(header.count()):
            with self.subTest(column=column):
                self.assertEqual(
                    header.sectionResizeMode(column), QHeaderView.Interactive
                )

    def test_the_first_boundary_can_be_moved(self):
        header = self.header()
        header.resizeSection(0, 400)
        self.assertEqual(header.sectionSize(0), 400)

    def test_widening_a_column_does_not_steal_from_its_neighbour(self):
        header = self.header()
        neighbour = header.sectionSize(2)
        header.resizeSection(1, header.sectionSize(1) + 120)
        self.assertEqual(header.sectionSize(2), neighbour)

    def test_widening_a_column_widens_the_table(self):
        before = self.total_width()
        self.header().resizeSection(1, self.header().sectionSize(1) + 120)
        self.assertEqual(self.total_width() - before, 120)

    def test_columns_may_overflow_into_a_horizontal_scrollbar(self):
        header = self.header()
        for column in range(header.count()):
            header.resizeSection(column, 300)
        _app.processEvents()
        table = self.window.records_tab.table
        self.assertGreater(self.total_width(), table.viewport().width())
        self.assertGreater(table.horizontalScrollBar().maximum(), 0)

    def test_breakdown_columns_are_free_too(self):
        header = self.window.calc_tab.table.horizontalHeader()
        self.assertFalse(header.stretchLastSection())
        header.resizeSection(0, 350)
        self.assertEqual(header.sectionSize(0), 350)

    def test_breakdown_stays_unsorted(self):
        """The order of the breakdown rows is its content."""
        self.assertFalse(self.window.calc_tab.table.isSortingEnabled())


if __name__ == "__main__":
    unittest.main()
