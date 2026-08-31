"""Универсальный выбор источников: сложение файлов и папок и статистика."""

import ctypes
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (  # noqa: E402
    QItemSelectionModel,
    QMimeData,
    QPoint,
    QSettings,
    Qt,
    QUrl,
)
from PySide6.QtGui import QDropEvent  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QDialog,
    QLineEdit,
)

_app = QApplication.instance() or QApplication([])

_settings_dir = tempfile.mkdtemp(prefix="containerhelper-settings-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from containerhelper.model import CopySlackModel, NtfsModel  # noqa: E402
from containerhelper.sizes import scan_paths, unique_roots  # noqa: E402
from containerhelper.ui.calc_tab import SOURCE_COLUMNS, CalcTab  # noqa: E402
from containerhelper.ui.path_picker import (  # noqa: E402
    FILE_VIEWS,
    PathPicker,
)


def default_models():
    return NtfsModel(), CopySlackModel()


class TreeFixture(unittest.TestCase):
    """Две независимые папки и отдельный файл — обычный случай выбора."""

    def setUp(self):
        self._first = tempfile.TemporaryDirectory()
        self._second = tempfile.TemporaryDirectory()
        self.first = Path(self._first.name)
        self.second = Path(self._second.name)

        (self.first / "nested").mkdir()
        (self.first / "nested" / "a.bin").write_bytes(b"\0" * 5000)
        (self.first / "b.bin").write_bytes(b"\0" * 1)
        (self.second / "c.bin").write_bytes(b"\0" * 4097)

    def tearDown(self):
        self._first.cleanup()
        self._second.cleanup()


class MultiScanTests(TreeFixture):
    def test_a_folder_and_a_file_add_up(self):
        result = scan_paths([self.first, self.second / "c.bin"], 4096)
        self.assertEqual(result.payload.file_count, 3)
        self.assertEqual(result.payload.logical_bytes, 5000 + 1 + 4097)
        self.assertEqual(result.payload.alloc_bytes, 8192 + 4096 + 8192)

    def test_every_source_keeps_its_own_numbers(self):
        result = scan_paths([self.first, self.second / "c.bin"], 4096)
        folder, single = result.sources
        self.assertTrue(folder.is_dir)
        self.assertEqual(folder.file_count, 2)
        self.assertEqual(folder.dir_count, 1)
        self.assertFalse(single.is_dir)
        self.assertEqual(single.file_count, 1)
        self.assertEqual(single.alloc_bytes(4096), 8192)

    def test_a_path_inside_a_chosen_folder_is_not_counted_twice(self):
        """Иначе выбор папки вместе с её файлом молча завышал бы расчёт."""
        together = scan_paths([self.first, self.first / "nested" / "a.bin"], 4096)
        alone = scan_paths([self.first], 4096)
        self.assertEqual(together.payload.file_count, alone.payload.file_count)
        self.assertEqual(len(together.sources), 1)

    def test_the_nested_path_loses_whichever_order_it_came_in(self):
        first = scan_paths([self.first / "nested", self.first], 4096)
        self.assertEqual([s.path for s in first.sources], [str(self.first)])

    def test_a_repeated_path_is_taken_once(self):
        result = scan_paths([self.first, self.first], 4096)
        self.assertEqual(len(result.sources), 1)

    def test_selection_order_survives(self):
        result = scan_paths([self.second / "c.bin", self.first], 4096)
        self.assertEqual(
            [s.path for s in result.sources],
            [str(self.second / "c.bin"), str(self.first)],
        )

    def test_unrelated_neighbours_are_both_kept(self):
        """«C:\\a\\b» лежит не внутри «C:\\ab», хотя строкой похоже."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ab").mkdir()
            (root / "a").mkdir()
            kept = unique_roots([root / "a", root / "ab"])
            self.assertEqual(len(kept), 2)

    def test_an_unreadable_path_does_not_stop_the_rest(self):
        result = scan_paths([self.first / "absent", self.second], 4096)
        self.assertEqual(result.payload.file_count, 1)
        self.assertFalse(result.ok)


class StatsTests(TreeFixture):
    def stats(self, cluster=4096):
        return scan_paths([self.first, self.second / "c.bin"], cluster).stats(cluster)

    def test_totals_cover_both_sources(self):
        stats = self.stats()
        self.assertEqual((stats.sources, stats.folders, stats.files), (2, 1, 1))
        self.assertEqual(stats.file_count, 3)
        self.assertEqual(stats.dir_count, 1)

    def test_cluster_tail_is_the_difference(self):
        stats = self.stats()
        self.assertEqual(
            stats.cluster_tail, stats.alloc_bytes - stats.logical_bytes
        )

    def test_a_bigger_cluster_costs_more(self):
        self.assertGreater(self.stats(65536).alloc_bytes, self.stats(4096).alloc_bytes)

    def test_extremes_and_average(self):
        stats = self.stats()
        self.assertEqual(stats.largest_bytes, 5000)
        self.assertEqual(stats.smallest_bytes, 1)
        self.assertEqual(stats.average_bytes, (5000 + 1 + 4097) // 3)

    def test_empty_files_are_counted_separately(self):
        """Кластера не занимают, а запись MFT каждому всё равно нужна."""
        (self.first / "zero.bin").write_bytes(b"")
        stats = scan_paths([self.first], 4096).stats(4096)
        self.assertEqual(stats.empty_files, 1)

    def test_nothing_chosen_gives_zeroes_not_a_crash(self):
        stats = scan_paths([], 4096).stats(4096)
        self.assertEqual(stats.file_count, 0)
        self.assertEqual(stats.average_bytes, 0)


class CalcTabSourceTests(TreeFixture):
    def setUp(self):
        super().setUp()
        self.tab = CalcTab(default_models)

    def test_the_table_lists_every_source(self):
        self.tab._rescan([str(self.first), str(self.second / "c.bin")])
        self.assertEqual(self.tab.source_table.rowCount(), 2)
        self.assertEqual(
            self.tab.source_table.columnCount(), len(SOURCE_COLUMNS)
        )

    def test_the_full_path_lives_in_the_tooltip(self):
        """В ячейке имя: полный путь растянул бы столбец на весь экран."""
        self.tab._rescan([str(self.second / "c.bin")])
        self.assertEqual(self.tab.source_table.item(0, 0).text(), "c.bin")
        self.assertEqual(
            self.tab.source_table.item(0, 0).toolTip(), str(self.second / "c.bin")
        )

    def test_adding_keeps_what_was_already_chosen(self):
        self.tab._rescan([str(self.first)])
        self.tab._rescan(self.tab._current_paths() + [str(self.second / "c.bin")])
        self.assertEqual(self.tab.source_table.rowCount(), 2)
        self.assertEqual(self.tab.count_spin.value(), 3)

    def test_dropping_a_row_recounts_the_rest(self):
        self.tab._rescan([str(self.first), str(self.second / "c.bin")])
        self.tab.source_table.selectRow(0)
        self.tab._drop_sources()
        self.assertEqual(self.tab.source_table.rowCount(), 1)
        self.assertEqual(self.tab.count_spin.value(), 1)

    def test_dropping_everything_returns_to_manual_entry(self):
        self.tab._rescan([str(self.first)])
        self.tab.source_table.selectAll()
        self.tab._drop_sources()
        self.assertIsNone(self.tab._scan)
        self.assertFalse(self.tab.source_box.isVisibleTo(self.tab))

    def test_dropping_everything_zeroes_the_fields(self):
        """Иначе расчёт продолжает считать по снятому источнику.

        Подпись уже говорит «источник не выбран», а размер и Container init
        стоят прежние, и отличить это от ручного ввода нечем.
        """
        self.tab._rescan([str(self.first)])
        self.tab.source_table.selectAll()
        self.tab._drop_sources()
        self.assertEqual(self.tab.size_edit.text(), "")
        self.assertEqual(self.tab.count_spin.value(), 1)
        self.assertEqual(self.tab.result_label.text(), "—")

    def test_dropping_the_last_row_zeroes_the_fields_too(self):
        """Последняя строка убирается кнопкой так же, как все разом."""
        self.tab._rescan([str(self.second / "c.bin")])
        self.tab.source_table.selectRow(0)
        self.tab._drop_sources()
        self.assertEqual(self.tab.size_edit.text(), "")
        self.assertEqual(self.tab.count_spin.value(), 1)

    def test_the_table_hides_while_nothing_is_chosen(self):
        self.assertFalse(self.tab.source_box.isVisibleTo(self.tab))
        self.tab._rescan([str(self.first)])
        self.assertTrue(self.tab.source_box.isVisibleTo(self.tab))

    def test_drop_is_disabled_without_a_selection(self):
        self.tab._rescan([str(self.first)])
        self.tab.source_table.clearSelection()
        self.assertFalse(self.tab.drop_button.isEnabled())

    def test_changing_the_cluster_recounts_the_table(self):
        self.tab._rescan([str(self.first)])
        before = self.tab.source_table.item(0, 5).text()
        self.tab.cluster_combo.setCurrentText("65536")
        self.assertNotEqual(self.tab.source_table.item(0, 5).text(), before)

    def test_the_summary_names_folders_and_files(self):
        self.tab._rescan([str(self.first), str(self.second / "c.bin")])
        text = self.tab.source_label.text()
        for expected in ("папок", "файлов", "по кластерам", "хвост"):
            with self.subTest(expected):
                self.assertIn(expected, text)

    def test_manual_entry_empties_the_summary_again(self):
        self.tab._rescan([str(self.first)])
        self.tab._on_manual_edit()
        self.assertIn("вручную", self.tab.source_label.text())

    def test_all_sources_are_reported_not_just_the_first(self):
        self.tab._rescan([str(self.first), str(self.second)])
        self.assertEqual(
            self.tab.current_sources(), [str(self.first), str(self.second)]
        )
        self.tab._on_manual_edit()
        self.assertEqual(self.tab.current_sources(), [])

    def selected_rows(self):
        rows = self.tab.source_table.selectionModel().selectedRows()
        return sorted(index.row() for index in rows)

    def test_the_selection_buttons_cover_the_whole_table(self):
        self.tab._rescan([str(self.first), str(self.second / "c.bin")])
        self.tab.source_table_select_all()
        self.assertEqual(self.selected_rows(), [0, 1])
        self.tab.source_table_select_none()
        self.assertEqual(self.selected_rows(), [])

    def test_inverting_swaps_chosen_and_unchosen(self):
        """Построчный selectRow оставил бы одну строку: он сбрасывает прежнее."""
        self.tab._rescan([str(self.first), str(self.second / "c.bin")])
        self.tab.source_table.selectRow(0)
        self.tab.source_table_invert()
        self.assertEqual(self.selected_rows(), [1])
        self.tab.source_table_invert()
        self.assertEqual(self.selected_rows(), [0])

    def test_the_selection_buttons_hide_with_the_table(self):
        for button in self.tab.select_buttons:
            self.assertFalse(button.isVisibleTo(self.tab))
        self.tab._rescan([str(self.first)])
        for button in self.tab.select_buttons:
            self.assertTrue(button.isVisibleTo(self.tab))

    def test_the_statistics_are_available_to_the_rest_of_the_app(self):
        self.tab._rescan([str(self.first)])
        self.assertEqual(self.tab.current_stats().file_count, 2)
        self.tab._on_manual_edit()
        self.assertIsNone(self.tab.current_stats())


class DragAndDropTests(TreeFixture):
    """Бросок мышью — то же «Добавить…», только без диалога."""

    def setUp(self):
        super().setUp()
        self.tab = CalcTab(default_models)

    def _drop(self, *paths, text=""):
        """Собрать бросок и отдать его вкладке. Возвращает само событие."""
        mime = QMimeData()
        if paths:
            mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        if text:
            mime.setText(text)
        event = QDropEvent(
            QPoint(5, 5), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier
        )
        self.tab.dropEvent(event)
        return event

    def test_a_dropped_folder_becomes_a_source(self):
        event = self._drop(str(self.first))
        self.assertTrue(event.isAccepted())
        self.assertEqual(self.tab.source_table.rowCount(), 1)
        self.assertEqual(self.tab.count_spin.value(), 2)

    def test_a_drop_adds_instead_of_replacing(self):
        """Замена — это кнопка с диалогом; бросок не спрашивает ничего."""
        self.tab._rescan([str(self.first)])
        self._drop(str(self.second / "c.bin"))
        self.assertEqual(self.tab.source_table.rowCount(), 2)
        self.assertEqual(self.tab.count_spin.value(), 3)

    def test_a_url_without_a_file_is_thrown_away(self):
        """Ссылка из браузера приходит тем же mime-типом, что и файл."""
        mime = QMimeData()
        mime.setUrls(
            [QUrl.fromLocalFile(str(self.first)), QUrl("https://example.com/x")]
        )
        kept = self.tab._dropped_paths(mime)
        # QUrl отдаёт путь с прямыми слэшами; к обратным его приводит уже
        # unique_roots, через который проходит любой источник.
        self.assertEqual(
            [os.path.normcase(os.path.abspath(path)) for path in kept],
            [os.path.normcase(str(self.first))],
        )

    def test_a_drop_without_any_path_changes_nothing(self):
        event = self._drop(text="просто текст")
        self.assertFalse(event.isAccepted())
        self.assertIsNone(self.tab._scan)

    def test_no_input_field_swallows_the_drop(self):
        """QLineEdit принял бы бросок сам и вставил путь текстом в размер."""
        self.assertTrue(self.tab.acceptDrops())
        greedy = [
            field
            for field in self.tab.findChildren(QLineEdit)
            if field.acceptDrops()
        ]
        self.assertEqual(greedy, [])


class PathPickerTests(unittest.TestCase):
    """Кнопка выбора одна на файлы и папки — иначе делить их нечем."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        (self.root / "folder").mkdir()
        (self.root / "file.bin").write_bytes(b"\0" * 10)
        hidden = self.root / "secret.bin"
        hidden.write_bytes(b"\0" * 10)
        ctypes.windll.kernel32.SetFileAttributesW(str(hidden), 0x2)
        self.picker = PathPicker(directory=str(self.root))

    def tearDown(self):
        self.picker.deleteLater()
        self._dir.cleanup()

    def test_it_does_not_use_the_native_dialog(self):
        """Родные диалоги Windows умеют либо файлы, либо одну папку."""
        self.assertTrue(self.picker.testOption(PathPicker.DontUseNativeDialog))

    def test_a_folder_is_accepted_instead_of_entered(self):
        target = str(self.root / "folder")
        self.picker.selectedFiles = lambda: [target]
        self.picker.accept()
        self.assertEqual(self.picker.result(), QDialog.Accepted)
        self.assertEqual(self.picker.chosen_paths(), [target])

    def test_files_and_folders_come_back_together(self):
        chosen = [str(self.root / "folder"), str(self.root / "file.bin")]
        self.picker.selectedFiles = lambda: chosen
        self.picker.accept()
        self.assertEqual(self.picker.chosen_paths(), chosen)

    def test_an_empty_selection_leaves_the_dialog_open(self):
        self.picker.selectedFiles = lambda: []
        self.picker.accept()
        self.assertNotEqual(self.picker.result(), QDialog.Accepted)

    def test_a_vanished_path_is_dropped(self):
        self.picker.selectedFiles = lambda: [str(self.root / "gone")]
        self.picker.accept()
        self.assertNotEqual(self.picker.result(), QDialog.Accepted)

    def test_the_mouse_draws_a_rubber_band_instead_of_dragging(self):
        """Qt ставит видам диалога InternalMove, и протяжка начинает перенос.

        С ним выделить мышью несколько имён нельзя вовсе — остаются только Ctrl
        и Shift, а это и выглядит как «мышкой не выделяется».
        """
        for name in FILE_VIEWS:
            view = self.picker.findChild(QAbstractItemView, name)
            with self.subTest(name):
                self.assertIsNotNone(view)
                self.assertFalse(view.dragEnabled())
                self.assertEqual(view.dragDropMode(), QAbstractItemView.NoDragDrop)
                self.assertEqual(
                    view.selectionMode(), QAbstractItemView.ExtendedSelection
                )

    def test_the_sidebar_keeps_its_drag(self):
        """В боковую панель перетаскиванием складывают закладки."""
        sidebar = self.picker.findChild(QAbstractItemView, "sidebar")
        self.assertIsNotNone(sidebar)
        self.assertTrue(sidebar.dragEnabled())

    def test_select_all_and_none_cover_the_folder(self):
        view = self.picker._active_view()
        _app.processEvents()
        self.picker._select_all()
        chosen = {index.row() for index in view.selectionModel().selectedIndexes()}
        self.assertEqual(len(chosen), view.model().rowCount(view.rootIndex()))
        self.picker._select_none()
        self.assertEqual(view.selectionModel().selectedIndexes(), [])

    def test_inverting_leaves_everything_but_the_chosen(self):
        view = self.picker._active_view()
        _app.processEvents()
        model = view.model()
        total = model.rowCount(view.rootIndex())
        view.selectionModel().select(
            model.index(0, 0, view.rootIndex()),
            QItemSelectionModel.Select | QItemSelectionModel.Rows,
        )
        self.picker._invert_selection()
        rows = {index.row() for index in view.selectionModel().selectedIndexes()}
        self.assertEqual(rows, set(range(1, total)))

    def test_hidden_files_appear_only_with_the_checkbox(self):
        view = self.picker._active_view()
        _app.processEvents()
        model = view.model()
        visible = model.rowCount(view.rootIndex())
        self.assertFalse(self.picker.show_hidden)

        self.picker.hidden_check.setChecked(True)
        _app.processEvents()
        self.assertTrue(self.picker.show_hidden)
        self.assertEqual(model.rowCount(view.rootIndex()), visible + 1)

    def test_the_context_menu_and_the_checkbox_are_one_switch(self):
        """У QFileDialog тот же переключатель есть в контекстном меню списка.

        Фильтр он правит по triggered, а галочка слушала бы toggled — тот
        приходит раньше, видит ещё старое состояние и переключает пункт второй
        раз: нажатие в меню не делало бы ничего.
        """
        action = self.picker._hidden_action
        self.assertIsNotNone(action)
        action.trigger()
        self.assertTrue(self.picker.show_hidden)
        self.assertTrue(self.picker.hidden_check.isChecked())
        action.trigger()
        self.assertFalse(self.picker.show_hidden)
        self.assertFalse(self.picker.hidden_check.isChecked())

    def test_the_checkbox_starts_where_it_was_left(self):
        """Диалог живёт один показ; состояние приходит снаружи и уходит наружу."""
        opened = PathPicker(directory=str(self.root), show_hidden=True)
        self.assertTrue(opened.show_hidden)
        self.assertTrue(opened.hidden_check.isChecked())
        opened.deleteLater()


if __name__ == "__main__":
    unittest.main()
