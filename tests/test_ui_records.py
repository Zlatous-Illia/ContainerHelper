"""Tests of the Records tab, the edit dialog and the link to Calculation."""

import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

# Settings are moved to a separate folder so that a test run does not touch
# the user's real registry.
_settings_dir = tempfile.mkdtemp(prefix="containerhelper-settings-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from containerhelper.model import Payload, solve_container_mib  # noqa: E402
from containerhelper.records import Record, Store  # noqa: E402
from containerhelper.ui.app import MainWindow  # noqa: E402
from containerhelper.ui.record_dialog import RecordDialog  # noqa: E402
from containerhelper.ui.records_tab import COLUMNS, RecordsTab  # noqa: E402
from tests import reference  # noqa: E402


def column_of(title: str) -> int:
    """The column index for a header title.

    By name, not by number: columns get added to the table, and a tie to the
    number breaks silently — the test keeps comparing, just the wrong thing.
    """
    for index, (name, _kind, _tip) in enumerate(COLUMNS):
        if name == title:
            return index
    raise AssertionError(f"нет столбца «{title}»")


def seeded_store(path: Path) -> Store:
    store = Store(path=path, records=list(reference.ALL))
    store.save()
    return store


class RecordsTabTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"
        seeded_store(self.path)
        self.tab = RecordsTab()
        self.errors: list[tuple[str, str]] = []
        self.tab.report_error = lambda title, text: self.errors.append((title, text))
        self.tab.load_from(self.path)

    def tearDown(self):
        self._dir.cleanup()

    def test_table_lists_every_record(self):
        self.assertEqual(self.tab.table.rowCount(), 3)
        names = [self.tab.table.item(row, 0).text() for row in range(3)]
        self.assertEqual(names, ["Cache 1", "Cache 2", "Cache 4"])

    def test_derived_ntfs_is_shown(self):
        for row in range(3):
            name = self.tab.table.item(row, 0).text()
            shown = int(self.tab.table.item(row, 3).text().replace(" ", ""))
            with self.subTest(name):
                self.assertEqual(shown, reference.EXPECTED_NTFS[name])

    def test_broken_record_is_marked(self):
        statuses = {
            self.tab.table.item(row, 0).text(): self.tab.table.item(
                row, column_of("Статус")
            ).text()
            for row in range(3)
        }
        self.assertEqual(statuses["Cache 1"], "ок")
        self.assertEqual(statuses["Cache 4"], "ок")
        self.assertEqual(statuses["Cache 2"], "ошибки")

    def test_deviation_is_measured_against_the_uncalibrated_baseline(self):
        """Else the column is all zero: the model is exact at its points."""
        values = [
            int(self.tab.table.item(row, 4).text().replace(" ", "")) for row in range(3)
        ]
        self.assertTrue(any(value != 0 for value in values))

    def test_summary_counts_calibration_inputs(self):
        text = self.tab.summary_label.text()
        self.assertIn("Точек для модели NTFS: 3", text)
        self.assertIn("Замеров запаса на копирование: 2", text)

    def test_missing_file_starts_empty(self):
        self.tab.load_from(Path(self._dir.name) / "absent.json")
        self.assertEqual(self.tab.table.rowCount(), 0)
        self.assertEqual(self.errors, [])
        self.assertIn("работает на значениях по умолчанию", self.tab.summary_label.text())

    def test_corrupt_file_reports_and_keeps_the_path(self):
        broken = Path(self._dir.name) / "broken.json"
        broken.write_text("{oops", encoding="utf-8")

        self.assertFalse(self.tab.load_from(broken))
        self.assertEqual(self.tab.store.path, broken)
        self.assertEqual(self.tab.table.rowCount(), 0)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("повреждён", self.errors[0][1])

    def test_unknown_schema_is_reported(self):
        alien = Path(self._dir.name) / "alien.json"
        alien.write_text('{"schema": 99, "records": []}', encoding="utf-8")
        self.assertFalse(self.tab.load_from(alien))
        self.assertIn("схемы", self.errors[0][1])

    def test_delete_asks_before_removing(self):
        self.tab.table.selectRow(0)
        self.tab.confirm = lambda *_: False
        self.tab._delete()
        self.assertEqual(len(self.tab.store.records), 3)

        self.tab.confirm = lambda *_: True
        self.tab._delete()
        self.assertEqual(len(self.tab.store.records), 2)
        self.assertEqual(self.tab.table.rowCount(), 2)

    def test_delete_without_selection_does_nothing(self):
        self.tab.table.clearSelection()
        self.tab.confirm = lambda *_: True
        self.tab._delete()
        self.assertEqual(len(self.tab.store.records), 3)


class RecordDialogTests(unittest.TestCase):
    def test_loads_and_rebuilds_a_record_unchanged(self):
        dialog = RecordDialog(reference.CACHE_1)
        rebuilt = dialog.build_record()
        for name in (
            "id",
            "container_mib",
            "mounted_bytes",
            "empty_free_bytes",
            "file_bytes",
            "file_count",
            "left_bytes",
            "cluster_bytes",
        ):
            with self.subTest(name):
                self.assertEqual(getattr(rebuilt, name), getattr(reference.CACHE_1, name))

    def test_shows_derived_values(self):
        dialog = RecordDialog(reference.CACHE_1)
        self.assertIn("266 240", dialog.header_label.text())
        self.assertIn("40 316 928", dialog.ntfs_label.text())
        self.assertIn("143 360", dialog.slack_label.text())

    def test_reports_the_impossible_left_space(self):
        dialog = RecordDialog(reference.CACHE_2)
        self.assertIn("не может быть меньше файла", dialog.issues_label.text())
        self.assertEqual(dialog.save_button.text(), "Сохранить с пометкой")

    def test_clean_record_saves_without_a_flag(self):
        dialog = RecordDialog(reference.CACHE_4)
        dialog._on_save()
        self.assertFalse(dialog.result_record().flagged)

    def test_broken_record_is_flagged_on_save(self):
        dialog = RecordDialog(reference.CACHE_2)
        dialog._on_save()
        self.assertTrue(dialog.result_record().flagged)

    def test_save_is_blocked_without_a_name(self):
        dialog = RecordDialog(replace(reference.CACHE_1, id=""))
        self.assertFalse(dialog.save_button.isEnabled())

    def test_partial_record_is_allowed(self):
        dialog = RecordDialog(Record(id="probe", container_mib=2048))
        self.assertEqual(dialog.issues_label.text(), "")
        self.assertTrue(dialog.save_button.isEnabled())


class WiringTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_records_feed_the_calculator(self):
        seeded_store(self.path)
        window = MainWindow(data_dir=Path(tempfile.mkdtemp()))
        window.records_tab.report_error = lambda *_: None
        window.records_tab.load_from(self.path)

        ntfs, slack = window.models()
        self.assertTrue(ntfs.calibrated)
        self.assertTrue(slack.calibrated)

        payload = Payload.for_file(reference.CACHE_1.file_bytes)
        calibrated = solve_container_mib(payload, ntfs=ntfs, slack=slack).container_mib
        default = solve_container_mib(payload).container_mib

        self.assertLessEqual(calibrated, default)
        self.assertGreaterEqual(calibrated, reference.TRUE_MINIMUM_MIB["Cache 1"])

    def test_calculator_updates_when_records_change(self):
        window = MainWindow(data_dir=Path(tempfile.mkdtemp()))
        window.records_tab.report_error = lambda *_: None
        window.records_tab.load_from(self.path)
        window.calc_tab.size_edit.setText(str(reference.CACHE_1.file_bytes))
        window.calc_tab._on_manual_edit()
        before = window.calc_tab.result_label.text()

        # The factory points cover 1–100 GiB, so there is no extrapolation
        # here any more — the model is calibrated from the first launch.
        self.assertTrue(window.models()[0].calibrated)

        window.records_tab.store.records = list(reference.ALL)
        window.records_tab._save()

        self.assertTrue(window.models()[0].calibrated)
        self.assertLessEqual(
            int(window.calc_tab.result_label.text().replace(" ", "")),
            int(before.replace(" ", "")),
        )
        # The size fell inside the range the measurements cover — nothing to
        # warn about.
        self.assertEqual(window.calc_tab.notes_label.text(), "")

    def test_saving_writes_the_file_and_a_backup(self):
        window = MainWindow(data_dir=Path(tempfile.mkdtemp()))
        window.records_tab.report_error = lambda *_: None
        window.records_tab.load_from(self.path)
        window.records_tab.store.add(reference.CACHE_1)
        window.records_tab._save()
        self.assertTrue(self.path.exists())

        window.records_tab.store.add(reference.CACHE_4)
        window.records_tab._save()
        self.assertTrue(window.records_tab.store.backup_path.exists())


class ModelessDialogTests(unittest.TestCase):
    """Edit windows are modeless.

    There can be several of them, and the Calculation tab stays alive.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"
        seeded_store(self.path)
        self.tab = RecordsTab()
        self.errors: list[tuple[str, str]] = []
        self.tab.report_error = lambda title, text: self.errors.append((title, text))
        self.tab.confirm = lambda *_: True
        self.tab.load_from(self.path)

    def tearDown(self):
        self.tab.close_editors()
        self._dir.cleanup()

    def test_two_records_open_at_once(self):
        first = self.tab.open_record(self.tab.store.records[0])
        second = self.tab.open_record(self.tab.store.records[1])
        self.assertIsNot(first, second)
        self.assertTrue(first.isVisible())
        self.assertTrue(second.isVisible())

    def test_the_same_record_opens_only_once(self):
        """Two windows on one row are a race won by whoever clicks last."""
        record = self.tab.store.records[0]
        first = self.tab.open_record(record)
        self.assertIs(self.tab.open_record(record), first)

    def test_the_dialog_does_not_block_the_program(self):
        dialog = self.tab.open_record(self.tab.store.records[0])
        self.assertFalse(dialog.isModal())

    def test_saving_lands_in_the_same_record_after_the_list_shifted(self):
        """An index taken on opening would already point at another row."""
        record = self.tab.store.records[2]
        dialog = self.tab.open_record(record)
        dialog.id_edit.setText("Переименована")
        self.tab.store.remove_at(0)
        dialog.save_button.click()
        self.assertEqual(self.tab.store.records[-1].id, "Переименована")
        self.assertEqual(len(self.tab.store.records), 2)

    def test_a_record_deleted_meanwhile_is_reported_not_resurrected(self):
        record = self.tab.store.records[1]
        dialog = self.tab.open_record(record)
        self.tab.store.records.remove(record)
        dialog.id_edit.setText("Призрак")
        dialog.save_button.click()
        self.assertTrue(self.errors)
        self.assertNotIn("Призрак", [item.id for item in self.tab.store.records])

    def test_deleting_a_record_closes_its_open_window(self):
        """Else its Save button looks working, but there is nowhere to save."""
        self.tab.table.selectRow(0)
        record = self.tab.store.records[0]
        dialog = self.tab.open_record(record)
        self.tab._delete()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.tab._editors, {})

    def test_changing_the_file_closes_every_window(self):
        """An open window holds a record of the previous store."""
        dialog = self.tab.open_record(self.tab.store.records[0])
        other = Path(self._dir.name) / "other.json"
        seeded_store(other)
        self.tab.load_from(other)
        self.assertFalse(dialog.isVisible())

    def test_cancelling_changes_nothing(self):
        record = self.tab.store.records[0]
        dialog = self.tab.open_record(record)
        dialog.id_edit.setText("Не сохранится")
        dialog.reject()
        self.assertEqual(self.tab.store.records[0].id, "Cache 1")

    def test_several_new_records_can_be_drafted_at_once(self):
        """Until a record is saved, the windows cannot clash in any way."""
        first = self.tab._add()
        second = self.tab._add()
        self.assertIsNot(first, second)
        self.assertEqual(len(self.tab._creators), 2)
        first.id_edit.setText("Свежая")
        first.container_edit.setText("2048")
        # setText does not raise textEdited, and Save is enabled by it: with
        # no name there is nothing to save, and the button is disabled.
        first._refresh()
        first.save_button.click()
        self.assertIn("Свежая", [item.id for item in self.tab.store.records])
        second.reject()

    def test_a_calibration_point_opens_once_per_size(self):
        first = self.tab.add_calibration_point(4096)
        self.assertIs(self.tab.add_calibration_point(4096), first)
        self.assertFalse(first.isModal())
        second = self.tab.add_calibration_point(8192)
        self.assertIsNot(second, first)
        first.reject()
        second.reject()

    def test_a_saved_calibration_point_reaches_the_store(self):
        dialog = self.tab.add_calibration_point(1024)
        dialog.mounted_edit.setText("1073475584")
        dialog.free_edit.setText("1055596544")
        dialog.save_button.click()
        volumes = [item.mounted_bytes for item in self.tab.store.calibration]
        self.assertIn(1073475584, volumes)


class FieldValidationTests(unittest.TestCase):
    """A letter in a byte field is a slipped key, not an unparsable value."""

    def typed(self, field, text):
        field.setText("")
        for symbol in text:
            field.insert(symbol)
        return field.text()

    def test_numeric_fields_take_digits_and_separators_only(self):
        dialog = RecordDialog()
        for name in (
            "container_edit",
            "mounted_edit",
            "free_edit",
            "file_edit",
            "count_edit",
            "alloc_edit",
            "left_edit",
            "predicted_edit",
            "predicted_safety_edit",
        ):
            with self.subTest(name):
                self.assertEqual(
                    self.typed(getattr(dialog, name), "12a3 4б5-"), "123 45"
                )

    def test_a_pasted_number_with_separators_still_fits(self):
        """Explorer gives numbers with spaces — parsing drops them anyway."""
        dialog = RecordDialog()
        dialog.mounted_edit.setText("")
        dialog.mounted_edit.insert("11 599 081 472")
        self.assertEqual(dialog.mounted_edit.text(), "11 599 081 472")

    def test_the_cluster_field_is_numeric_too(self):
        dialog = RecordDialog()
        self.assertIsNotNone(dialog.cluster_combo.validator())

    def test_text_fields_refuse_control_characters(self):
        """In JSON they end up escaped and later cannot be found by eye."""
        dialog = RecordDialog()
        self.assertEqual(self.typed(dialog.id_edit, "Cache\t5"), "Cache5")
        self.assertEqual(self.typed(dialog.note_edit, "за\rметка"), "заметка")

    def test_the_name_and_the_note_have_a_ceiling(self):
        dialog = RecordDialog()
        self.assertGreater(dialog.id_edit.maxLength(), 0)
        self.assertGreater(dialog.note_edit.maxLength(), dialog.id_edit.maxLength())


if __name__ == "__main__":
    unittest.main()
