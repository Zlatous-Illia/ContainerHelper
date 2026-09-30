"""Shell: window minimums, table heights, coverage block, record buttons."""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

_settings_dir = tempfile.mkdtemp(prefix="containerhelper-settings-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from containerhelper.model import Payload  # noqa: E402
from containerhelper.records import Record, Store  # noqa: E402
from containerhelper.sizes import VolumeScan  # noqa: E402
from containerhelper.ui.app import MIN_WINDOW_WIDTH, MainWindow  # noqa: E402
from containerhelper.ui.calibration_tab import RECOMMENDED_MIB  # noqa: E402
from containerhelper.ui.record_dialog import RecordDialog  # noqa: E402
from containerhelper.ui.table import MIN_TABLE_ROWS, height_for_rows  # noqa: E402
from tests.reference import HEADERS_AND_TAIL  # noqa: E402


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
            for gib in (1, 2, 4, 8, 16, 32, 64, 100)
        ],
    ).save()


class WindowFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "records.json"
        seeded(path)
        self.window = MainWindow(data_dir=Path(tempfile.mkdtemp()))
        self.window.records_tab.report_error = lambda *_: None
        self.window.records_tab.load_from(path)
        self.window.show()
        _app.processEvents()

    def tearDown(self):
        self.window.close()
        self._dir.cleanup()

    def tables(self):
        return (
            self.window.calc_tab.table,
            self.window.records_tab.table,
            self.window.model_tab.ntfs_table,
            self.window.model_tab.slack_table,
        )


class MinimumSizeTests(WindowFixture):
    def test_window_has_a_floor_width(self):
        self.window.resize(400, 900)
        _app.processEvents()
        self.assertGreaterEqual(self.window.width(), MIN_WINDOW_WIDTH)

    def test_tables_have_a_floor_width(self):
        for table in self.tables():
            with self.subTest(table.objectName()):
                self.assertGreater(table.minimumWidth(), 0)

    def test_minimum_height_fits_header_and_rows(self):
        """Else the header covers the first row when squeezed to minimum."""
        for table in self.tables():
            with self.subTest(table.objectName()):
                header = table.horizontalHeader().sizeHint().height()
                row = table.verticalHeader().defaultSectionSize()
                self.assertGreaterEqual(
                    table.minimumHeight(), header + MIN_TABLE_ROWS * row
                )

    def test_squeezed_table_leaves_room_below_the_header(self):
        """At the minimum height the header must not fill the whole table."""
        table = self.window.model_tab.ntfs_table
        header = table.horizontalHeader().sizeHint().height()
        row = table.verticalHeader().defaultSectionSize()
        self.assertGreaterEqual(table.minimumHeight() - header, MIN_TABLE_ROWS * row)


class TableHeightTests(WindowFixture):
    def test_expanding_grows_the_minimum_height(self):
        table = self.window.model_tab.ntfs_table
        self.window.expand_tables.setChecked(False)
        compact = table.minimumHeight()
        self.window.expand_tables.setChecked(True)
        _app.processEvents()
        self.assertGreater(table.minimumHeight(), compact)
        self.assertGreaterEqual(
            table.minimumHeight(), height_for_rows(table, table.rowCount())
        )

    def test_expanding_lengthens_the_scroll(self):
        """A splitter cannot do this: it shares out the height already there.

        The tab is found by title, not by index: one reordering of the tabs
        changes the index, and the test then silently measures the wrong page.
        """
        from PySide6.QtWidgets import QTabWidget

        tabs = self.window.centralWidget().findChild(QTabWidget)
        titles = [tabs.tabText(i) for i in range(tabs.count())]
        tabs.setCurrentIndex(titles.index("Калибровка"))
        # Settings outlive tests within one process — set the state explicitly.
        self.window.expand_tables.setChecked(False)
        _app.processEvents()
        area = tabs.currentWidget()
        before = area.verticalScrollBar().maximum()
        self.window.expand_tables.setChecked(True)
        _app.processEvents()
        self.assertGreater(area.verticalScrollBar().maximum(), before)

    def test_collapsing_returns_the_compact_height(self):
        table = self.window.model_tab.ntfs_table
        self.window.expand_tables.setChecked(False)
        compact = table.minimumHeight()
        self.window.expand_tables.setChecked(True)
        self.window.expand_tables.setChecked(False)
        _app.processEvents()
        self.assertEqual(table.minimumHeight(), compact)

    def test_the_grip_sits_right_under_the_table(self):
        """The grip must hug the table's outline, not the text below it."""
        from containerhelper.ui.table import TableGrip

        for name, table in self.window.all_tables().items():
            with self.subTest(name):
                box = table.parent()
                items = [
                    box.layout().itemAt(i).widget()
                    for i in range(box.layout().count())
                ]
                self.assertIs(items[0], table)
                self.assertIsInstance(items[1], TableGrip)


class CoverageTests(WindowFixture):
    def table(self):
        return self.window.calibration_tab.table

    def test_coverage_lives_on_its_own_tab(self):
        titles = [self.window.tabs.tabText(i) for i in range(self.window.tabs.count())]
        self.assertIn("Калибровка", titles)

    def test_range_list_reaches_past_the_measured_data(self):
        """Extrapolating up is the model's weak spot: measure there."""
        self.assertGreater(len(RECOMMENDED_MIB), 15)
        self.assertGreaterEqual(max(RECOMMENDED_MIB), 1024 * 1024)
        self.assertLessEqual(min(RECOMMENDED_MIB), 512)

    def test_ranges_are_sorted_and_unique(self):
        self.assertEqual(list(RECOMMENDED_MIB), sorted(set(RECOMMENDED_MIB)))

    def test_every_range_is_listed(self):
        self.assertEqual(self.table().rowCount(), len(RECOMMENDED_MIB))

    def test_the_model_tab_no_longer_carries_coverage(self):
        self.assertFalse(hasattr(self.window.model_tab, "coverage_table"))


class RecordDialogButtonTests(WindowFixture):
    def dialog(self):
        return RecordDialog(
            payload_provider=self.window.records_tab._payload_provider,
            container_provider=self.window.records_tab._container_provider,
        )

    def test_three_actions_in_one_row(self):
        dialog = self.dialog()
        row = dialog.payload_button.parentWidget().layout()
        self.assertTrue(
            all(
                button.isEnabled() or button is dialog.payload_button
                for button in (
                    dialog.payload_button,
                    dialog.measure_button,
                    dialog.left_button,
                )
            )
        )
        self.assertEqual(dialog.left_button.text(), "Замерить остаток")

    def test_taking_from_the_calculator_brings_container_and_cluster(self):
        self.window.calc_tab.size_edit.setText("10941734967")
        self.window.calc_tab.cluster_combo.setCurrentText("8192")
        self.window.calc_tab._on_manual_edit()
        expected = self.window.calc_tab.current_container_mib()

        dialog = self.dialog()
        dialog._take_payload()
        record = dialog.build_record()
        self.assertEqual(record.container_mib, expected)
        self.assertEqual(record.cluster_bytes, 8192)
        self.assertEqual(record.file_bytes, 10_941_734_967)


class VerdictTests(WindowFixture):
    """Checking the outcome: the file count and the cluster-rounded payload."""

    def prepared(self, count, alloc):
        dialog = self.dialog_with(count, alloc)
        return dialog

    def dialog_with(self, count, alloc):
        dialog = RecordDialog()
        dialog.count_edit.setText(str(count))
        dialog.alloc_edit.setText(str(alloc))
        return dialog

    def scan(self, count, alloc, service=(), errors=()):
        return VolumeScan(
            payload=Payload(
                logical_bytes=alloc, alloc_bytes=alloc, file_count=count,
                cluster_bytes=4096,
            ),
            service_dirs=list(service),
            errors=list(errors),
        )

    def test_match_is_reported_plainly(self):
        dialog = self.dialog_with(500, 2_048_000)
        text = dialog._verdict("E:", self.scan(500, 2_048_000))
        self.assertIn("совпало", text)
        self.assertNotIn("⚠", text)

    def test_wrong_file_count_is_flagged(self):
        dialog = self.dialog_with(500, 2_048_000)
        text = dialog._verdict("E:", self.scan(499, 2_048_000))
        self.assertIn("⚠", text)
        self.assertIn("499", text)

    def test_wrong_volume_is_flagged(self):
        dialog = self.dialog_with(500, 2_048_000)
        text = dialog._verdict("E:", self.scan(500, 3_000_000))
        self.assertIn("⚠", text)

    def test_service_directories_are_named_not_counted(self):
        """They eat clusters but are not payload."""
        dialog = self.dialog_with(500, 2_048_000)
        text = dialog._verdict(
            "E:", self.scan(500, 2_048_000, service=["System Volume Information"])
        )
        self.assertIn("System Volume Information", text)
        self.assertNotIn("⚠", text)

    def test_nothing_to_compare_says_so(self):
        dialog = RecordDialog()
        text = dialog._verdict("E:", self.scan(500, 2_048_000))
        self.assertIn("Сверять не с чем", text)


if __name__ == "__main__":
    unittest.main()
