"""A language switch reaches every label without reopening a window."""

import os
import re
from collections import Counter
import tempfile
import unittest
from unittest import mock
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from shiboken6 import isValid  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTabWidget,
    QWidget,
)

_app = QApplication.instance() or QApplication([])

from containerhelper import i18n  # noqa: E402
from containerhelper.model import MIB  # noqa: E402
from containerhelper.records import Record  # noqa: E402
from containerhelper.ui.app import LANGUAGE_KEY, MainWindow, language_name  # noqa: E402
from containerhelper.ui.chart_window import (  # noqa: E402
    CHART_CALC,
    CHART_FORECAST,
    CHART_NTFS,
    CHART_SLACK,
)
from containerhelper.ui.collect_dialog import PATH_KEY, CollectDialog  # noqa: E402
from containerhelper.ui.language import _qt_translator, apply_language  # noqa: E402
from containerhelper.ui.measure_dialog import MeasureDialog  # noqa: E402
from containerhelper.ui.path_picker import PathPicker  # noqa: E402
from containerhelper.ui.record_dialog import RecordDialog  # noqa: E402
from tests.test_veracrypt import make_install  # noqa: E402

MEASURE = "containerhelper.ui.measure_dialog"

CYRILLIC = re.compile("[А-Яа-яЁё]")


def texts(top: QWidget):
    """Every piece of UI text of a window, with the kind of widget it sits in.

    Not with its position: a refresh rebuilds the widgets in table cells, and
    the order of children changes with it. Table cells are in: the names of
    collected and factory records are built when shown, and switch with the
    rest (decision 9). The language list is left out: each name there is in
    its own language, whatever the current one.
    """
    # Cell widgets replaced by a refresh are only scheduled for deletion;
    # until then they are still children, in the old language.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    for widget in [top, *top.findChildren(QWidget)]:
        kind = f"{type(widget).__name__}:{widget.objectName()}"
        yield kind + ":tip", widget.toolTip()
        if widget.isWindow():
            yield kind + ":title", widget.windowTitle()
        if isinstance(widget, (QLabel, QAbstractButton)):
            yield kind + ":text", widget.text()
        if isinstance(widget, QLineEdit):
            yield kind + ":placeholder", widget.placeholderText()
        if isinstance(widget, QGroupBox):
            yield kind + ":group", widget.title()
        if (
            isinstance(widget, QComboBox)
            and not widget.isEditable()
            and widget.objectName() != "language"
        ):
            for item in range(widget.count()):
                yield kind + ":item", widget.itemText(item)
        if isinstance(widget, QTabWidget):
            for tab in range(widget.count()):
                yield kind + ":tab", widget.tabText(tab)
                yield kind + ":tab:tip", widget.tabToolTip(tab)
        if isinstance(widget, QTableWidget):
            for column in range(widget.columnCount()):
                item = widget.horizontalHeaderItem(column)
                if item is not None:
                    yield kind + ":column", item.text()
                    yield kind + ":column:tip", item.toolTip()
            for row in range(widget.rowCount()):
                for column in range(widget.columnCount()):
                    item = widget.item(row, column)
                    if item is not None:
                        yield kind + ":cell", item.text()
                        yield kind + ":cell:tip", item.toolTip()


def switch(code: str) -> None:
    """What the language list does, with Qt's queue emptied after it."""
    apply_language(code)
    _app.processEvents()


def dispose(*windows: QWidget) -> None:
    """Delete windows now: a closed one left alive hears every later switch
    of the suite and reads whatever its substitutes no longer cover."""
    for window in windows:
        if not isValid(window):  # disposed of by the test itself
            continue
        window.close()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class LiveSwitchTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(apply_language, i18n.current())
        folder = Path(tempfile.mkdtemp())
        self.window = MainWindow(data_dir=folder)
        self.window.records_tab.report_error = lambda *_: None
        self.window.show()
        # Collected measurements, as collection hands them over: no name and
        # no note written, both built when shown, in the language of the
        # moment (a point and a file set; the second is a point as well).
        for fileset in ("", "small-500"):
            self.window.records_tab.store_calibration_point(
                Record(
                    id="",
                    container_mib=1536 if fileset else 1024,
                    mounted_bytes=(1536 if fileset else 1024) * MIB - 266_240,
                    empty_free_bytes=(1536 if fileset else 1024) * MIB - 20 * MIB,
                    file_bytes=500 * 1024 if fileset else None,
                    file_count=500 if fileset else None,
                    left_bytes=1400 * MIB if fileset else None,
                    fileset=fileset,
                    veracrypt="1.26.24",
                )
            )
        # A calculation to explain: the safety advice names the records near
        # it, factory ones among them, whose names are built in a language.
        self.window.calc_tab.size_edit.setText("700 000 000")
        self.window.calc_tab.recalculate()
        for key in (CHART_NTFS, CHART_SLACK, CHART_FORECAST, CHART_CALC):
            self.window.open_chart(key)
        charts = self.window._charts[CHART_NTFS]
        charts.detach(1)
        self.record = RecordDialog(
            Record(id="r", container_mib=1024), parent=self.window
        )
        self.picker = PathPicker(self.window)
        # The volume is read again on every switch: the substitutes stay on.
        for name, value in (
            ("mounted_drives", ["X:\\"]),
            ("volume_usage", (1024 * MIB, 512 * MIB)),
            ("volume_filesystem", "exFAT"),
            ("cluster_size", 32768),
        ):
            patcher = mock.patch(f"{MEASURE}.{name}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.measure = MeasureDialog(self.window, prompt_key="record.prompt.left")
        settings = QSettings(str(folder / "collect.ini"), QSettings.IniFormat)
        install = make_install(folder / "VeraCrypt", "VeraCrypt Format.exe", "VeraCrypt.exe")
        settings.setValue(PATH_KEY, str(install))
        # The free space of a real disk drifts between two readings.
        with mock.patch.object(CollectDialog, "free_bytes", return_value=64 * MIB):
            self.collect = CollectDialog(sizes=(1, 2, 4), settings=settings)
        self.collect.free_bytes = lambda: 64 * MIB
        self.windows = [
            self.window,
            *self.window._charts.values(),
            *charts._windows.values(),
            self.record,
            self.picker,
            self.measure,
            self.collect,
        ]
        for window in self.windows:
            window.show()
        _app.processEvents()

    def tearDown(self):
        # The parentless ones; the rest go with the main window.
        dispose(self.collect, self.window)

    def shown(self) -> Counter:
        """The texts of every window with how often each occurs."""
        return Counter(
            (number, kind, text)
            for number, window in enumerate(self.windows)
            for kind, text in texts(window)
            if text
        )

    def test_no_language_leaves_russian_behind(self):
        """Data keeps its language: the folder path in the picker."""
        data = (str(Path.cwd()),)
        for code in i18n.LANGUAGES:
            if code == "ru":
                continue
            with self.subTest(code=code):
                switch(code)
                russian = []
                for number, kind, text in self.shown():
                    for piece in data:
                        text = text.replace(piece, "")
                    if CYRILLIC.search(text):
                        russian.append((number, kind, text))
                self.assertEqual(russian, [])

    def test_every_header_fits_after_a_switch(self):
        """The widths are fitted once, in the first language; a longer header
        in the next one showed only its middle."""
        for code in i18n.LANGUAGES:
            with self.subTest(code=code):
                switch(code)
                cut = [
                    (name, column, table.horizontalHeaderItem(column).text())
                    for name, table in self.window.all_tables().items()
                    for column in range(table.columnCount())
                    if table.horizontalHeader().sectionSize(column)
                    < table.horizontalHeader().sectionSizeHint(column)
                ]
                self.assertEqual(cut, [])

    def test_a_column_dragged_narrow_stays_until_its_header_changes(self):
        """A refresh with the same header is no reason to undo a drag."""
        tab = self.window.records_tab
        header = tab.table.horizontalHeader()
        column = 2  # «Ёмкость тома» / "Volume capacity": the text changes
        narrow = header.minimumSectionSize()
        header.resizeSection(column, narrow)
        tab.set_unit(tab._unit)
        self.assertEqual(header.sectionSize(column), narrow)
        switch("en")
        self.assertGreaterEqual(
            header.sectionSize(column), header.sectionSizeHint(column)
        )

    def test_a_picked_point_survives_a_switch(self):
        """Found again by its key, not its name: a collected point's name is
        built in the language of the moment, and the line describing it
        follows the language instead of emptying."""
        window = self.window._charts[CHART_NTFS]
        point = next(
            point
            for view in window.views
            for series in view.chart().series
            for point in series.points
            if isinstance(point.key, tuple) and point.key[2] == 1024 and not point.key[3]
        )
        window._on_picked(point.key, point.tip)
        before = window.detail.text()
        switch("en")
        self.assertEqual(window._picked, point.key)
        self.assertTrue(window.detail.text())
        self.assertNotEqual(window.detail.text(), before)
        self.assertFalse(CYRILLIC.search(window.detail.text()), window.detail.text())

    def test_a_switch_leaves_the_collection_progress_alone(self):
        """A collection may be running: the bar is not the scope's to reset."""
        self.collect.progress.setMaximum(100)
        self.collect.progress.setValue(40)
        switch("en")
        self.assertEqual(
            (self.collect.progress.maximum(), self.collect.progress.value()), (100, 40)
        )

    def test_a_button_s_message_is_said_again(self):
        """The check of the copied data is not repeated on a switch: a lost
        mismatch warning would let a spoilt record be saved."""
        self.record._take_payload()  # no Calculation tab: a message, not data
        switch("en")
        self.assertIn(i18n.tr("record.payload.none"), self.record.issues_label.text())
        QTest.keyClicks(self.record.id_edit, "x")  # an edit clears it
        self.assertNotIn(i18n.tr("record.payload.none"), self.record.issues_label.text())

    def test_switching_back_restores_every_text(self):
        before = self.shown()
        switch("en")
        switch("ru")
        after = self.shown()
        self.assertEqual((before - after, after - before), (Counter(), Counter()))

    def test_each_window_hears_of_a_switch_once(self):
        """Qt tells a window with a parent twice: as a window and through its
        parent. Twice is the charts rebuilt and the volume read twice."""
        counted = {}
        for number, window in enumerate(self.windows):
            counted[number] = mock.patch.object(
                window, "retranslate", wraps=window.retranslate
            ).start()
        self.addCleanup(mock.patch.stopall)
        switch("en")
        calls = {number: spy.call_count for number, spy in counted.items()}
        self.assertEqual(calls, {number: 1 for number in counted})

    def test_every_language_brings_qt_s_catalog(self):
        """A language without it shows Qt's dialogs in English."""
        for code in i18n.LANGUAGES:
            with self.subTest(code=code):
                self.assertIsNotNone(_qt_translator(code))

    def test_qt_s_own_captions_follow(self):
        """The stock buttons and labels of Qt's dialogs come from Qt's own
        catalog; without it they stayed English in Russian windows."""
        look_in = lambda: QCoreApplication.translate("QFileDialog", "Look in:")
        switch("ru")
        self.assertTrue(CYRILLIC.search(look_in()), look_in())
        switch("en")
        self.assertEqual(look_in(), "Look in:")


class LanguageChoiceTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(apply_language, i18n.current())
        self.folder = Path(tempfile.mkdtemp())

    def open(self) -> MainWindow:
        window = MainWindow(data_dir=self.folder)
        self.addCleanup(dispose, window)
        return window

    def test_a_start_retranslates_once(self):
        """Qt posts the new window an event for the catalog it installs at
        start; built in that language, the window has nothing to redo."""
        with mock.patch.object(MainWindow, "retranslate", autospec=True) as spy:
            window = self.open()
            window.show()
            _app.processEvents()
        self.assertEqual(spy.call_count, 1)

    def test_an_unknown_stored_code_keeps_the_language(self):
        window = self.open()
        window.settings.setValue(LANGUAGE_KEY, "xx")
        dispose(window)
        again = self.open()
        self.assertEqual(i18n.current(), "ru")
        self.assertEqual(again.language_combo.currentData(), "ru")
        look_in = QCoreApplication.translate("QFileDialog", "Look in:")
        self.assertTrue(CYRILLIC.search(look_in), look_in)

    def test_an_unread_language_is_marked_in_itself(self):
        reviewed = i18n.meta
        draft = lambda code: {**reviewed(code), "status": "draft"}
        with mock.patch("containerhelper.ui.app.meta", side_effect=draft):
            self.assertEqual(language_name("ru"), "Русский (бета)")
            self.assertEqual(language_name("en"), "English (beta)")
        self.assertEqual(language_name("ru"), "Русский")

    def test_the_list_switches_and_stores_the_code(self):
        window = self.open()
        combo = window.language_combo
        combo.setCurrentIndex(combo.findData("en"))
        self.assertEqual(i18n.current(), "en")
        self.assertEqual(window.windowTitle(), i18n.tr("app.title"))
        self.assertEqual(window.settings.value(LANGUAGE_KEY), "en")

    def test_the_stored_language_opens_the_next_launch(self):
        window = self.open()
        window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
        dispose(window)
        apply_language("ru")
        again = self.open()
        self.assertEqual(i18n.current(), "en")
        self.assertEqual(again.language_combo.currentData(), "en")
        self.assertFalse(CYRILLIC.search(again.windowTitle()))

    def test_without_a_stored_choice_the_language_stays(self):
        window = self.open()
        self.assertEqual(window.language_combo.currentData(), i18n.current())

    def test_names_are_in_their_own_language(self):
        """Whoever cannot read the current language has to find theirs."""
        window = self.open()
        combo = window.language_combo
        names = {combo.itemData(i): combo.itemText(i) for i in range(combo.count())}
        apply_language("en")
        again = {combo.itemData(i): combo.itemText(i) for i in range(combo.count())}
        self.assertEqual(names, again)
        self.assertEqual(names["ru"], i18n.meta("ru")["name"])


class GuardTests(unittest.TestCase):
    def test_every_window_class_drops_a_repeated_switch(self):
        """A window class without the guard hears every switch up to four
        times; nothing on screen shows it, only the work done four times."""
        import ast

        missing = []
        for path in sorted((Path(__file__).parent.parent / "containerhelper" / "ui").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                source = ast.unparse(node)
                bases = {ast.unparse(base) for base in node.bases}
                window = bases & {"QDialog", "QMainWindow", "QFileDialog"} or (
                    "Qt.Window" in source and "super().__init__" in source
                )
                if window and "repeated_change(self, event)" not in source:
                    missing.append(f"{path.name}: {node.name}")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
