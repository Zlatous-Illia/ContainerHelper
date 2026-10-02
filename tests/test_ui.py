"""Tests of the Calculation tab. Qt starts in offscreen mode."""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from containerhelper.model import MIB, CopySlackModel, MetadataModel  # noqa: E402
from containerhelper.records import Store  # noqa: E402
from containerhelper.ui.calc_tab import CalcTab  # noqa: E402
from tests import reference  # noqa: E402

_app = QApplication.instance() or QApplication([])


def default_models():
    return MetadataModel(), CopySlackModel()


class CalcTabTests(unittest.TestCase):
    def setUp(self):
        self.tab = CalcTab(default_models)

    def _set_size(self, value: int, count: int = 1) -> None:
        self.tab.size_edit.setText(str(value))
        self.tab.count_spin.setValue(count)
        self.tab._on_manual_edit()

    def _result(self) -> int:
        return int(self.tab.result_label.text().replace(" ", ""))

    def test_matches_the_core_solver(self):
        self._set_size(reference.CACHE_2.file_bytes)
        self.assertEqual(self._result(), 10477)

    def test_empty_input_shows_no_result(self):
        self._set_size_text("")
        self.assertEqual(self.tab.result_label.text(), "—")
        self.assertEqual(self.tab.table.rowCount(), 0)

    def test_garbage_input_shows_no_result(self):
        self._set_size_text("не число")
        self.assertEqual(self.tab.result_label.text(), "—")

    def _set_size_text(self, text: str) -> None:
        self.tab.size_edit.setText(text)
        self.tab._on_manual_edit()

    def test_breakdown_sums_to_the_container(self):
        self._set_size(reference.CACHE_1.file_bytes)
        rows = {
            self.tab.table.item(row, 0).text().strip(): int(
                self.tab.table.item(row, 1).text().replace(" ", "")
            )
            for row in range(self.tab.table.rowCount())
        }
        total = (
            rows["Полезные данные (по кластерам)"]
            + rows["Заголовок VeraCrypt"]
            + rows["Метаданные NTFS"]
            + rows["Запас на копирование"]
            + rows["Ожидаемый остаток (Left space)"]
        )
        self.assertEqual(total, rows["Итого контейнер"])

    def test_safety_margin_changes_the_result(self):
        self._set_size(8 * 1024**3)
        with_default = self._result()
        self.tab.safety_spin.setValue(0)
        self.assertEqual(with_default - self._result(), 4)

    def test_more_files_never_shrinks_the_container(self):
        self._set_size(4 * 1024**3, count=1)
        one = self._result()
        self._set_size(4 * 1024**3, count=100_000)
        self.assertGreater(self._result(), one)

    def test_warns_about_manual_entry_of_multiple_files(self):
        self._set_size(4 * 1024**3, count=5000)
        self.assertIn("вручную для нескольких файлов", self.tab.notes_label.text())

    def test_warns_about_extrapolation_without_calibration(self):
        self._set_size(4 * 1024**3)
        self.assertIn("экстраполяцией", self.tab.notes_label.text())

    def test_copies_bare_number(self):
        self._set_size(reference.CACHE_2.file_bytes)
        self.tab._copy_result()
        self.assertEqual(QGuiApplication.clipboard().text(), "10477")

    def test_scanned_folder_recomputes_on_cluster_change(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for index in range(3):
                (root / f"f{index}.bin").write_bytes(b"\0" * 5000)
            self.tab._rescan([str(root)])
            self.assertEqual(self.tab.count_spin.value(), 3)

            self.tab.cluster_combo.setCurrentText("65536")
            payload = self.tab._payload()
            self.assertEqual(payload.alloc_bytes, 3 * 65536)
            self.assertEqual(payload.file_count, 3)

    def test_manual_edit_detaches_from_the_scan(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "a.bin").write_bytes(b"\0" * 5000)
            self.tab._rescan([str(root)])
            self.assertIsNotNone(self.tab._scan)
            self._set_size(1234)
            self.assertIsNone(self.tab._scan)
            self.assertEqual(self.tab._payload().logical_bytes, 1234)


class FieldValidationTests(unittest.TestCase):
    """The size field accepts only digits and digit group separators.

    A letter there is a slipped key, not "a value that failed to parse":
    parse_bytes silently returned None, the field kept the garbage, and
    Container init turned into a dash without a single word about why.
    """

    def setUp(self):
        self.tab = CalcTab(default_models)

    def typed(self, field, text):
        field.setText("")
        for symbol in text:
            field.insert(symbol)
        return field.text()

    def test_the_size_field_drops_everything_but_digits(self):
        self.assertEqual(self.typed(self.tab.size_edit, "10a9 4б1-"), "109 41")

    def test_a_pasted_number_with_separators_goes_through(self):
        self.tab.size_edit.setText("")
        self.tab.size_edit.insert("10 941 734 967")
        self.assertEqual(self.tab.size_edit.text(), "10 941 734 967")

    def test_a_pasted_number_with_a_unit_is_refused_whole(self):
        self.tab.size_edit.setText("")
        self.tab.size_edit.insert("10941734967 B")
        self.assertEqual(self.tab.size_edit.text(), "")

    def test_the_cluster_field_is_numeric_too(self):
        self.assertIsNotNone(self.tab.cluster_combo.validator())
        self.assertEqual(self.typed(self.tab.cluster_combo.lineEdit(), "40x96"), "4096")


class AutoSafetyTests(unittest.TestCase):
    def test_one_input_gives_one_answer(self):
        """547 MiB in 10 000 files, on the factory calibration.

        The advice there alternates between 5 and 4 MiB. Seeded with the
        field, which holds the previous answer, the tab showed 5 and 4 MiB in
        turn, and Container init moved with it on every recalculation.
        """
        with tempfile.TemporaryDirectory() as folder:
            store = Store(path=Path(folder) / "none.json")
            models, safety = store.models(), store.safety()
        tab = CalcTab(lambda: models, lambda: safety)
        tab.size_edit.setText(str(547 * MIB))
        tab.count_spin.setValue(10_000)
        tab._on_manual_edit()
        answers = set()
        for _ in range(3):
            tab.recalculate()
            answers.add((tab.result_label.text(), tab.safety_spin.value()))
        self.assertEqual(len(answers), 1, answers)
        self.assertEqual(tab.safety_spin.value(), 5)


if __name__ == "__main__":
    unittest.main()
