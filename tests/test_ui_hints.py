"""Interface tooltips and the 64-bit signal of the Calibration tab."""

import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

_settings_dir = tempfile.mkdtemp(prefix="containerhelper-settings-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from containerhelper.formatting import UNIT_GIB  # noqa: E402
from containerhelper.model import MIB, DEFAULT_CLUSTER_BYTES  # noqa: E402
from containerhelper.model import volume_of as model_volume_of  # noqa: E402
from containerhelper.records import Record, Store  # noqa: E402
from containerhelper.ui.app import MainWindow  # noqa: E402
from containerhelper.ui.measure_dialog import MeasureDialog  # noqa: E402
from containerhelper.ui.record_dialog import RecordDialog  # noqa: E402

#: Sizes that have a measurement. 4 GiB and above do not fit a 32-bit int —
#: exactly where the Use factory and Restore own buttons stopped working.
SIZES_GIB = (1, 2, 4, 8, 16, 32, 100)


def volume_of(gib: int) -> int:
    return model_volume_of(gib * 1024 * MIB)


def seeded(path: Path) -> None:
    Store(
        path=path,
        calibration=[
            Record(
                id=f"Своя {gib} GiB",
                container_mib=gib * 1024,
                mounted_bytes=volume_of(gib) - DEFAULT_CLUSTER_BYTES,
                empty_free_bytes=volume_of(gib) - 20 * MIB,
            )
            for gib in SIZES_GIB
        ],
    ).save()


class WindowFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"
        seeded(self.path)
        self.window = MainWindow(data_dir=Path(tempfile.mkdtemp()))
        self.window.records_tab.report_error = lambda *_: None
        self.window.records_tab.load_from(self.path)

    def tearDown(self):
        self.window.close()
        self._dir.cleanup()

    def disabled_at(self, gib: int):
        volume = volume_of(gib)
        for record in self.window.records_tab.store.calibration:
            if record.volume_bytes == volume:
                return record.disabled
        return None


class BigVolumeSignalTests(WindowFixture):
    """A volume size is a 64-bit value, and the signal must carry it whole.

    Qt's int is a four-byte C++ int. Everything from 4 GiB up overflowed in
    it, the truncated value matched no measurement, and the button silently
    did nothing.
    """

    def toggle(self, gib: int, disabled: bool) -> None:
        self.window.calibration_tab.disableRequested.emit(volume_of(gib), disabled)

    def test_every_size_can_be_switched_to_the_factory_value(self):
        for gib in SIZES_GIB:
            with self.subTest(gib=gib):
                self.toggle(gib, True)
                self.assertTrue(self.disabled_at(gib))

    def test_every_size_can_be_switched_back(self):
        for gib in SIZES_GIB:
            with self.subTest(gib=gib):
                self.toggle(gib, True)
                self.toggle(gib, False)
                self.assertFalse(self.disabled_at(gib))

    def test_the_volume_arrives_whole(self):
        seen = []
        self.window.calibration_tab.disableRequested.connect(
            lambda volume, _flag: seen.append(volume)
        )
        for gib in SIZES_GIB:
            self.toggle(gib, True)
        self.assertEqual(seen, [volume_of(gib) for gib in SIZES_GIB])

    def test_emitting_warns_about_nothing(self):
        """A shiboken overflow arrives as a warning, not as an exception."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for gib in SIZES_GIB:
                self.toggle(gib, True)
        self.assertEqual([str(w.message) for w in caught], [])

    def test_only_the_matching_record_moves(self):
        self.toggle(16, True)
        self.assertTrue(self.disabled_at(16))
        self.assertFalse(any(self.disabled_at(g) for g in SIZES_GIB if g != 16))

    def test_the_switch_reaches_the_file(self):
        self.toggle(32, True)
        again = Store.load(self.path)
        stored = [r for r in again.calibration if r.volume_bytes == volume_of(32)]
        self.assertTrue(stored[0].disabled)


class MeasureDialogTests(unittest.TestCase):
    def test_it_takes_the_caller_s_wording(self):
        """Called from two places, and they measure different things."""
        dialog = MeasureDialog(prompt="Смонтируйте пустой контейнер.")
        self.assertEqual(dialog.prompt.text(), "Смонтируйте пустой контейнер.")

    def test_without_wording_it_explains_both_cases(self):
        dialog = MeasureDialog()
        self.assertIn("Пустой том", dialog.prompt.text())

    def details(self, filesystem, cluster):
        """The dialog's text for one volume, read from substituted sizes."""
        module = "containerhelper.ui.measure_dialog"
        with (
            mock.patch(f"{module}.mounted_drives", return_value=["X:\\"]),
            mock.patch(f"{module}.volume_usage", return_value=(1024 * MIB, 512 * MIB)),
            mock.patch(f"{module}.volume_filesystem", return_value=filesystem),
            mock.patch(f"{module}.cluster_size", return_value=cluster),
        ):
            dialog = MeasureDialog()
        return dialog.details.text()

    def test_another_profile_is_named(self):
        """The measurement is kept, not rejected: it goes to its own profile."""
        text = self.details("exFAT", 32768)
        self.assertIn("профиль exFAT, 32 KiB", text)
        self.assertIn("NTFS, 4 KiB", text)

    def test_another_ntfs_cluster_is_another_profile(self):
        self.assertIn("профиль NTFS, 64 KiB", self.details("NTFS", 65536))

    def test_the_default_profile_has_no_warning(self):
        self.assertNotIn("профиль", self.details("NTFS", 4096))
        self.assertNotIn("профиль", self.details("", None))

    def test_the_record_dialog_can_open_it(self):
        """Measure volume and Measure left space pass their own wording."""
        dialog = RecordDialog()
        opened = []
        dialog._ask_volume = lambda prompt: opened.append(prompt)
        dialog._measure_empty()
        dialog._measure_left()
        self.assertEqual(len(opened), 2)
        self.assertNotEqual(opened[0], opened[1])


class HeaderTooltipTests(WindowFixture):
    """Each column has its own tooltip: headers are short by necessity."""

    def tips_of(self, table):
        return [
            table.horizontalHeaderItem(column).toolTip()
            if table.horizontalHeaderItem(column)
            else ""
            for column in range(table.columnCount())
        ]

    def test_every_column_of_every_table_explains_itself(self):
        for name, table in self.window.all_tables().items():
            with self.subTest(name):
                self.assertTrue(all(self.tips_of(table)))

    def test_they_survive_a_change_of_units(self):
        """setHorizontalHeaderLabels creates the header items anew."""
        self.window.unit_combo.setCurrentIndex(
            self.window.unit_combo.findData(UNIT_GIB.key)
        )
        for name, table in self.window.all_tables().items():
            with self.subTest(name):
                self.assertTrue(all(self.tips_of(table)))


class WidgetTooltipTests(WindowFixture):
    def test_every_tab_says_what_it_is_for(self):
        for index in range(self.window.tabs.count()):
            with self.subTest(self.window.tabs.tabText(index)):
                self.assertTrue(self.window.tabs.tabToolTip(index))

    def test_the_tables_themselves_carry_one(self):
        for name, table in self.window.all_tables().items():
            with self.subTest(name):
                self.assertTrue(table.toolTip())

    def test_the_calc_tab_buttons_carry_one(self):
        tab = self.window.calc_tab
        for name in ("pick_button", "add_button", "drop_button", "copy_button"):
            with self.subTest(name):
                self.assertTrue(getattr(tab, name).toolTip())

    def test_the_record_dialog_fields_carry_one(self):
        dialog = RecordDialog()
        for name in (
            "id_edit",
            "container_edit",
            "cluster_combo",
            "mounted_edit",
            "free_edit",
            "file_edit",
            "count_edit",
            "alloc_edit",
            "left_edit",
            "note_edit",
        ):
            with self.subTest(name):
                self.assertTrue(getattr(dialog, name).toolTip())

    def test_calibration_rows_name_the_volume_behind_them(self):
        """A measurement's key is the volume size; the row does not show it."""
        table = self.window.calibration_tab.table
        tooltip = table.item(0, 0).toolTip()
        self.assertIn("том", tooltip)
        self.assertIn("Состояние", tooltip)

    def test_the_breakdown_rows_say_where_the_number_comes_from(self):
        self.window.calc_tab.size_edit.setText("10941734967")
        self.window.calc_tab._on_manual_edit()
        table = self.window.calc_tab.table
        tips = [table.item(row, 0).toolTip() for row in range(table.rowCount())]
        self.assertTrue(all(tips))


if __name__ == "__main__":
    unittest.main()
