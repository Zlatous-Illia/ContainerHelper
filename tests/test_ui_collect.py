"""Диалог автоматического сбора: поиск VeraCrypt, план и ход работы."""

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

_settings_dir = tempfile.mkdtemp(prefix="containerhelper-settings-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from containerhelper.collect import SELF_CHECK_MIB, Collector, Step  # noqa: E402
from containerhelper.records import Record  # noqa: E402
from containerhelper.veracrypt import VeraCrypt, install_at  # noqa: E402
from containerhelper.ui.app import MainWindow  # noqa: E402
from containerhelper.ui.collect_dialog import (  # noqa: E402
    PATH_KEY,
    CollectDialog,
    CollectWorker,
)

from tests.test_veracrypt import Fake, make_install

#: Крохотные размеры: диалогу всё равно, какие они, а файлы создаются
#: настоящие — гигабайтным тут делать нечего.
TINY = (1, 2, 4)


class DialogFixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.folder = Path(self._dir.name)
        self.veracrypt_dir = make_install(
            self.folder / "VeraCrypt", "VeraCrypt Format.exe", "VeraCrypt.exe"
        )
        self.settings = QSettings(
            str(self.folder / "settings.ini"), QSettings.IniFormat
        )
        self.errors = []
        self.chosen = ""

    def tearDown(self):
        self._dir.cleanup()

    def dialog(self, sizes=TINY, covered=()):
        """Диалог, которому VeraCrypt уже указан — как после первого выбора."""
        self.settings.setValue(PATH_KEY, str(self.veracrypt_dir))
        dialog = CollectDialog(sizes=sizes, covered=covered, settings=self.settings)
        dialog.report_error = lambda title, text: self.errors.append((title, text))
        dialog.confirm = lambda *_: True
        dialog.ask_directory = lambda *_: self.chosen
        dialog.is_admin = lambda: True
        dialog._workdir = self.folder
        return dialog

    def blind(self, **kwargs):
        """Диалог, который VeraCrypt не находит нигде."""
        dialog = CollectDialog(
            sizes=kwargs.pop("sizes", TINY), settings=self.settings, **kwargs
        )
        dialog.report_error = lambda title, text: self.errors.append((title, text))
        dialog.confirm = lambda *_: True
        dialog.ask_directory = lambda *_: self.chosen
        dialog.find_install = lambda extra=(): None
        dialog._install = None
        dialog._refresh_install()
        dialog._workdir = self.folder
        return dialog


class DiscoveryTests(DialogFixture):
    """Стандартные места ищутся сами, остальное спрашивается."""

    def test_a_missing_veracrypt_names_both_standard_places(self):
        dialog = self.blind()
        text = dialog.install_label.text()
        self.assertIn(r"C:\Program Files\VeraCrypt", text)
        self.assertIn(r"C:\Program Files (x86)\VeraCrypt", text)

    def test_nothing_starts_without_veracrypt(self):
        dialog = self.blind()
        self.assertFalse(dialog.start_button.isEnabled())

    def test_a_named_folder_is_accepted_and_remembered(self):
        dialog = self.blind()
        self.chosen = str(self.veracrypt_dir)
        dialog._choose_install()

        self.assertIsNotNone(dialog._install)
        self.assertIn(str(self.veracrypt_dir), dialog.install_label.text())
        self.assertTrue(dialog.start_button.isEnabled())
        self.assertEqual(self.settings.value(PATH_KEY, "", type=str), str(self.veracrypt_dir))

    def test_a_folder_without_binaries_is_refused_with_a_reason(self):
        dialog = self.blind()
        self.chosen = str(self.folder / "пусто")
        (self.folder / "пусто").mkdir()
        dialog._choose_install()

        self.assertIsNone(dialog._install)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("VeraCrypt Format.exe", self.errors[0][1])

    def test_the_remembered_folder_is_used_next_time(self):
        self.settings.setValue(PATH_KEY, str(self.veracrypt_dir))
        dialog = CollectDialog(sizes=TINY, settings=self.settings)
        self.assertIsNotNone(dialog._install)
        self.assertEqual(dialog._install.directory, self.veracrypt_dir)

    def test_cancelling_the_folder_dialog_changes_nothing(self):
        dialog = self.blind()
        self.chosen = ""
        dialog._choose_install()
        self.assertIsNone(dialog._install)
        self.assertEqual(self.errors, [])


class ScopeTests(DialogFixture):
    def test_only_missing_skips_what_is_already_measured(self):
        dialog = self.dialog(covered=(2,))
        dialog.with_self_check.setChecked(False)
        self.assertEqual(
            [step.container_mib for step in dialog.steps()], [1, 4]
        )

    def test_everything_measures_every_size(self):
        dialog = self.dialog(covered=(2,))
        dialog.with_self_check.setChecked(False)
        dialog.everything.setChecked(True)
        self.assertEqual(
            [step.container_mib for step in dialog.steps()], list(TINY)
        )

    def test_the_self_check_adds_two_steps_of_its_own(self):
        dialog = self.dialog()
        self.assertTrue(dialog.with_self_check.isChecked())
        first_two = dialog.steps()[:2]
        self.assertTrue(all(step.self_check for step in first_two))
        self.assertTrue(all(step.container_mib == SELF_CHECK_MIB for step in first_two))

    def test_the_counts_are_written_on_the_buttons(self):
        dialog = self.dialog(covered=(2,))
        self.assertIn("(2)", dialog.only_missing.text())
        self.assertIn("(3)", dialog.everything.text())

    def test_nothing_left_to_measure_disables_the_start(self):
        dialog = self.dialog(sizes=(1,), covered=(1,))
        dialog.with_self_check.setChecked(False)
        self.assertFalse(dialog.start_button.isEnabled())
        self.assertIn("уже закрыты", dialog.scope_summary.text())


class RightsTests(DialogFixture):
    def test_an_elevated_run_hides_the_offer(self):
        dialog = self.dialog()
        dialog.is_admin = lambda: True
        dialog._refresh_rights()
        self.assertFalse(dialog.elevate_button.isVisible())
        self.assertIn("с правами администратора", dialog.rights_label.text())

    def test_without_rights_the_offer_is_there_with_a_reason(self):
        dialog = self.dialog()
        dialog.is_admin = lambda: False
        dialog._refresh_rights()
        self.assertIn("Прав администратора нет", dialog.rights_label.text())
        self.assertIn("подтверждение на каждый контейнер", dialog.rights_label.text())

    def test_a_refused_elevation_is_reported(self):
        dialog = self.dialog()
        dialog.relaunch = lambda data_dir: False
        dialog._elevate()
        self.assertEqual(len(self.errors), 1)

    def test_a_successful_elevation_asks_the_window_to_close(self):
        """Две копии в одной папке данных писали бы поверх друг друга."""
        dialog = self.dialog()
        dialog.relaunch = lambda data_dir: True
        asked = []
        dialog.relaunchRequested.connect(lambda: asked.append(True))
        dialog._elevate()
        self.assertEqual(asked, [True])


class WorkerTests(unittest.TestCase):
    """Шаги крутятся сами, отмена срабатывает между ними."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.workdir = Path(self._dir.name)
        folder = make_install(
            self.workdir / "VeraCrypt", "VeraCrypt Format.exe", "VeraCrypt.exe"
        )
        self.fake = Fake()
        self.collector = Collector(
            veracrypt=VeraCrypt(
                install=install_at(folder),
                run=self.fake.run,
                pause=self.fake.pause,
                volumes=self.fake.volumes(),
            ),
            workdir=self.workdir,
            steps=[Step(mib) for mib in TINY],
        )
        self.started = []
        self.finished = []
        self.reason = None

    def tearDown(self):
        self._dir.cleanup()

    def worker(self):
        worker = CollectWorker(self.collector)
        worker.stepStarted.connect(lambda index, title: self.started.append(index))
        worker.stepFinished.connect(self.finished.append)
        worker.done.connect(lambda reason: setattr(self, "reason", reason))
        return worker

    def test_every_step_runs_in_order(self):
        self.worker().run()
        self.assertEqual(self.started, [0, 1, 2])
        self.assertEqual(len(self.finished), 3)
        self.assertEqual(self.reason, "")

    def test_stopping_before_the_first_step_runs_nothing(self):
        worker = self.worker()
        worker.stop()
        worker.run()
        self.assertEqual(self.finished, [])
        self.assertIn("Остановлено", self.reason)

    def test_a_fatal_step_ends_the_run(self):
        self.collector.steps = [Step(1, self_check=True), Step(2)]
        self.fake.broken.add("create")
        self.worker().run()
        self.assertEqual(len(self.finished), 1)
        self.assertTrue(self.reason)

    def test_orphans_are_reported_before_the_run(self):
        (self.workdir / "containerhelper-calibration-99.hc").write_bytes(b"")
        notes = []
        worker = self.worker()
        worker.note.connect(notes.append)
        worker.run()
        self.assertTrue(any("прерванного сбора" in note for note in notes))


class RunTests(DialogFixture):
    """Полный проход через настоящий поток: замеры доходят наружу."""

    def running_dialog(self):
        dialog = self.dialog()
        dialog.with_self_check.setChecked(False)
        fake = Fake()
        dialog.make_veracrypt = lambda install: VeraCrypt(
            install=install,
            run=fake.run,
            pause=fake.pause,
            volumes=fake.volumes(),
        )
        return dialog

    def drain(self, dialog, seconds=10.0):
        deadline = time.monotonic() + seconds
        while dialog.running() and time.monotonic() < deadline:
            _app.processEvents()
        _app.processEvents()

    def test_a_whole_run_hands_every_measurement_outwards(self):
        dialog = self.running_dialog()
        measured = []
        dialog.pointMeasured.connect(measured.append)

        dialog._start()
        self.drain(dialog)

        self.assertEqual([record.container_mib for record in measured], list(TINY))
        self.assertTrue(all(record.filesystem == "NTFS" for record in measured))
        # Полоса меряется байтами, а не шагами: сравнивать её значение с
        # числом шагов больше не с чем, зато дойти до конца она обязана.
        self.assertEqual(dialog.progress.value(), dialog.progress.maximum())
        self.assertIn("Готово", dialog.log.toPlainText())

    def test_the_note_says_where_the_numbers_came_from(self):
        dialog = self.running_dialog()
        measured = []
        dialog.pointMeasured.connect(measured.append)

        dialog._start()
        self.drain(dialog)

        self.assertIn("Автоматический сбор", measured[0].note)

    def test_buttons_come_back_after_the_run(self):
        dialog = self.running_dialog()
        dialog._start()
        self.drain(dialog)
        self.assertTrue(dialog.start_button.isEnabled())
        self.assertFalse(dialog.stop_button.isEnabled())

    def test_an_unwritable_folder_stops_before_anything_is_created(self):
        dialog = self.running_dialog()
        dialog._workdir = Path("Z:/нет такого пути/и не будет")
        dialog._start()
        self.assertFalse(dialog.running())
        self.assertEqual(len(self.errors), 1)


class TabTests(unittest.TestCase):
    """Кнопка на вкладке «Калибровка» доводит просьбу до окна."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.window = MainWindow(data_dir=Path(self._dir.name))

    def tearDown(self):
        self.window.close()
        self._dir.cleanup()

    def test_the_tab_offers_automatic_collection(self):
        button = self.window.calibration_tab.collect_button
        self.assertIn("автоматически", button.text())

    def test_pressing_it_reaches_the_window(self):
        # Штатный получатель отцеплен: он поднимает модальное окно, и закрыть
        # его из теста нечем.
        self.window.calibration_tab.collectRequested.disconnect()
        asked = []
        self.window.calibration_tab.collectRequested.connect(lambda: asked.append(True))
        self.window.calibration_tab.collect_button.click()
        self.assertEqual(asked, [True])

    def test_the_window_hands_the_dialog_its_store_and_data_folder(self):
        dialog = self.window.build_collect_dialog()
        self.assertEqual(dialog._data_dir, self.window.data_dir)

        record = Record(
            id="Снято",
            container_mib=1024,
            cluster_bytes=4096,
            mounted_bytes=1_073_475_584,
            empty_free_bytes=1_055_596_544,
        )
        dialog.pointMeasured.emit(record)
        self.assertEqual(
            [item.id for item in self.window.records_tab.store.calibration], ["Снято"]
        )

    def test_covered_sizes_come_from_the_measurements_file(self):
        self.window.records_tab.store.calibration = [
            Record(id="Своя", container_mib=1024, mounted_bytes=1, empty_free_bytes=0),
            Record(
                id="Отключённая",
                container_mib=2048,
                mounted_bytes=2,
                empty_free_bytes=1,
                disabled=True,
            ),
        ]
        self.assertEqual(self.window.covered_sizes(), [1024])


if __name__ == "__main__":
    unittest.main()
