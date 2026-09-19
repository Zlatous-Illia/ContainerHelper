"""Sorting, column widths, display units and taking data from Calculation.

Everything tested here arrived with the table rework, so it lives in a
separate module rather than mixed into the tests of the Records tab itself.
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QWidget,
)

_app = QApplication.instance() or QApplication([])

_settings_dir = tempfile.mkdtemp(prefix="containerhelper-settings-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from containerhelper.formatting import UNIT_AUTO, UNIT_GIB  # noqa: E402
from containerhelper.model import Payload  # noqa: E402
from containerhelper.records import Record, Store  # noqa: E402
from containerhelper.ui.app import MainWindow  # noqa: E402
from containerhelper.ui.record_dialog import RecordDialog  # noqa: E402
from containerhelper.ui.records_tab import RecordsTab  # noqa: E402
from containerhelper.ui.table import fit_widget_columns  # noqa: E402

#: The names deliberately do not follow size order: otherwise sorting could
#: not be told apart from the original row order.
UNSORTED = (
    Record(
        id="Zeta",
        container_mib=102400,
        mounted_bytes=107_373_916_160,
        empty_free_bytes=107_273_248_768,
    ),
    Record(
        id="alpha",
        container_mib=1024,
        mounted_bytes=1_073_475_584,
        empty_free_bytes=1_055_596_544,
    ),
    Record(
        id="Mid",
        container_mib=8192,
        mounted_bytes=8_589_668_352,
        empty_free_bytes=8_555_397_120,
    ),
)


class TableFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"
        Store(path=self.path, records=list(UNSORTED)).save()
        self.tab = RecordsTab()
        self.tab.load_from(self.path)

    def tearDown(self):
        self._dir.cleanup()

    def names(self):
        return [
            self.tab.table.item(row, 0).text()
            for row in range(self.tab.table.rowCount())
        ]

    def column(self, index):
        return [
            self.tab.table.item(row, index).text()
            for row in range(self.tab.table.rowCount())
        ]


class SortingTests(TableFixture):
    def test_opens_in_the_order_records_were_taken(self):
        self.assertEqual(self.names(), ["Zeta", "alpha", "Mid"])

    def test_byte_columns_sort_by_value_not_by_text(self):
        self.tab.table.sortItems(2, Qt.AscendingOrder)
        self.assertEqual(self.names(), ["alpha", "Mid", "Zeta"])
        self.tab.table.sortItems(2, Qt.DescendingOrder)
        self.assertEqual(self.names(), ["Zeta", "Mid", "alpha"])

    def test_names_sort_case_insensitively(self):
        self.tab.table.sortItems(0, Qt.AscendingOrder)
        self.assertEqual(self.names(), ["alpha", "Mid", "Zeta"])

    def test_empty_cells_do_not_mix_into_the_numbers(self):
        self.tab.store.add(Record(id="bare", container_mib=2048))
        self.tab.refresh()
        self.tab.table.sortItems(2, Qt.AscendingOrder)
        self.assertEqual(self.names()[-1], "bare")

    def test_refresh_keeps_the_chosen_order(self):
        self.tab.table.sortItems(0, Qt.AscendingOrder)
        self.tab.refresh()
        self.assertEqual(self.names(), ["alpha", "Mid", "Zeta"])


class SelectionAfterSortingTests(TableFixture):
    """Row number and position in the store diverge — take the index."""

    def test_selected_index_points_at_the_right_record(self):
        self.tab.table.sortItems(2, Qt.AscendingOrder)
        self.tab.table.selectRow(0)
        self.assertEqual(self.tab._selected_index(), 1)
        self.assertEqual(self.tab.store.records[1].id, "alpha")

    def test_deleting_removes_the_record_that_was_selected(self):
        self.tab.table.sortItems(2, Qt.DescendingOrder)
        self.tab.table.selectRow(0)
        self.tab.confirm = lambda *_: True
        self.tab._delete()
        self.assertEqual([r.id for r in self.tab.store.records], ["alpha", "Mid"])

    def test_nothing_selected_yields_no_index(self):
        self.tab.table.clearSelection()
        self.assertIsNone(self.tab._selected_index())


class ColumnWidthTests(TableFixture):
    def test_every_column_can_be_dragged(self):
        header = self.tab.table.horizontalHeader()
        for column in range(header.count()):
            if column == 0:
                continue
            with self.subTest(column=column):
                self.assertEqual(
                    header.sectionResizeMode(column), QHeaderView.Interactive
                )

    def test_width_survives_a_refresh(self):
        self.tab.table.setColumnWidth(3, 231)
        self.tab.refresh()
        self.assertEqual(self.tab.table.columnWidth(3), 231)


class DisplayUnitTests(TableFixture):
    def test_bytes_are_the_default(self):
        self.assertEqual(
            self.tab.table.horizontalHeaderItem(2).text(), "Ёмкость тома, B"
        )
        self.assertIn("107 373 916 160", self.column(2))

    def test_switching_unit_rewrites_header_and_cells(self):
        self.tab.set_unit(UNIT_GIB)
        self.assertEqual(
            self.tab.table.horizontalHeaderItem(2).text(), "Ёмкость тома, GiB"
        )
        self.assertIn("100.000", self.column(2))

    def test_auto_names_the_unit_in_each_cell(self):
        """In Auto each row has its own unit, and the header cannot name it."""
        self.tab.set_unit(UNIT_AUTO)
        self.assertEqual(self.tab.table.horizontalHeaderItem(2).text(), "Ёмкость тома")
        self.assertTrue(all(cell.endswith(("MiB", "GiB")) for cell in self.column(2)))

    def test_container_init_is_not_a_byte_column(self):
        """VeraCrypt itself takes Container init in MiB: nothing to convert."""
        self.tab.set_unit(UNIT_GIB)
        self.assertEqual(
            self.tab.table.horizontalHeaderItem(1).text(), "Container init, MiB"
        )
        self.assertIn("102 400", self.column(1))

    def test_sorting_still_works_in_a_scaled_unit(self):
        self.tab.set_unit(UNIT_GIB)
        self.tab.table.sortItems(2, Qt.AscendingOrder)
        self.assertEqual(self.names(), ["alpha", "Mid", "Zeta"])


class PayloadHandoffTests(unittest.TestCase):
    """Carrying size and file count from the Calculation tab to a record."""

    def test_button_fills_the_measured_payload_fields(self):
        payload = Payload(
            logical_bytes=624_750,
            alloc_bytes=2_048_000,
            file_count=500,
            cluster_bytes=4096,
        )
        dialog = RecordDialog(payload_provider=lambda: payload)
        dialog._take_payload()
        record = dialog.build_record()
        self.assertEqual(record.file_bytes, 624_750)
        self.assertEqual(record.file_count, 500)
        self.assertEqual(record.file_alloc_bytes, 2_048_000)

    def test_measured_alloc_keeps_copy_slack_honest(self):
        """Without it, Σ ceil minus ceil Σ would end up in the slack."""
        payload = Payload(
            logical_bytes=624_750,
            alloc_bytes=2_048_000,
            file_count=500,
            cluster_bytes=4096,
        )
        dialog = RecordDialog(payload_provider=lambda: payload)
        dialog._take_payload()
        self.assertEqual(dialog.build_record().payload_alloc, 2_048_000)

    def test_cluster_size_comes_along(self):
        payload = Payload(
            logical_bytes=1000, alloc_bytes=65_536, file_count=1, cluster_bytes=65_536
        )
        dialog = RecordDialog(payload_provider=lambda: payload)
        dialog._take_payload()
        self.assertEqual(dialog.build_record().cluster_bytes, 65_536)

    def test_nothing_to_take_leaves_the_record_untouched(self):
        dialog = RecordDialog(payload_provider=lambda: None)
        dialog._take_payload()
        record = dialog.build_record()
        self.assertIsNone(record.file_bytes)
        self.assertIsNone(record.file_alloc_bytes)

    def test_button_is_off_without_a_provider(self):
        dialog = RecordDialog()
        self.assertFalse(dialog.payload_button.isEnabled())


class MainWindowWiringTests(unittest.TestCase):
    def setUp(self):
        self.window = MainWindow(data_dir=Path(tempfile.mkdtemp()))

    def test_records_tab_reads_the_calc_tab(self):
        self.window.calc_tab.size_edit.setText("10941734967")
        self.window.calc_tab.count_spin.setValue(1)
        self.window.calc_tab._on_manual_edit()
        dialog = RecordDialog(
            payload_provider=self.window.records_tab._payload_provider
        )
        dialog._take_payload()
        record = dialog.build_record()
        self.assertEqual(record.file_bytes, 10_941_734_967)
        self.assertEqual(record.file_alloc_bytes, 10_941_739_008)

    def test_unit_choice_reaches_every_tab(self):
        self.window.unit_combo.setCurrentIndex(self.window.unit_combo.findData("GiB"))
        self.assertEqual(
            self.window.records_tab.table.horizontalHeaderItem(2).text(),
            "Ёмкость тома, GiB",
        )
        self.assertEqual(
            self.window.model_tab.ntfs_table.horizontalHeaderItem(1).text(),
            "Измерено, GiB",
        )
        self.assertEqual(
            self.window.calc_tab.table.horizontalHeaderItem(2).text(), "GiB"
        )

    def test_container_init_never_leaves_mib(self):
        """This number goes into VeraCrypt as is; the unit cannot touch it."""
        self.window.calc_tab.size_edit.setText("10941734967")
        self.window.calc_tab._on_manual_edit()
        before = self.window.calc_tab.result_label.text()
        self.window.unit_combo.setCurrentIndex(self.window.unit_combo.findData("GiB"))
        self.assertEqual(self.window.calc_tab.result_label.text(), before)
        self.assertEqual(self.window.calc_tab.result_units.text(), "MiB")


class WidgetColumnTests(unittest.TestCase):
    """Fitting to contents does not measure buttons in cells at all.

    The column with Re-measure and Use factory came out at 97 px instead of
    the 284 it needed, and both buttons showed three letters each.
    """

    def setUp(self):
        self.table = QTableWidget(2, 2)
        self.table.setColumnWidth(1, 30)
        for row in range(2):
            box = QWidget()
            layout = QHBoxLayout(box)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(QPushButton("Достаточно длинная надпись"))
            self.table.setCellWidget(row, 1, box)

    def test_the_column_grows_to_the_widget(self):
        needed = self.table.cellWidget(0, 1).sizeHint().width()
        fit_widget_columns(self.table)
        self.assertGreaterEqual(self.table.columnWidth(1), needed)

    def test_a_wider_column_is_left_alone(self):
        """A width stretched by hand must not be narrowed."""
        self.table.setColumnWidth(1, 900)
        fit_widget_columns(self.table)
        self.assertEqual(self.table.columnWidth(1), 900)

    def test_columns_without_widgets_are_untouched(self):
        before = self.table.columnWidth(0)
        fit_widget_columns(self.table)
        self.assertEqual(self.table.columnWidth(0), before)


class CalibrationButtonWidthTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.window = MainWindow(data_dir=Path(self._dir.name))

    def tearDown(self):
        self.window.close()
        self._dir.cleanup()

    def test_the_row_buttons_are_not_squeezed(self):
        table = self.window.calibration_tab.table
        column = table.columnCount() - 1
        widget = table.cellWidget(0, column)
        self.assertIsNotNone(widget)
        self.assertGreaterEqual(table.columnWidth(column), widget.sizeHint().width())


if __name__ == "__main__":
    unittest.main()
