"""Dialog for automatic collection of measurements through VeraCrypt.

The collection logic lives in `collect.py` and knows nothing about Qt. Here is
only what the dialog exists for: find VeraCrypt (and ask if it is not found),
show what is going on, and let the user stop it.

The steps run in a separate thread. Creating and mounting a container takes
seconds, minutes with a full format, and writing a four-gigabyte file set
longer still; in the window's thread all of this would freeze it solid, and
there would be no way to press Stop.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence

from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..collect import (
    Collector,
    Progress,
    Step,
    StepResult,
    free_space,
    plan,
    required_bytes,
    slack_plan,
    slack_step,
    total_bytes,
    weight_bytes,
)
from ..elevation import is_admin, relaunch_as_admin
from ..fileset import FileSet
from ..formatting import UNIT_AUTO, fmt_both, fmt_with_unit
from ..i18n import tr, tr_n
from ..model import MIB, CopySlackModel, MetadataModel, SafetyModel
from ..paths import is_writable
from ..records import UNREAD_VERSION
from .language import repeated_change
from .table import scrollable, wrapped
from ..veracrypt import (
    FORMAT_NAMES,
    MOUNT_NAMES,
    VeraCrypt,
    find_install,
    install_at,
    missing_report,
    version_notice,
)

#: Keys in settings.ini. Only a VeraCrypt path chosen by hand is remembered:
#: the standard locations are searched anew every time, and there is no point
#: remembering them.
PATH_KEY = "veracrypt/path"
WORKDIR_KEY = "veracrypt/workdir"

def _not_found() -> str:
    return tr(
        "collect.dialog.not_found",
        format_exe=FORMAT_NAMES[0],
        mount_exe=MOUNT_NAMES[0],
    )


#: Line break in tooltips. A constant, as in the other tabs: escaping inside
#: edit templates has already collapsed once.
LINE_BREAK = chr(10)

#: How often progress is sent to the window. A set of ten thousand files would
#: send ten thousand signals across the thread boundary and clog the event
#: queue before the window could paint them.
PROGRESS_INTERVAL = 0.1

#: The share of the way after which the remaining-time estimate stops being a
#: guess. Before it, the speed is measured over the first seconds of creating
#: a container and is off several times over.
ETA_AFTER_PERCENT = 5

#: Below this the dialog's content stops being readable: the file set labels
#: collapse and the row of buttons under them slides off the edge. Past it,
#: horizontal scrolling kicks in — the same solution as in the main window's
#: tabs.
MIN_CONTENT_WIDTH = 640

#: Indent of the labels under a check box — the same number as the check
#: boxes themselves.
NOTE_INDENT = 20

#: How often the window repaints the clock on its own, in milliseconds. Time
#: used to move only together with progress, and progress comes from the
#: step: during a full format and during mounting there is none at all, and
#: the clock stood still for minutes — the only sign that the program was
#: alive froze exactly when it was needed.
CLOCK_INTERVAL = 1000

ModelProvider = Callable[[], "tuple[MetadataModel, CopySlackModel]"]
SafetyProvider = Callable[[], SafetyModel]


def _clock(seconds: float) -> str:
    """Seconds as "m:ss" or "h:mm:ss" — the way the eye reads them."""
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class CollectWorker(QObject):
    """Runs the steps in its own thread and reports on them through signals."""

    stepStarted = Signal(int, str)
    stepFinished = Signal(object)
    #: The step number and what is happening inside it. Throttling is here
    #: too: without it there would be as many signals as files in the set.
    stepProgress = Signal(int, object)
    note = Signal(str)
    #: Empty — ran to the end. Otherwise the reason it stopped.
    done = Signal(str)

    def __init__(self, collector: Collector) -> None:
        super().__init__()
        self.collector = collector
        self._stop = False
        self._index = 0
        self._last_sent = 0.0
        self._last_phase = ""
        collector.progress = self._on_progress
        collector.should_stop = self.stopping

    def stop(self) -> None:
        """Stop the collection.

        Between steps — at once. Inside a step the cancel reaches down into
        the writing of the file set: four gigabytes take minutes to write, and
        there is no point waiting for the end, the unfinished set is thrown
        away together with the container anyway. But a running VeraCrypt
        cannot be interrupted without leaving behind a mounted volume and a
        terabyte file — creation and formatting are carried through to the
        end.
        """
        self._stop = True

    def stopping(self) -> bool:
        return self._stop

    def _on_progress(self, progress: Progress) -> None:
        now = time.monotonic()
        if progress.phase == self._last_phase and now - self._last_sent < PROGRESS_INTERVAL:
            return
        self._last_sent = now
        self._last_phase = progress.phase
        self.stepProgress.emit(self._index, progress)

    def run(self) -> None:
        reason = ""
        try:
            removed = self.collector.prepare()
            if removed:
                self.note.emit(
                    tr("collect.dialog.log.removed", count=len(removed))
                )

            for index, step in enumerate(self.collector.steps):
                if self._stop:
                    reason = tr("collect.dialog.log.stopped")
                    break
                self._index = index
                self._last_phase = ""
                self.stepStarted.emit(index, step.title)
                result = self.collector.run_step(step)
                self.stepFinished.emit(result)
                if result.fatal:
                    reason = result.error
                    break
        finally:
            # The signal must fire even on an unexpected error: otherwise the
            # window stays "running" forever, and there is no way to close it.
            self.done.emit(reason)


class CollectDialog(QDialog):
    """Find VeraCrypt, plan the collection and carry it out."""

    #: A measurement just taken. It goes out at once, one at a time: the
    #: collection runs for hours, and a crash halfway must not cost everything
    #: already measured.
    pointMeasured = Signal(object)
    #: The elevated restart has happened — the window must close, otherwise
    #: two copies will write into one data folder.
    relaunchRequested = Signal()

    def __init__(
        self,
        sizes: Sequence[int],
        covered: Sequence[int] = (),
        settings=None,
        data_dir: Path | None = None,
        parent: QWidget | None = None,
        filesets: Sequence[FileSet] = (),
        slack_covered: Sequence[str] = (),
        models: ModelProvider | None = None,
        safety: SafetyProvider | None = None,
        forbidden_sizes: Sequence[int] = (),
    ) -> None:
        super().__init__(parent)
        self.setMinimumWidth(660)

        self._sizes = tuple(sizes)
        self._covered = tuple(covered)
        self._filesets = tuple(filesets)
        #: Keys of the file sets that already have an own measurement. Keys,
        #: not file counts: two sets with n = 1 differ in size, and both
        #: must be measured — comparing them is what checks that copy slack
        #: does not depend on file size.
        self._slack_covered = tuple(slack_covered)
        self._models = models
        self._safety = safety
        #: Sizes a copy-slack measurement must not land on: they are rows of
        #: the coverage table, and the Use factory button in such a row would
        #: disable the copy-slack measurement along with it.
        self._forbidden = tuple(forbidden_sizes)
        #: The steps the scope text was last built from. Read only by the
        #: constructor, right after retranslate built them.
        self._scope_steps: Sequence[Step] = ()
        self._settings = settings
        self._data_dir = data_dir
        #: Set before the widgets are built: redrawing the collection scope
        #: asks for free space, and that happens already during the VeraCrypt
        #: search.
        self._workdir = Path(tempfile.gettempdir())
        self._install = None
        #: Whether the version found is good enough for collection: a very
        #: old one lacks switches the collection cannot run without.
        self._supported = False
        self._thread: QThread | None = None
        self._worker: CollectWorker | None = None
        self._measured = 0
        self._failed = 0
        self._skipped = 0
        #: The window was asked to close mid-collection: it closes when the
        #: step ends.
        self._closing = False
        #: Step weights and their running sum — the denominator and the
        #: reference points for the progress bar.
        self._weights: list[int] = []
        self._before: list[int] = [0]
        self._total_weight = 0
        self._started = 0.0
        self._index = 0
        #: What is running now and since which second. The phase is kept
        #: apart from the detail: "writing files" lasts for minutes, while the
        #: number of bytes written changes ten times a second, and the phase
        #: clock would reset along with it.
        self._phase = ""
        self._detail = ""
        self._phase_started = 0.0
        #: The largest share of the current step reached. The bar has no
        #: right to move backwards.
        self._share = 0.0

        #: The window's clock. It runs on its own, not from progress: see
        #: CLOCK_INTERVAL.
        self._clock = QTimer(self)
        self._clock.setInterval(CLOCK_INTERVAL)
        self._clock.timeout.connect(self._show_progress)

        #: Replaceable handlers: a modal window in the middle of the logic
        #: cannot be closed from a test, and an elevated restart cannot be
        #: undone.
        self.report_error = self._show_error
        self.confirm = self._ask_confirmation
        self.ask_directory = self._ask_directory
        self.find_install = find_install
        self.is_admin = is_admin
        self.relaunch = relaunch_as_admin
        self.make_veracrypt = VeraCrypt

        # Everything except the buttons lives under scrolling. The window asks
        # for 1178 pixels of height — more than the screen has — and without
        # scrolling Qt squashes the labels: the line with a file set's cost
        # got zero height, and the text vanished entirely. The buttons stay
        # outside: Stop must be at hand without scrolling the window.
        content = QWidget()
        inner = QVBoxLayout(content)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(self._build_intro())
        inner.addWidget(self._build_install_box())
        inner.addWidget(self._build_workdir_box())
        inner.addWidget(self._build_scope_box())
        inner.addWidget(self._build_rights_box())
        inner.addWidget(self._build_progress())

        layout = QVBoxLayout(self)
        layout.addWidget(scrollable(content, MIN_CONTENT_WIDTH), 1)
        layout.addLayout(self._build_buttons())
        self.resize(760, 820)

        self._restore_install()
        self._restore_workdir()
        # The text half of the scope is built by retranslate, from the same
        # steps: worked out a second time, they would cost a second pass of
        # the models over the whole plan for nothing.
        self.retranslate()
        self._refresh_scope_controls(self._scope_steps)

    def retranslate(self) -> None:
        """Set the static text in the current language and rebuild the text
        made from data.

        The scope is rebuilt as text only: `_refresh_scope` also resets the
        progress bar, and a language switch mid-collection must not do that.
        Lines already in the log stay as written.
        """
        self.setWindowTitle(tr("collect.dialog.title"))
        self.intro_label.setText(tr("collect.dialog.intro"))
        self.install_button.setText(tr("collect.dialog.install.button"))
        self.install_button.setToolTip(
            tr(
                "collect.dialog.install.button.tip",
                format_exe=FORMAT_NAMES[0],
                mount_exe=MOUNT_NAMES[0],
            )
        )
        self.version_label.setToolTip(tr("collect.dialog.version.tip"))
        self.workdir_group.setTitle(tr("collect.dialog.workdir.group"))
        self.workdir_label.setToolTip(tr("collect.dialog.workdir.tip"))
        self.workdir_button.setText(tr("collect.dialog.workdir.button"))
        self.space_label.setToolTip(tr("collect.dialog.space.tip"))
        self.scope_group.setTitle(tr("collect.dialog.scope.group"))
        self.want_ntfs.setText(tr("collect.dialog.ntfs"))
        self.want_ntfs.setToolTip(tr("collect.dialog.ntfs.tip"))
        self.want_slack.setText(tr("collect.dialog.slack"))
        self.want_slack.setToolTip(tr("collect.dialog.slack.tip"))
        self.everything.setToolTip(tr("collect.dialog.everything.tip"))
        self.with_self_check.setText(tr("collect.dialog.self_check"))
        self.with_self_check.setToolTip(tr("collect.dialog.self_check.tip"))
        for item in self._filesets:
            self.fileset_boxes[item.key].setText(tr(item.title))
        for button, title, tip in self._mark_buttons:
            button.setText(tr(title))
            button.setToolTip(tr(tip))
        self.rights_group.setTitle(tr("collect.dialog.rights.group"))
        self.elevate_button.setText(tr("collect.dialog.elevate"))
        self.elevate_button.setToolTip(tr("collect.dialog.elevate.tip"))
        self.progress.setToolTip(tr("collect.dialog.progress.tip"))
        self.progress_label.setToolTip(tr("collect.dialog.status.tip"))
        self.log.setToolTip(tr("collect.dialog.log.tip"))
        self.start_button.setText(tr("collect.dialog.start"))
        self.stop_button.setText(tr("collect.dialog.stop"))
        self.stop_button.setToolTip(tr("collect.dialog.stop.tip"))
        self.close_button.setText(tr("collect.dialog.close"))

        self._show_install()
        self._show_workdir()
        self._refresh_rights()
        self._refresh_scope_text(self.steps())
        if self.running():
            self._show_progress()

    def event(self, event) -> bool:
        if repeated_change(self, event):
            return True
        return super().event(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    # --- building ----------------------------------------------------------

    def _build_intro(self) -> QLabel:
        text = QLabel()
        text.setWordWrap(True)
        text.setStyleSheet("color: palette(mid);")
        self.intro_label = text
        return text

    def _build_install_box(self) -> QGroupBox:
        group = QGroupBox("VeraCrypt")
        outer = QVBoxLayout(group)
        row = QHBoxLayout()
        outer.addLayout(row)

        self.install_label = QLabel()
        self.install_label.setWordWrap(True)
        self.install_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self.install_label, 1)

        self.install_button = QPushButton()
        self.install_button.clicked.connect(self._choose_install)
        row.addWidget(self.install_button)

        self.version_label = QLabel()
        self.version_label.setWordWrap(True)
        self.version_label.setStyleSheet("color: palette(mid);")
        outer.addWidget(self.version_label)
        return group

    def _build_workdir_box(self) -> QGroupBox:
        group = QGroupBox()
        self.workdir_group = group
        outer = QVBoxLayout(group)
        row = QHBoxLayout()
        outer.addLayout(row)

        self.workdir_label = QLabel()
        self.workdir_label.setWordWrap(True)
        self.workdir_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self.workdir_label, 1)

        self.workdir_button = QPushButton()
        self.workdir_button.clicked.connect(self._choose_workdir)
        row.addWidget(self.workdir_button)

        self.space_label = QLabel()
        self.space_label.setWordWrap(True)
        self.space_label.setStyleSheet("color: palette(mid);")
        outer.addWidget(self.space_label)
        return group

    def _build_scope_box(self) -> QGroupBox:
        group = QGroupBox()
        self.scope_group = group
        layout = QVBoxLayout(group)

        self.want_ntfs = QCheckBox()
        self.want_ntfs.setChecked(True)
        self.want_ntfs.toggled.connect(self._refresh_scope)
        layout.addWidget(self.want_ntfs)
        layout.addWidget(self._build_ntfs_options())

        self.want_slack = QCheckBox()
        self.want_slack.setChecked(bool(self._filesets))
        self.want_slack.toggled.connect(self._refresh_scope)
        layout.addWidget(self.want_slack)
        layout.addWidget(self._build_slack_options())

        self.scope_summary = QLabel()
        self.scope_summary.setWordWrap(True)
        self.scope_summary.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.scope_summary)
        return group

    def _build_ntfs_options(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(NOTE_INDENT, 0, 0, 0)

        self.only_missing = QRadioButton()
        # No tooltip: it retold the radio button's own label.
        self.only_missing.setChecked(True)
        self.only_missing.toggled.connect(self._refresh_scope)
        layout.addWidget(self.only_missing)

        self.everything = QRadioButton()
        layout.addWidget(self.everything)

        self.with_self_check = QCheckBox()
        self.with_self_check.setChecked(True)
        self.with_self_check.toggled.connect(self._refresh_scope)
        layout.addWidget(self.with_self_check)

        self.ntfs_options = box
        return box

    def _build_slack_options(self) -> QWidget:
        """A check box per file set: "measure all" and "measure some".

        A list of check boxes, not a table: a file set is described in one
        line, and the choice is made with the mouse, one at a time — exactly
        when some of the sets are already measured or do not fit on the disk.
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(NOTE_INDENT, 0, 0, 0)

        self.fileset_boxes: dict[str, QCheckBox] = {}
        #: A file set's cost and state go in a separate label under the check
        #: box. They do not belong in the check box itself: QCheckBox does not
        #: wrap lines, and the Russian line "10 000 files of 1 KiB — 10000
        #: files, 39.06 MiB by clusters, 61.5 MiB needed, own measurement
        #: already exists" asks for 1224 pixels in a 660-pixel window and got
        #: cut off exactly at the most important part — at «уже есть».
        self.fileset_notes: dict[str, QLabel] = {}
        for item in self._filesets:
            check = QCheckBox()
            check.setChecked(item.key not in self._slack_covered)
            check.toggled.connect(self._refresh_scope)
            layout.addWidget(check)
            self.fileset_boxes[item.key] = check

            note = wrapped(QLabel())
            note.setStyleSheet("color: palette(mid);")
            note.setContentsMargins(NOTE_INDENT, 0, 0, 0)
            layout.addWidget(note)
            self.fileset_notes[item.key] = note

        row = QHBoxLayout()
        #: The "all" and "none" buttons with the keys of their text.
        self._mark_buttons: list[tuple[QPushButton, str, str]] = []
        for title, wanted, tip in (
            ("collect.dialog.all", True, "collect.dialog.all.tip"),
            ("collect.dialog.none", False, "collect.dialog.none.tip"),
        ):
            button = QPushButton()
            button.clicked.connect(lambda _=False, value=wanted: self._set_all(value))
            row.addWidget(button)
            self._mark_buttons.append((button, title, tip))
        row.addStretch(1)
        layout.addLayout(row)

        self.slack_options = box
        return box

    def _set_all(self, wanted: bool) -> None:
        for check in self.fileset_boxes.values():
            check.blockSignals(True)
            check.setChecked(wanted)
            check.blockSignals(False)
        self._refresh_scope()

    def _build_rights_box(self) -> QGroupBox:
        group = QGroupBox()
        self.rights_group = group
        row = QHBoxLayout(group)

        self.rights_label = QLabel()
        self.rights_label.setWordWrap(True)
        row.addWidget(self.rights_label, 1)

        self.elevate_button = QPushButton()
        self.elevate_button.clicked.connect(self._elevate)
        row.addWidget(self.elevate_button)
        return group

    def _build_progress(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        self.progress = QProgressBar()
        layout.addWidget(self.progress)

        self.progress_label = QLabel()
        self.progress_label.setWordWrap(True)
        self.progress_label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.progress_label)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(160)
        layout.addWidget(self.log)
        return box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.start_button = QPushButton()
        self.start_button.clicked.connect(self._start)
        row.addWidget(self.start_button)

        self.stop_button = QPushButton()
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        row.addWidget(self.stop_button)

        row.addStretch(1)
        self.close_button = QPushButton()
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        return row

    # --- VeraCrypt ---------------------------------------------------------

    def _restore_install(self) -> None:
        remembered = self._stored(PATH_KEY)
        self._install = self.find_install([remembered] if remembered else [])

    def _refresh_install(self) -> None:
        self._show_install()
        self._refresh_scope()

    def _show_install(self) -> None:
        """The install and version labels; also whether the version will do."""
        if self._install is None:
            self.install_label.setText(_not_found())
            self._supported, notice = False, ""
        else:
            self.install_label.setText(self._install.title)
            self._supported, notice = version_notice(self._install)
        self.version_label.setText(notice)
        self.version_label.setVisible(bool(notice))

    def _choose_install(self) -> None:
        chosen = self.ask_directory(
            tr("collect.dialog.install.ask"), self._install_start_dir()
        )
        if not chosen:
            return
        found = install_at(chosen)
        if found is None:
            self.report_error(
                tr("collect.dialog.install.missing"), missing_report(chosen)
            )
            return
        self._install = found
        self._remember(PATH_KEY, str(found.directory))
        self._refresh_install()

    def _install_start_dir(self) -> str:
        if self._install is not None:
            return str(self._install.directory)
        remembered = self._stored(PATH_KEY)
        return remembered or ""

    # --- working folder ----------------------------------------------------

    def _restore_workdir(self) -> None:
        remembered = self._stored(WORKDIR_KEY)
        self._workdir = Path(remembered) if remembered else Path(tempfile.gettempdir())

    def _refresh_workdir(self) -> None:
        self._show_workdir()
        self._refresh_scope()

    def _show_workdir(self) -> None:
        free = self.free_bytes()
        self.workdir_label.setText(
            tr("collect.dialog.workdir.free", path=self._workdir, free=fmt_both(free))
            if free
            else tr("collect.dialog.workdir.unavailable", path=self._workdir)
        )

    def free_bytes(self) -> int:
        return free_space(self._workdir)

    def _choose_workdir(self) -> None:
        chosen = self.ask_directory(
            tr("collect.dialog.workdir.ask"), str(self._workdir)
        )
        if not chosen:
            return
        self._workdir = Path(chosen)
        self._remember(WORKDIR_KEY, chosen)
        self._refresh_workdir()

    # --- permissions -------------------------------------------------------

    def _refresh_rights(self) -> None:
        elevated = self.is_admin()
        self.elevate_button.setVisible(not elevated)
        self.rights_label.setText(
            tr("collect.dialog.rights.admin")
            if elevated
            else tr("collect.dialog.rights.none")
        )

    def _elevate(self) -> None:
        if not self.confirm(
            tr("collect.dialog.elevate.confirm.title"),
            tr("collect.dialog.elevate.confirm"),
        ):
            return
        if not self.relaunch(self._data_dir):
            self.report_error(
                tr("collect.dialog.elevate.failed.title"),
                tr("collect.dialog.elevate.failed"),
            )
            return
        # Two copies in one data folder would write over each other.
        self.accept()
        self.relaunchRequested.emit()

    # --- plan --------------------------------------------------------------

    def current_models(self) -> tuple[MetadataModel, CopySlackModel]:
        return self._models() if self._models is not None else (MetadataModel(), CopySlackModel())

    def chosen_filesets(self) -> list[FileSet]:
        if not self.want_slack.isChecked():
            return []
        return [
            item
            for item in self._filesets
            if self.fileset_boxes[item.key].isChecked()
        ]

    def steps(self) -> list[Step]:
        """The whole plan: empty volumes first, then file sets cheapest first.

        File sets come after the NTFS points on purpose. They cost more space,
        and they are computed more precisely once the metadata model has been
        refined by fresh measurements: it is that model that sizes the
        container for a file set.
        """
        steps: list[Step] = []
        if self.want_ntfs.isChecked():
            covered = () if self.everything.isChecked() else self._covered
            steps.extend(
                plan(
                    self._sizes,
                    covered,
                    self_check=self.with_self_check.isChecked(),
                )
            )

        filesets = self.chosen_filesets()
        if filesets:
            ntfs, slack = self.current_models()
            steps.extend(
                slack_plan(
                    filesets,
                    ntfs=ntfs,
                    slack=slack,
                    safety=self._safety() if self._safety is not None else None,
                    forbidden=self._forbidden,
                )
            )
        return steps

    def _refresh_scope(self) -> None:
        steps = self.steps()
        self._refresh_scope_text(steps)
        self._refresh_scope_controls(steps)

    def _refresh_scope_controls(self, steps: Sequence[Step]) -> None:
        """What the scope shows and allows: the options of the chosen kinds,
        the progress bar's length and the Start button."""
        self.ntfs_options.setVisible(self.want_ntfs.isChecked())

        self.want_slack.setVisible(bool(self._filesets))
        self.slack_options.setVisible(
            bool(self._filesets) and self.want_slack.isChecked()
        )

        self.progress.setMaximum(max(self._plan_weight(steps) // MIB, 1))
        self.progress.setValue(0)
        self.start_button.setEnabled(bool(steps) and self._supported)

    def _refresh_scope_text(self, steps: Sequence[Step]) -> None:
        """The scope's text alone: the radio buttons, the file set notes and
        the summary. Leaves the progress bar and the buttons as they are."""
        self._scope_steps = steps
        missing = len([size for size in self._sizes if size not in self._covered])
        self.only_missing.setText(tr("collect.dialog.only_missing", n=missing))
        self.everything.setText(tr("collect.dialog.everything", n=len(self._sizes)))
        self._refresh_fileset_labels()
        self._refresh_scope_summary(steps)

    def _plan_weight(self, steps: Sequence[Step]) -> int:
        ntfs, _slack = self.current_models()
        return total_bytes(steps, ntfs)

    def _refresh_fileset_labels(self) -> None:
        """Add each file set's cost and state to it.

        The file count and volume are what a set is chosen for; the space
        required and "already measured" are what it is turned down for.
        """
        if not self._filesets:
            return
        ntfs, slack = self.current_models()
        free = self.free_bytes()
        for item in self._filesets:
            step = self._preview_step(item, ntfs, slack)
            need = required_bytes(step, ntfs)
            parts = [
                tr_n("collect.dialog.fileset.files", item.file_count),
                tr(
                    "collect.dialog.fileset.alloc",
                    size=fmt_with_unit(item.alloc_bytes(), UNIT_AUTO),
                ),
                tr("collect.dialog.fileset.need", size=fmt_with_unit(need, UNIT_AUTO)),
            ]
            if item.key in self._slack_covered:
                parts.append(tr("collect.dialog.fileset.covered"))
            if free and free < need:
                parts.append(tr("collect.dialog.fileset.short"))
            check = self.fileset_boxes[item.key]
            self._set_note(self.fileset_notes[item.key], ", ".join(parts))
            check.setToolTip(
                tr(
                    "collect.dialog.fileset.tip",
                    container=step.container_mib,
                    predicted=step.predicted_mib,
                    safety=step.predicted_safety_mib,
                )
            )

    @staticmethod
    def _set_note(note: QLabel, text: str) -> None:
        """The label under a file set's check box, with height for wrapping.

        The height has to be set by hand: under scrolling the layout squeezes
        a wrapped label down to one line and below — a QLabel's minimum
        height does not depend on its width — and the second line vanished
        entirely. The width taken is not the current one but the guaranteed
        one: the content will not get narrower than that, past it horizontal
        scrolling kicks in.
        """
        note.setText(text)
        width = max(note.width(), MIN_CONTENT_WIDTH - NOTE_INDENT * 2)
        note.setMinimumHeight(note.heightForWidth(width))

    def _preview_step(self, item: FileSet, ntfs, slack) -> Step:
        return slack_step(
            item, ntfs, slack,
            self._safety() if self._safety is not None else None,
            forbidden=self._forbidden,
        )

    def _refresh_scope_summary(self, steps: Sequence[Step]) -> None:
        ntfs, _slack = self.current_models()
        free = self.free_bytes()
        peak = max((required_bytes(step, ntfs) for step in steps), default=0)
        self.space_label.setText(
            tr(
                "collect.dialog.space.peak",
                peak=fmt_with_unit(peak, UNIT_AUTO),
                free=fmt_with_unit(free, UNIT_AUTO),
            )
            if steps
            else tr("collect.dialog.space.free", free=fmt_with_unit(free, UNIT_AUTO))
        )
        if not steps:
            self.scope_summary.setText(tr("collect.dialog.scope.nothing"))
            return
        text = tr(
            "collect.dialog.scope.summary",
            count=len(steps),
            weight=fmt_with_unit(self._plan_weight(steps), UNIT_AUTO),
            peak=fmt_with_unit(peak, UNIT_AUTO),
        )
        short = [step for step in steps if required_bytes(step, ntfs) > free]
        if free and short:
            text += " " + tr("collect.dialog.scope.short", count=len(short))
        self.scope_summary.setText(text)

    # --- collection run ----------------------------------------------------

    def _start(self) -> None:
        if self._install is None:
            self.report_error(tr("collect.dialog.install.missing"), _not_found())
            return
        if not is_writable(self._workdir):
            self.report_error(
                tr("collect.dialog.workdir.denied.title"),
                tr("collect.dialog.workdir.denied", path=self._workdir),
            )
            return

        steps = self.steps()
        ntfs, _slack = self.current_models()
        weight = total_bytes(steps, ntfs)
        if not self.confirm(
            tr("collect.dialog.start.confirm.title"),
            tr(
                "collect.dialog.start.confirm",
                count=len(steps),
                weight=fmt_with_unit(weight, UNIT_AUTO),
            ),
        ):
            return

        self._measured = 0
        self._failed = 0
        self._skipped = 0
        self.log.clear()
        self._weights = [weight_bytes(step, ntfs) for step in steps]
        self._before = [0]
        for value in self._weights:
            self._before.append(self._before[-1] + value)
        self._total_weight = self._before[-1]
        self.progress.setMaximum(max(self._total_weight // MIB, 1))
        self.progress.setValue(0)
        self._started = time.monotonic()
        self._set_phase("collect.dialog.phase.prepare")
        self._clock.start()
        self._say(tr("collect.dialog.log.started", install=self._install.title))

        collector = Collector(
            veracrypt=self.make_veracrypt(self._install),
            workdir=self._workdir,
            steps=steps,
            ntfs=ntfs,
            free_bytes=self.free_bytes,
        )
        self._run_in_thread(CollectWorker(collector))

    def _run_in_thread(self, worker: CollectWorker) -> None:
        self._worker = worker
        self._thread = QThread(self)
        worker.moveToThread(self._thread)
        self._thread.started.connect(worker.run)
        worker.stepStarted.connect(self._on_step_started)
        worker.stepFinished.connect(self._on_step_finished)
        worker.stepProgress.connect(self._on_step_progress)
        worker.note.connect(self._say)
        worker.done.connect(self._on_done)
        self._set_running(True)
        self._thread.start()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.install_button.setEnabled(not running)
        self.elevate_button.setEnabled(not running)

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self.stop_button.setEnabled(False)
            self._say(tr("collect.dialog.log.stopping"))

    def _on_step_started(self, index: int, title: str) -> None:
        self._index = index
        self._share = 0.0
        self._set_phase("collect.dialog.phase.step")
        self._advance(index, 0.0)
        self._say(f"[{index + 1}/{len(self._weights)}] {title}")

    def _on_step_progress(self, index: int, progress: Progress) -> None:
        self._index = index
        self._advance(index, progress.share)
        self._set_phase(progress.phase, progress.detail)
        self._show_progress()

    def _set_phase(self, phase: str, detail: str = "") -> None:
        """Remember the phase — its key, translated when shown — and when it
        started.

        The phase clock is needed exactly where there is no progress:
        "creating the container" with a full format lasts for minutes, and
        without a ticking second the window cannot be told from a hung one.
        """
        if phase != self._phase:
            self._phase = phase
            self._phase_started = time.monotonic()
        self._detail = detail

    def _advance(self, index: int, share: float) -> None:
        """The bar is the share of weight done out of the whole plan's weight.

        The step's share is computed by Progress itself, with the same weight
        the plan is weighed by: bytes written plus an allowance for creating
        the files.

        The share is kept at the largest reached in the step, and here is why.
        Outside writing it is zero — how many bytes VeraCrypt laid down while
        creating the container is not visible from outside — and after
        writing come four more phases: verification, remount, left-space
        measurement, cleanup. If we did not keep what was reached, the bar
        would roll back to the start of the step on each of them.
        """
        if index >= len(self._before) - 1:
            return
        self._share = max(self._share, share)
        done = self._before[index] + int(self._weights[index] * self._share)
        self.progress.setValue(min(done // MIB, self.progress.maximum()))

    def _show_progress(self) -> None:
        """Build the line under the bar from what is known right now.

        Takes nothing and is called from anywhere — both from the step's
        progress and from the once-a-second timer: otherwise the clock would
        move only when the step reports on itself, and it is silent exactly in
        the longest places.
        """
        if not self._weights:
            return
        now = time.monotonic()
        phase = tr(self._phase)
        if self._detail:
            phase = f"{phase}, {self._detail}"
        parts = [f"[{self._index + 1}/{len(self._weights)}] {phase}"]
        if self._phase_started:
            parts.append(
                tr("collect.dialog.status.phase", time=_clock(now - self._phase_started))
            )
        elapsed = now - self._started
        parts.append(tr("collect.dialog.status.elapsed", time=_clock(elapsed)))
        done = self.progress.value()
        total = self.progress.maximum()
        if done > total * ETA_AFTER_PERCENT // 100 and done:
            parts.append(
                tr(
                    "collect.dialog.status.left",
                    time=_clock(elapsed * (total - done) / done),
                )
            )
        self.progress_label.setText(" · ".join(parts))

    def _on_step_finished(self, result: StepResult) -> None:
        self._advance(self._index, 1.0)
        if result.skipped:
            self._skipped += 1
            self._say("    " + tr("collect.dialog.log.skipped", error=result.error))
            return
        if result.measurement is not None:
            # The measurement goes out even when the self-check fails: it is
            # real, and the second of the pair is precisely the ordinary
            # container.
            self.pointMeasured.emit(result.measurement.as_record(self._version()))
            self._measured += 1
            self._say(self._measurement_line(result))
        if result.error:
            self._failed += 1
            self._say("    " + tr("collect.dialog.log.error", error=result.error))

    def _measurement_line(self, result: StepResult) -> str:
        measurement = result.measurement
        line = "    " + tr(
            "collect.dialog.log.metadata", size=fmt_both(measurement.metadata_bytes)
        )
        slack = measurement.copy_slack_bytes
        if slack is not None:
            record = measurement.as_record()
            miss = record.miss_mib
            line += f"{LINE_BREAK}    " + tr_n(
                "collect.dialog.log.slack", measurement.file_count, slack=fmt_both(slack)
            )
            if slack < 0:
                # Less is used than the data itself — that cannot happen. Most
                # often it means a small file fit right into its MFT record
                # and got no cluster, while Σ ceil(size / cluster) counted it.
                # The measurement will not go into the copy-slack
                # calibration, but its NTFS point is valid, and there is no
                # reason to throw it away.
                line += f"{LINE_BREAK}    " + tr("collect.dialog.log.negative")
            if miss is not None:
                line += f"{LINE_BREAK}    " + tr(
                    "collect.dialog.log.prediction",
                    predicted=measurement.predicted_mib,
                    minimum=record.minimum_mib,
                    miss=f"{miss:+d}",
                )
        return line

    def _on_done(self, reason: str) -> None:
        self._clock.stop()
        self._finish_thread()
        self._set_running(False)
        self._phase = self._detail = ""
        self.progress_label.setText("")
        self._say(
            tr(
                "collect.dialog.log.done",
                measured=self._measured,
                failed=self._failed,
                skipped=self._skipped,
            )
            if not reason
            else tr("collect.dialog.log.interrupted", reason=reason)
        )
        if self._skipped:
            self._say(tr("collect.dialog.log.skipped_later"))
        self._say(
            tr("collect.dialog.log.saved")
            if self._measured
            else tr("collect.dialog.log.nothing")
        )
        if self._closing:
            super().reject()

    def _finish_thread(self) -> None:
        """Close the thread. Called once run() has returned; does not wait."""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
        self._worker = None

    def _version(self) -> str:
        version = self._install.version if self._install else ""
        return version or UNREAD_VERSION

    def _say(self, text: str) -> None:
        self.log.appendPlainText(text)

    # --- housekeeping ------------------------------------------------------

    def running(self) -> bool:
        return self._thread is not None

    def reject(self) -> None:
        """Closing mid-collection does not cut the current step; it waits.

        Without blocking the window: closing is put off until the step ends.
        Cutting VeraCrypt off halfway would leave a mounted volume and a
        terabyte file, and waiting with a blocking wait() would freeze the
        window for minutes.
        """
        if self.running():
            if not self.confirm(
                tr("collect.dialog.close.confirm.title"),
                tr("collect.dialog.close.confirm"),
            ):
                return
            self._closing = True
            self._stop()
            self.close_button.setEnabled(False)
            return
        super().reject()

    def _stored(self, key: str) -> str:
        if self._settings is None:
            return ""
        return str(self._settings.value(key, "", type=str))

    def _remember(self, key: str, value: str) -> None:
        # Straight to disk: the VeraCrypt path is found by hand once, and
        # losing it because the program was not closed cleanly would be a
        # shame.
        if self._settings is not None:
            self._settings.setValue(key, value)
            self._settings.sync()

    def _ask_directory(self, title: str, start: str) -> str:
        return QFileDialog.getExistingDirectory(self, title, start)

    def _show_error(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _ask_confirmation(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.Yes
