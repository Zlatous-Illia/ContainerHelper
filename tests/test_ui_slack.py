"""The UI of two new features: copy-slack collection and the prediction check.

The file sets in the dialog and the measurements table on the Calibration tab
are the first; the miss columns on the Records tab and in the record dialog
are the second.
"""

import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

_settings_dir = tempfile.mkdtemp(prefix="containerhelper-settings-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from containerhelper.fileset import FILE_SETS, KIB, FileSet, Group  # noqa: E402
from containerhelper.model import MIB  # noqa: E402
from tests.reference import HEADERS_AND_TAIL  # noqa: E402
from containerhelper.records import Record, Store  # noqa: E402
from containerhelper.ui.app import MainWindow  # noqa: E402
from containerhelper.ui.calibration_tab import (  # noqa: E402
    RECOMMENDED_MIB,
    SLACK_COLUMNS,
)
from containerhelper.ui.collect_dialog import PATH_KEY, CollectDialog  # noqa: E402
from containerhelper.ui.record_dialog import RecordDialog  # noqa: E402
from containerhelper.ui.records_tab import COLUMNS, RecordsTab  # noqa: E402
from containerhelper.veracrypt import VeraCrypt  # noqa: E402

from tests.test_veracrypt import Fake, make_install

TINY_SET = FileSet("tiny", "10 файлов по 1 KiB", (Group(10, KIB),))
FAT_SET = FileSet("fat", "4 файла по 1 MiB", (Group(4, MIB),))

VOLUME = 1024 * MIB - HEADERS_AND_TAIL


def column_of(columns, title: str) -> int:
    """Column index by header title — a tie to a number breaks silently."""
    for index, (name, _kind, _tip) in enumerate(columns):
        if name == title:
            return index
    raise AssertionError(f"нет столбца «{title}»")


def slack_record(**changes) -> Record:
    base = Record(
        id="Запас 10 файлов по 1 KiB",
        container_mib=1024,
        mounted_bytes=VOLUME,
        empty_free_bytes=VOLUME - 18 * MIB,
        file_bytes=10 * KIB,
        file_count=10,
        file_alloc_bytes=10 * 4096,
        left_bytes=VOLUME - 18 * MIB - 10 * 4096 - 200_000,
        predicted_mib=1024,
        predicted_safety_mib=4,
        fileset="tiny",
    )
    return replace(base, **changes)


class DialogFixture(unittest.TestCase):
    """A dialog already told where VeraCrypt is, with the self-check off."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.folder = Path(self._dir.name)
        self.veracrypt_dir = make_install(
            self.folder / "VeraCrypt", "VeraCrypt Format.exe", "VeraCrypt.exe"
        )
        self.settings = QSettings(
            str(self.folder / "settings.ini"), QSettings.IniFormat
        )
        self.settings.setValue(PATH_KEY, str(self.veracrypt_dir))

    def tearDown(self):
        self._dir.cleanup()

    def dialog(self, **kwargs):
        kwargs.setdefault("sizes", (512,))
        kwargs.setdefault("filesets", (TINY_SET, FAT_SET))
        dialog = CollectDialog(settings=self.settings, **kwargs)
        dialog.is_admin = lambda: True
        dialog._workdir = self.folder
        dialog.with_self_check.setChecked(False)
        return dialog


class ScopeTests(DialogFixture):
    """Both "measure all" and "measure some": one check box per file set."""

    def test_every_set_gets_its_own_checkbox(self):
        dialog = self.dialog()
        self.assertEqual(set(dialog.fileset_boxes), {"tiny", "fat"})

    def test_a_measured_set_starts_unchecked(self):
        dialog = self.dialog(slack_covered=(TINY_SET.key,))
        self.assertFalse(dialog.fileset_boxes["tiny"].isChecked())
        self.assertTrue(dialog.fileset_boxes["fat"].isChecked())

    def test_unchecking_takes_the_set_out_of_the_plan(self):
        dialog = self.dialog()
        dialog.fileset_boxes["fat"].setChecked(False)
        keys = [step.key for step in dialog.steps() if step.key]
        self.assertEqual(keys, ["tiny"])

    def test_the_whole_group_can_be_turned_off(self):
        dialog = self.dialog()
        dialog.want_slack.setChecked(False)
        self.assertTrue(all(not step.key for step in dialog.steps()))

    def test_ntfs_can_be_turned_off_to_measure_slack_alone(self):
        dialog = self.dialog()
        dialog.want_ntfs.setChecked(False)
        self.assertTrue(all(step.key for step in dialog.steps()))

    def test_all_and_none_buttons_work(self):
        dialog = self.dialog(slack_covered=(TINY_SET.key,))
        dialog._set_all(True)
        self.assertTrue(all(box.isChecked() for box in dialog.fileset_boxes.values()))
        dialog._set_all(False)
        self.assertFalse(any(box.isChecked() for box in dialog.fileset_boxes.values()))

    def test_nothing_selected_disables_the_start(self):
        dialog = self.dialog(covered=(512,))
        dialog._set_all(False)
        self.assertFalse(dialog.start_button.isEnabled())

    def test_the_label_names_the_price_of_the_set(self):
        """A set's price is a label under the check box, not in the check box.

        QCheckBox does not wrap lines, and this whole line asked for more than
        a thousand pixels in a 660-wide window — what got cut off was exactly
        the ending, which speaks of the space and of a measurement already
        taken.
        """
        dialog = self.dialog()
        self.assertEqual(dialog.fileset_boxes["fat"].text(), "4 файла по 1 MiB")
        text = dialog.fileset_notes["fat"].text()
        self.assertIn("4 файла", text)
        self.assertIn("по кластерам", text)
        self.assertIn("нужно", text)

    def test_a_measured_set_says_so_on_its_label(self):
        dialog = self.dialog(slack_covered=(TINY_SET.key,))
        self.assertIn("уже есть", dialog.fileset_notes["tiny"].text())

    def test_slack_steps_come_after_the_empty_volumes(self):
        """A set's container comes from the model that the points refine."""
        dialog = self.dialog()
        keys = [bool(step.key) for step in dialog.steps()]
        self.assertEqual(keys, sorted(keys))


class SpaceTests(DialogFixture):
    """Free-space pre-check: what is needed and what to do when it is short."""

    def test_the_summary_names_the_hungriest_step(self):
        dialog = self.dialog()
        self.assertIn("прожорливому шагу нужно", dialog.scope_summary.text())
        self.assertIn("свободно", dialog.space_label.text())

    def test_a_plan_that_does_not_fit_says_so_and_still_starts(self):
        """Skip and measure later — rather than refuse everything at once."""
        dialog = self.dialog()
        dialog.free_bytes = lambda: MIB
        dialog._refresh_scope()
        self.assertIn("места не хватает", dialog.scope_summary.text())
        self.assertTrue(dialog.start_button.isEnabled())

    def test_a_set_that_does_not_fit_is_marked_on_its_label(self):
        dialog = self.dialog()
        dialog.free_bytes = lambda: MIB
        dialog._refresh_scope()
        self.assertIn("НЕ ХВАТАЕТ МЕСТА", dialog.fileset_notes["fat"].text())

    def test_the_progress_bar_is_measured_in_written_bytes(self):
        """Not steps: an empty terabyte takes seconds, a set takes minutes."""
        dialog = self.dialog()
        self.assertGreater(dialog.progress.maximum(), len(dialog.steps()))


class CoverageTableTests(unittest.TestCase):
    """Copy-slack measurements have their own table on the Calibration tab."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.window = MainWindow(data_dir=Path(self._dir.name))
        self.window.confirm = lambda *_: True

    def tearDown(self):
        self.window.close()
        self._dir.cleanup()

    def tab(self):
        return self.window.calibration_tab

    def put(self, *records):
        for record in records:
            self.window.records_tab.store.put_calibration(record)
        self.window.records_tab.save_store()

    def test_a_slack_measurement_shows_up_there(self):
        self.put(slack_record())
        self.assertEqual(self.tab().slack_table.rowCount(), 1)

    def test_it_does_not_take_a_row_in_the_coverage_table(self):
        """Otherwise Use factory in that row would disable it as well."""
        before = [
            self.tab().table.item(row, 2).text()
            for row in range(self.tab().table.rowCount())
        ]
        self.put(slack_record())
        after = [
            self.tab().table.item(row, 2).text()
            for row in range(self.tab().table.rowCount())
        ]
        self.assertEqual(before, after)

    def test_the_miss_is_shown_with_its_sign(self):
        self.put(slack_record())
        column = column_of(SLACK_COLUMNS, "Промах")
        self.assertTrue(self.tab().slack_table.item(0, column).text().startswith("+"))

    def test_the_model_miss_takes_the_safety_out(self):
        self.put(slack_record())
        miss = int(
            self.tab().slack_table.item(0, column_of(SLACK_COLUMNS, "Промах")).text()
        )
        model_miss = int(
            self.tab()
            .slack_table.item(0, column_of(SLACK_COLUMNS, "Промах модели"))
            .text()
        )
        self.assertEqual(miss - model_miss, 4)

    def test_the_summary_says_the_forecast_held(self):
        self.put(slack_record())
        self.assertIn("ни разу не занизил", self.tab().slack_summary.text())

    def test_the_summary_names_a_forecast_that_would_not_have_fitted(self):
        self.put(slack_record(predicted_mib=10))
        self.assertIn("занизил на 1", self.tab().slack_summary.text())

    def test_one_file_count_is_not_enough_for_a_slope(self):
        self.put(slack_record())
        self.assertIn("двух и более различных", self.tab().slack_summary.text())

    def test_two_file_counts_stop_the_warning(self):
        self.put(
            slack_record(),
            slack_record(id="Другой", fileset="fat", file_count=500),
        )
        self.assertNotIn("двух и более различных", self.tab().slack_summary.text())

    def test_removing_a_measurement_reaches_the_store(self):
        record = slack_record()
        self.put(record)
        self.tab().slackRemoveRequested.emit(
            self.window.records_tab.store.slack_measurements()[0]
        )
        self.assertEqual(self.window.records_tab.store.slack_measurements(), [])

    def test_a_refused_removal_changes_nothing(self):
        self.window.confirm = lambda *_: False
        self.put(slack_record())
        self.tab().slackRemoveRequested.emit(
            self.window.records_tab.store.slack_measurements()[0]
        )
        self.assertEqual(len(self.window.records_tab.store.slack_measurements()), 1)

    def test_resetting_to_factory_leaves_slack_measurements_alone(self):
        """There may be no factory measurement for that file count at all."""
        self.put(
            slack_record(),
            Record(
                id="Калибровка 1 GiB",
                container_mib=1024,
                mounted_bytes=VOLUME,
                empty_free_bytes=VOLUME - 18 * MIB,
            ),
        )
        self.window._disable_all_points()
        store = self.window.records_tab.store
        self.assertTrue(all(item.disabled for item in store.calibration_points()))
        self.assertFalse(any(item.disabled for item in store.slack_measurements()))

    def test_the_window_hands_the_dialog_the_sets_and_what_is_covered(self):
        self.put(slack_record())
        dialog = self.window.build_collect_dialog()
        self.assertEqual(len(dialog.fileset_boxes), len(FILE_SETS))
        self.assertEqual(self.window.covered_filesets(), ["tiny"])
        self.assertEqual(dialog._forbidden, tuple(RECOMMENDED_MIB))

    def test_two_sets_with_one_file_are_both_still_offered(self):
        """A file-count key would silently eat the second such measurement."""
        self.put(
            slack_record(id="Один файл 64 MiB", fileset="one", file_count=1),
            slack_record(id="Один файл 4 GiB", fileset="huge", file_count=1),
        )
        self.assertEqual(len(self.window.records_tab.store.slack_measurements()), 2)
        self.assertEqual(self.window.covered_filesets(), ["huge", "one"])


class RecommendedGridTests(unittest.TestCase):
    """The recommended sizes grid: denser where the curve bends."""

    def test_the_sizes_go_up_without_repeats(self):
        self.assertEqual(list(RECOMMENDED_MIB), sorted(set(RECOMMENDED_MIB)))

    def test_no_doubling_step_is_left_where_the_curve_bends(self):
        """The 1610 MiB measurement lay 202 672 B above the 1024…2048 chord.

        The sag grows as the square of the gap width, so segments with a
        doubling step where the metadata still grows steeply are the weakest
        spot of the grid. Above 8 GiB the curve is flat and needs no such
        density.
        """
        bending = [size for size in RECOMMENDED_MIB if size <= 8192]
        for low, high in zip(bending, bending[1:]):
            with self.subTest(f"{low}→{high}"):
                self.assertLess(high / low, 1.6)

    def test_the_four_midpoints_are_there(self):
        for size in (768, 1536, 3072, 6144):
            self.assertIn(size, RECOMMENDED_MIB)


class MissColumnTests(unittest.TestCase):
    """The miss columns on the Records tab — from real copies."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"
        self.record = replace(slack_record(id="Копия"), fileset="")
        Store(path=self.path, records=[self.record]).save()
        self.tab = RecordsTab()
        self.tab.report_error = lambda *_: None
        self.tab.load_from(self.path)

    def tearDown(self):
        self._dir.cleanup()

    def cell(self, title):
        return self.tab.table.item(0, column_of(COLUMNS, title)).text()

    def test_both_columns_are_there(self):
        self.assertEqual(self.cell("Промах, MiB"), f"{self.record.miss_mib:+d}")
        self.assertEqual(
            self.cell("Промах модели, MiB"), f"{self.record.model_miss_mib:+d}"
        )

    def test_a_record_without_a_promise_shows_a_dash(self):
        Store(path=self.path, records=[replace(self.record, predicted_mib=None)]).save()
        self.tab.load_from(self.path)
        self.assertEqual(self.cell("Промах, MiB"), "—")

    def test_the_summary_reports_the_check(self):
        self.assertIn("Прогноз проверен на 1", self.tab.summary_label.text())

    def test_the_summary_names_a_forecast_that_fell_short(self):
        Store(path=self.path, records=[replace(self.record, predicted_mib=10)]).save()
        self.tab.load_from(self.path)
        self.assertIn("занизил", self.tab.summary_label.text())

    def test_nothing_to_check_is_said_plainly(self):
        Store(path=self.path, records=[replace(self.record, predicted_mib=None)]).save()
        self.tab.load_from(self.path)
        self.assertIn("ни на чём не проверен", self.tab.summary_label.text())


class RecordDialogForecastTests(unittest.TestCase):
    def test_the_promise_is_editable_and_kept(self):
        dialog = RecordDialog(slack_record())
        self.assertEqual(dialog.predicted_edit.text(), "1 024")
        self.assertEqual(dialog.predicted_safety_edit.text(), "4")
        self.assertEqual(dialog.build_record().predicted_mib, 1024)

    def test_the_verdict_is_spelled_out(self):
        dialog = RecordDialog(slack_record())
        self.assertIn("перезаклад", dialog.miss_label.text())

    def test_a_forecast_that_would_not_have_fitted_says_so(self):
        dialog = RecordDialog(slack_record(predicted_mib=10))
        self.assertIn("не влезли бы", dialog.miss_label.text())

    def test_without_a_promise_it_says_what_is_missing(self):
        dialog = RecordDialog(slack_record(predicted_mib=None))
        self.assertIn("Обещано расчётом", dialog.miss_label.text())

    def test_the_minimum_is_shown_too(self):
        record = slack_record()
        dialog = RecordDialog(record)
        self.assertIn(str(record.minimum_mib), dialog.minimum_label.text())

    def test_taking_the_calculation_fills_the_promise_and_its_safety(self):
        """Else the prediction is typed by hand after the model has changed."""
        from containerhelper.model import Payload

        dialog = RecordDialog(
            payload_provider=lambda: Payload(1000, 4096, 1, 4096),
            container_provider=lambda: 777,
            safety_provider=lambda: 9,
        )
        dialog._take_payload()
        self.assertEqual(dialog.predicted_edit.text(), "777")
        self.assertEqual(dialog.predicted_safety_edit.text(), "9")
        self.assertEqual(dialog.container_edit.text(), "777")

    def test_a_calibration_point_has_no_forecast_fields(self):
        dialog = RecordDialog(
            Record(id="Калибровка", container_mib=1024), calibration=True
        )
        self.assertFalse(dialog.predicted_edit.isVisibleTo(dialog))
        self.assertFalse(dialog.predicted_safety_edit.isVisibleTo(dialog))


class RunTests(DialogFixture):
    """A full pass through a real thread: the slack measurement gets out."""

    def running_dialog(self, **kwargs):
        dialog = self.dialog(sizes=(), **kwargs)
        dialog.confirm = lambda *_: True
        dialog.report_error = lambda *_: None
        dialog.want_ntfs.setChecked(False)
        self.fake = Fake()
        dialog.make_veracrypt = lambda install: VeraCrypt(
            install=install,
            run=self.fake.run,
            pause=self.fake.pause,
            volumes=self.fake.volumes(),
        )
        return dialog

    def drain(self, dialog, seconds=10.0):
        deadline = time.monotonic() + seconds
        while dialog.running() and time.monotonic() < deadline:
            _app.processEvents()
        _app.processEvents()

    def test_both_sets_are_measured_and_handed_outwards(self):
        dialog = self.running_dialog()
        measured = []
        dialog.pointMeasured.connect(measured.append)

        dialog._start()
        self.drain(dialog)

        self.assertEqual([record.fileset for record in measured], ["tiny", "fat"])
        self.assertTrue(all(record.copy_slack_measured for record in measured))
        self.assertTrue(all(record.miss_mib is not None for record in measured))
        self.assertEqual(dialog.progress.value(), dialog.progress.maximum())

    def test_the_log_reports_the_slack_and_the_forecast(self):
        dialog = self.running_dialog()
        dialog._start()
        self.drain(dialog)
        text = dialog.log.toPlainText()
        self.assertIn("запас на копирование", text)
        self.assertIn("при 10 файлах", text)
        self.assertIn("прогноз: обещано", text)
        self.assertIn("Готово", text)

    def test_a_step_without_room_is_counted_as_skipped_not_failed(self):
        dialog = self.running_dialog()
        dialog.free_bytes = lambda: MIB
        dialog._start()
        self.drain(dialog)
        text = dialog.log.toPlainText()
        self.assertIn("пропущен: не хватает места", text)
        self.assertIn("пропущено из-за места: 2", text)
        self.assertIn("доснять позже", text)
        self.assertNotIn("ошибка:", text)

    def test_the_bar_never_goes_backwards(self):
        """Four more phases follow the write, and their share is zero.

        If the dialog did not remember what it had reached, the bar would roll
        back to the start of the step during the check, the remount, the
        left-space measurement and the cleanup.
        """
        dialog = self.running_dialog()
        seen = []
        dialog._start()
        deadline = time.monotonic() + 10.0
        while dialog.running() and time.monotonic() < deadline:
            _app.processEvents()
            value = dialog.progress.value()
            if not seen or seen[-1] != value:
                seen.append(value)
        _app.processEvents()
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(len(seen), 2)

    def test_nothing_is_left_on_disk_after_the_run(self):
        dialog = self.running_dialog()
        dialog._start()
        self.drain(dialog)
        self.assertEqual(list(self.folder.glob("*.hc")), [])


class NoteFitTests(DialogFixture):
    """A set's price must not collapse under scrolling.

    QLabel's minimum height does not depend on its width, and the layout
    squeezed the wrapped label to one line and below — the second line
    vanished whole, together with «НЕ ХВАТАЕТ МЕСТА».
    """

    def test_every_note_keeps_room_for_its_own_text(self):
        dialog = self.dialog()
        for key, note in dialog.fileset_notes.items():
            with self.subTest(key):
                self.assertGreaterEqual(
                    note.minimumHeight(), note.fontMetrics().height()
                )

    def test_a_two_line_note_asks_for_two_lines(self):
        dialog = self.dialog()
        note = dialog.fileset_notes["fat"]
        long_text = "очень длинная строка про набор " * 6
        dialog._set_note(note, long_text)
        self.assertGreater(note.minimumHeight(), note.fontMetrics().height() * 2)


if __name__ == "__main__":
    unittest.main()
