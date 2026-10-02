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
from containerhelper.factory import factory_data  # noqa: E402
from containerhelper.model import MIB  # noqa: E402
from containerhelper.records import Record  # noqa: E402
from containerhelper.ui.app import MainWindow  # noqa: E402
from containerhelper.ui.chart_window import (  # noqa: E402
    CHART_CALC,
    CHART_FORECAST,
    CHART_NTFS,
    CHART_SLACK,
)
from containerhelper.ui.collect_dialog import PATH_KEY, CollectDialog  # noqa: E402
from containerhelper.ui.measure_dialog import MeasureDialog  # noqa: E402
from containerhelper.ui.path_picker import PathPicker  # noqa: E402
from containerhelper.ui.record_dialog import RecordDialog  # noqa: E402
from tests.test_veracrypt import make_install  # noqa: E402

MEASURE = "containerhelper.ui.measure_dialog"

CYRILLIC = re.compile("[А-Яа-яЁё]")


def texts(top: QWidget):
    """Every piece of UI text of a window, with the kind of widget it sits in.

    Not with its position: a refresh rebuilds the widgets in table cells, and
    the order of children changes with it. Table cells are left out: they show
    data, and record names stay in the language they were made in.
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
        if isinstance(widget, QComboBox) and not widget.isEditable():
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


def switch(code: str, windows) -> None:
    """What the language list will do: switch, then tell every window."""
    i18n.set_language(code)
    for window in windows:
        QCoreApplication.sendEvent(window, QEvent(QEvent.Type.LanguageChange))
    _app.processEvents()


class LiveSwitchTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.set_language, i18n.current())
        folder = Path(tempfile.mkdtemp())
        self.window = MainWindow(data_dir=folder)
        self.window.records_tab.report_error = lambda *_: None
        self.window.show()
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
        for window in reversed(self.windows):
            window.close()

    def shown(self) -> Counter:
        """The texts of every window with how often each occurs."""
        return Counter(
            (number, kind, text)
            for number, window in enumerate(self.windows)
            for kind, text in texts(window)
            if text
        )

    def test_english_leaves_no_russian(self):
        """Data keeps its language: the folder path in the picker, and the
        factory measurements' own description, which is data until it
        becomes catalog keys."""
        factory = factory_data()
        data = (str(Path.cwd()), factory.source, factory.note)
        switch("en", self.windows)
        russian = []
        for number, kind, text in self.shown():
            for piece in data:
                text = text.replace(piece, "")
            if CYRILLIC.search(text):
                russian.append((number, kind, text))
        self.assertEqual(russian, [])

    def test_a_switch_leaves_the_collection_progress_alone(self):
        """A collection may be running: the bar is not the scope's to reset."""
        self.collect.progress.setMaximum(100)
        self.collect.progress.setValue(40)
        switch("en", self.windows)
        self.assertEqual(
            (self.collect.progress.maximum(), self.collect.progress.value()), (100, 40)
        )

    def test_a_button_s_message_is_said_again(self):
        """The check of the copied data is not repeated on a switch: a lost
        mismatch warning would let a spoilt record be saved."""
        self.record._take_payload()  # no Calculation tab: a message, not data
        switch("en", self.windows)
        self.assertIn(i18n.tr("record.payload.none"), self.record.issues_label.text())
        QTest.keyClicks(self.record.id_edit, "x")  # an edit clears it
        self.assertNotIn(i18n.tr("record.payload.none"), self.record.issues_label.text())

    def test_switching_back_restores_every_text(self):
        before = self.shown()
        switch("en", self.windows)
        switch("ru", self.windows)
        after = self.shown()
        self.assertEqual((before - after, after - before), (Counter(), Counter()))


if __name__ == "__main__":
    unittest.main()
