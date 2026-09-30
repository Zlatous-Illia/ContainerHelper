"""Portable data folder, factory data and three-state sorting."""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

from containerhelper.factory import factory_data, factory_volume  # noqa: E402
from containerhelper.model import MIB, VC_HEADER_BYTES  # noqa: E402
from containerhelper.paths import (  # noqa: E402
    CALIBRATION_NAME,
    DATA_ARGUMENT,
    DATA_DIR_NAME,
    RECORDS_NAME,
    SETTINGS_NAME,
    calibration_path,
    data_dir_from_arguments,
    default_data_dir,
    is_writable,
    program_dir,
    records_path,
    resolve_data_dir,
    settings_path,
)
from containerhelper.records import Record, Store  # noqa: E402
from containerhelper.ui.app import (  # noqa: E402
    ACTIVE_TAB_KEY,
    TAB_CALC,
    TAB_CALIBRATION,
    TAB_MODEL,
    TAB_RECORDS,
    MainWindow,
)
from containerhelper.ui.calibration_tab import RECOMMENDED_MIB  # noqa: E402


class PathTests(unittest.TestCase):
    def test_data_lives_beside_the_program(self):
        self.assertEqual(default_data_dir(), program_dir() / DATA_DIR_NAME)

    def test_every_file_lands_in_one_folder(self):
        """The folder travels whole — separate places would drift apart."""
        folder = Path(tempfile.mkdtemp())
        self.assertEqual(records_path(folder), folder / RECORDS_NAME)
        self.assertEqual(calibration_path(folder), folder / CALIBRATION_NAME)
        self.assertEqual(settings_path(folder), folder / SETTINGS_NAME)

    def test_the_store_finds_the_calibration_beside_the_records(self):
        """The measurements file is not chosen: always beside, same name."""
        folder = Path(tempfile.mkdtemp())
        store = Store(path=records_path(folder))
        self.assertEqual(store.calibration_path, calibration_path(folder))

    def test_argument_wins_over_everything(self):
        folder = tempfile.mkdtemp()
        found, origin = resolve_data_dir([DATA_ARGUMENT, folder])
        self.assertEqual(found, Path(folder))
        self.assertEqual(origin, DATA_ARGUMENT)

    def test_argument_also_accepts_the_equals_form(self):
        folder = tempfile.mkdtemp()
        self.assertEqual(
            data_dir_from_arguments([f"{DATA_ARGUMENT}={folder}"]), Path(folder)
        )

    def test_no_argument_means_none(self):
        self.assertIsNone(data_dir_from_arguments(["--verbose"]))
        self.assertIsNone(data_dir_from_arguments([DATA_ARGUMENT]))

    def test_writability_is_checked_by_writing(self):
        """Permissions on network drives lie — check with a trial write."""
        self.assertTrue(is_writable(Path(tempfile.mkdtemp())))
        self.assertFalse(is_writable(Path("Z:/no/such/place/at/all")))


class FactoryDataTests(unittest.TestCase):
    def test_the_package_carries_measurements(self):
        data = factory_data()
        self.assertGreaterEqual(len(data.points), 12)
        self.assertTrue(data.source)

    def test_points_cover_one_to_a_hundred_gigabytes(self):
        sizes = {point.container_mib for point in factory_data().points}
        self.assertIn(1024, sizes)
        self.assertIn(102400, sizes)

    def test_every_point_has_a_plausible_overhead(self):
        for point in factory_data().points:
            with self.subTest(point.container_mib):
                self.assertGreater(point.metadata_bytes, MIB)
                self.assertLess(point.metadata_bytes, point.mounted_bytes // 20)

    def test_volume_matches_the_container(self):
        point = factory_data().points[0]
        self.assertEqual(factory_volume(point.container_mib), point.mounted_bytes)
        self.assertEqual(
            point.mounted_bytes, point.container_mib * MIB - VC_HEADER_BYTES
        )

    def test_every_factory_size_is_offered_in_the_table(self):
        offered = set(RECOMMENDED_MIB)
        for point in factory_data().points:
            with self.subTest(point.container_mib):
                self.assertIn(point.container_mib, offered)


class WindowFixture(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp())
        self.window = MainWindow(data_dir=self.data_dir)
        self.window.records_tab.report_error = lambda *_: None
        self.window.show()
        _app.processEvents()

    def tearDown(self):
        self.window.close()


class PreferenceTests(WindowFixture):
    def test_settings_land_next_to_the_data(self):
        self.window._store_preferences()
        self.assertTrue(settings_path(self.data_dir).exists())

    def test_column_widths_come_back(self):
        table = self.window.records_tab.table
        table.horizontalHeader().resizeSection(2, 271)
        self.window._store_preferences()

        again = MainWindow(data_dir=self.data_dir)
        self.assertEqual(again.records_tab.table.horizontalHeader().sectionSize(2), 271)
        again.close()

    def test_table_height_comes_back(self):
        table = self.window.model_tab.ntfs_table
        from containerhelper.ui.table import set_table_height

        set_table_height(table, 333)
        self.window._store_preferences()

        again = MainWindow(data_dir=self.data_dir)
        self.assertEqual(again.model_tab.ntfs_table.minimumHeight(), 333)
        again.close()

    def test_unit_and_toggles_come_back(self):
        self.window.unit_combo.setCurrentIndex(self.window.unit_combo.findData("GiB"))
        self.window.expand_tables.setChecked(True)
        self.window._store_preferences()

        again = MainWindow(data_dir=self.data_dir)
        self.assertEqual(again.unit_combo.currentData(), "GiB")
        self.assertTrue(again.expand_tables.isChecked())
        again.close()

    def test_the_picker_settings_come_back(self):
        """Set in the dialog, kept by the window: the dialog lives one show."""
        state = self.window.calc_tab.picker_state
        state.show_hidden = True
        state.remember_dir = False
        state.directory = str(self.data_dir)
        state.width, state.height = 700, 480
        self.window._store_preferences()

        again = MainWindow(data_dir=self.data_dir)
        restored = again.calc_tab.picker_state
        self.assertTrue(restored.show_hidden)
        self.assertFalse(restored.remember_dir)
        self.assertEqual(restored.directory, str(self.data_dir))
        self.assertEqual((restored.width, restored.height), (700, 480))
        again.close()

    def test_active_tab_comes_back(self):
        self.window._select_tab(TAB_CALIBRATION)
        self.window._store_preferences()
        again = MainWindow(data_dir=self.data_dir)
        self.assertEqual(again.current_tab_title(), TAB_CALIBRATION)
        again.close()

    def test_the_tab_is_stored_by_name_not_by_number(self):
        """Reordering the tabs changes the number, not the name."""
        self.window._select_tab(TAB_CALIBRATION)
        self.window._store_preferences()
        settings = QSettings(str(settings_path(self.data_dir)), QSettings.IniFormat)
        self.assertEqual(settings.value(ACTIVE_TAB_KEY, type=str), TAB_CALIBRATION)

    def test_an_old_numeric_setting_falls_back_to_the_first_tab(self):
        """Earlier versions stored the number; "2" is not a tab title."""
        settings = QSettings(str(settings_path(self.data_dir)), QSettings.IniFormat)
        settings.setValue(ACTIVE_TAB_KEY, 2)
        settings.sync()
        again = MainWindow(data_dir=self.data_dir)
        self.assertEqual(again.current_tab_title(), TAB_CALC)
        again.close()

    def test_an_unknown_tab_name_falls_back_too(self):
        settings = QSettings(str(settings_path(self.data_dir)), QSettings.IniFormat)
        settings.setValue(ACTIVE_TAB_KEY, "Вкладка, которой нет")
        settings.sync()
        again = MainWindow(data_dir=self.data_dir)
        self.assertEqual(again.current_tab_title(), TAB_CALC)
        again.close()

    def test_nothing_is_written_outside_the_folder(self):
        self.window._store_preferences()
        written = {item.name for item in self.data_dir.iterdir()}
        self.assertIn(SETTINGS_NAME, written)


class RememberTabTests(unittest.TestCase):
    """The Remember tab check box and its unchecked state."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def opened(self) -> MainWindow:
        return MainWindow(data_dir=self.data_dir)

    def closed_on(self, title: str, remember: bool = True) -> None:
        window = self.opened()
        window.remember_tab.setChecked(remember)
        window._select_tab(title)
        window._store_preferences()
        window.close()

    def test_remembering_is_on_by_default(self):
        window = self.opened()
        self.assertTrue(window.remember_tab.isChecked())
        window.close()

    def test_switching_it_off_opens_the_calculation_tab(self):
        self.closed_on(TAB_CALIBRATION, remember=False)
        again = self.opened()
        self.assertEqual(again.current_tab_title(), TAB_CALC)
        again.close()

    def test_the_switch_itself_survives_a_restart(self):
        self.closed_on(TAB_CALIBRATION, remember=False)
        again = self.opened()
        self.assertFalse(again.remember_tab.isChecked())
        again.close()

    def test_switched_off_it_does_not_overwrite_the_stored_tab(self):
        """Else the remembered tab would be lost silently, without asking."""
        self.closed_on(TAB_CALIBRATION, remember=True)
        off = self.opened()
        off.remember_tab.setChecked(False)
        off._store_preferences()
        off.close()

        settings = QSettings(str(settings_path(self.data_dir)), QSettings.IniFormat)
        self.assertEqual(settings.value(ACTIVE_TAB_KEY, type=str), TAB_CALIBRATION)

    def test_switching_it_back_on_restores_remembering(self):
        self.closed_on(TAB_MODEL, remember=True)
        self.closed_on(TAB_MODEL, remember=False)
        back = self.opened()
        back.remember_tab.setChecked(True)
        back._select_tab(TAB_RECORDS)
        back._store_preferences()
        back.close()

        again = self.opened()
        self.assertEqual(again.current_tab_title(), TAB_RECORDS)
        again.close()


class SortCycleTests(unittest.TestCase):
    """Three clicks on a header: ascending, descending, original order."""

    def setUp(self):
        from containerhelper.ui.records_tab import RecordsTab

        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "records.json"
        Store(
            path=path,
            records=[
                Record(id="Zeta", container_mib=102400,
                       mounted_bytes=107_373_916_160,
                       empty_free_bytes=107_273_248_768),
                Record(id="alpha", container_mib=1024,
                       mounted_bytes=1_073_475_584,
                       empty_free_bytes=1_055_596_544),
                Record(id="Mid", container_mib=8192,
                       mounted_bytes=8_589_668_352,
                       empty_free_bytes=8_555_397_120),
            ],
        ).save()
        self.tab = RecordsTab()
        self.tab.load_from(path)
        self.tab.show()
        _app.processEvents()
        self.header = self.tab.table.horizontalHeader()

    def tearDown(self):
        self._dir.cleanup()

    def names(self):
        return [
            self.tab.table.item(row, 0).text()
            for row in range(self.tab.table.rowCount())
        ]

    def click(self, section):
        """A real header click: Qt sets the indicator and sorts by itself."""
        x = (
            self.header.sectionViewportPosition(section)
            + self.header.sectionSize(section) // 2
        )
        QTest.mouseClick(
            self.header.viewport(),
            Qt.LeftButton,
            Qt.NoModifier,
            QPoint(x, self.header.height() // 2),
        )
        _app.processEvents()

    def test_first_two_clicks_sort_both_ways(self):
        self.click(2)
        self.assertEqual(self.names(), ["alpha", "Mid", "Zeta"])
        self.assertEqual(self.header.sortIndicatorSection(), 2)
        self.assertEqual(self.header.sortIndicatorOrder(), Qt.AscendingOrder)
        self.click(2)
        self.assertEqual(self.names(), ["Zeta", "Mid", "alpha"])
        self.assertEqual(self.header.sortIndicatorSection(), 2)
        self.assertEqual(self.header.sortIndicatorOrder(), Qt.DescendingOrder)

    def test_third_click_returns_the_original_order(self):
        for _ in range(3):
            self.click(2)
        self.assertEqual(self.names(), ["Zeta", "alpha", "Mid"])
        self.assertEqual(self.header.sortIndicatorSection(), -1)

    def test_another_column_starts_its_own_count(self):
        self.click(2)
        self.click(2)
        self.click(0)
        self.assertEqual(self.header.sortIndicatorSection(), 0)

    def test_the_cycle_repeats(self):
        for _ in range(4):
            self.click(2)
        self.assertEqual(self.header.sortIndicatorSection(), 2)


if __name__ == "__main__":
    unittest.main()
