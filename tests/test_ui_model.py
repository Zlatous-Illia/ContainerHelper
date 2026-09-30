"""Tests of the Model tab and of the safety margin link between the tabs."""

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

from containerhelper.model import CopySlackModel, MetadataModel  # noqa: E402
from containerhelper.records import Store, build_models  # noqa: E402
from containerhelper.ui.app import MainWindow  # noqa: E402
from containerhelper.ui.model_tab import ModelTab  # noqa: E402
from tests import reference  # noqa: E402


def tab_with(records):
    models = build_models(records) if records else (MetadataModel(), CopySlackModel())
    return ModelTab(lambda: models, lambda: records)


class ModelTabTests(unittest.TestCase):
    def test_empty_state_says_the_defaults_are_in_use(self):
        tab = tab_with([])
        self.assertIn("модель по умолчанию", tab.ntfs_state.text())
        self.assertIn("замеров нет", tab.slack_state.text())
        self.assertEqual(tab.ntfs_table.rowCount(), 0)
        self.assertIn("Записей для проверки пока нет", tab.ntfs_summary.text())

    def test_calibrated_state_reports_the_covered_range(self):
        tab = tab_with(reference.ALL)
        self.assertIn("3 точки", tab.ntfs_state.text())
        self.assertIn("7.9…10.9 GiB", tab.ntfs_state.text())

    def test_slack_state_admits_the_unverified_per_file_part(self):
        tab = tab_with(reference.ALL)
        self.assertIn("осталась предположением", tab.slack_state.text())

    def test_check_table_lists_every_usable_record(self):
        tab = tab_with(reference.ALL)
        self.assertEqual(tab.ntfs_table.rowCount(), 3)
        self.assertEqual(tab.slack_table.rowCount(), 2)

    def test_check_predictions_are_not_the_measurements(self):
        """Columns would match if the record were kept in the calibration."""
        tab = tab_with(reference.ALL)
        for row in range(tab.ntfs_table.rowCount()):
            measured = tab.ntfs_table.item(row, 1).text()
            predicted = tab.ntfs_table.item(row, 2).text()
            with self.subTest(row=row):
                self.assertNotEqual(measured, predicted)

    def test_summary_reports_the_worst_shortfall(self):
        tab = tab_with(reference.ALL)
        self.assertTrue(
            "Наибольшая недооценка" in tab.ntfs_summary.text()
            or "нигде не занизила" in tab.ntfs_summary.text()
        )

    def test_safety_hint_reports_the_worst_shortfall_as_diagnostics(self):
        """The overall miss is for reference, not a requirement.

        The leave-one-out check measures the model without one point. Demanding
        a safety margin from it for every size means paying everywhere for the
        spot where the grid is sparsest — which is what we moved away from.
        """
        tab = tab_with(reference.ALL)
        tab.set_safety_mib(0)
        tab.refresh()
        text = tab.safety_hint.text()
        self.assertIn("проверке исключением", text)
        self.assertNotIn("не хватает", text)

    def test_safety_says_who_owns_the_value(self):
        tab = tab_with(reference.ALL)
        tab.set_auto_safety(False)
        self.assertIn("вручную", tab.safety_value.text())
        tab.set_auto_safety(True)
        self.assertIn("под каждый расчёт", tab.safety_value.text())

    def test_there_is_no_second_safety_control(self):
        """A duplicate field rolled itself back under auto, looking broken."""
        tab = tab_with(reference.ALL)
        self.assertFalse(hasattr(tab, "safety_spin"))

    def test_safety_value_follows_the_calculator(self):
        tab = tab_with(reference.ALL)
        tab.set_safety_mib(17)
        self.assertIn("17", tab.safety_value.text())


class SafetyLinkTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"
        Store(path=self.path, records=list(reference.ALL)).save()
        self.window = MainWindow(data_dir=Path(tempfile.mkdtemp()))
        self.window.records_tab.report_error = lambda *_: None
        self.window.records_tab.load_from(self.path)

    def tearDown(self):
        self._dir.cleanup()

    def test_tabs_start_in_sync(self):
        self.assertIn(
            str(self.window.calc_tab.safety_spin.value()),
            self.window.model_tab.safety_value.text(),
        )

    def test_the_calculator_owns_the_value(self):
        """One owner, Calculation: the safety margin depends on size."""
        self.window.calc_tab.auto_safety.setChecked(False)
        self.window.calc_tab.safety_spin.setValue(23)
        self.assertIn("23", self.window.model_tab.safety_value.text())

    def test_calculator_drives_the_model_tab(self):
        self.window.calc_tab.auto_safety.setChecked(False)
        self.window.calc_tab.safety_spin.setValue(17)
        self.assertIn("17", self.window.model_tab.safety_value.text())

    def checked_names(self):
        table = self.window.model_tab.ntfs_table
        return {table.item(row, 0).text() for row in range(table.rowCount())}

    def test_records_refresh_the_model_tab(self):
        self.assertIn("Cache 4", self.checked_names())
        self.window.records_tab.store.records = [reference.CACHE_1]
        self.window.records_tab._save()
        self.assertNotIn("Cache 4", self.checked_names())
        self.assertIn("Cache 1", self.checked_names())

    def test_the_check_covers_the_calibration_points_too(self):
        """They hold the NTFS curve up — the report may not omit them."""
        self.assertTrue(
            any(name.startswith("Заводская") for name in self.checked_names())
        )

    def test_the_model_is_calibrated_out_of_the_box(self):
        """Factory points let a new copy calculate sensibly from the start."""
        self.window.records_tab.store.records = []
        self.window.records_tab._save()
        ntfs = self.window.models()[0]
        self.assertTrue(ntfs.calibrated)
        self.assertNotIn("модель по умолчанию", self.window.model_tab.ntfs_state.text())


if __name__ == "__main__":
    unittest.main()
