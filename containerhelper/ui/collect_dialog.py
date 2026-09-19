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

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
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
from ..formatting import UNIT_AUTO, fmt_both, fmt_with_unit, plural
from ..model import MIB, CopySlackModel, NtfsModel, SafetyModel
from ..paths import is_writable
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

NOT_FOUND = (
    "VeraCrypt не нашёлся ни в «C:\\Program Files\\VeraCrypt», ни в "
    "«C:\\Program Files (x86)\\VeraCrypt». Укажите папку, где лежат "
    "«{format_exe}» и «{mount_exe}»."
).format(format_exe=FORMAT_NAMES[0], mount_exe=MOUNT_NAMES[0])

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

ModelProvider = Callable[[], "tuple[NtfsModel, CopySlackModel]"]
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
                    f"Убрано контейнеров от прошлого прерванного сбора: "
                    f"{len(removed)}."
                )

            for index, step in enumerate(self.collector.steps):
                if self._stop:
                    reason = "Остановлено по требованию."
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
        self.setWindowTitle("Автоматический сбор замеров")
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

        #: Replaceable handlers: a modal window in the middle of the logic
        #: cannot be closed from a test, and an elevated restart cannot be
        #: undone.
        #: The window's clock. It runs on its own, not from progress: see
        #: CLOCK_INTERVAL.
        self._clock = QTimer(self)
        self._clock.setInterval(CLOCK_INTERVAL)
        self._clock.timeout.connect(self._show_progress)

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
        self._refresh_rights()
        self._refresh_scope()

    # --- building ----------------------------------------------------------

    def _build_intro(self) -> QLabel:
        text = QLabel(
            "Программа создаёт контейнеры, монтирует их и читает тома сама.\n"
            "Пустой том даёт метаданные NTFS — контейнеры динамические, "
            "поэтому терабайтный занимает на диске мегабайты. Замер запаса на "
            "копирование пишет на том сгенерированный набор файлов, и вот ему "
            "место нужно по-настоящему."
        )
        text.setWordWrap(True)
        text.setStyleSheet("color: palette(mid);")
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

        self.install_button = QPushButton("Указать папку…")
        self.install_button.setToolTip(
            "Папка, где лежат «" + FORMAT_NAMES[0] + "» и «"
            + MOUNT_NAMES[0] + "».\n"
            "Нужна, если VeraCrypt стоит не в Program Files — например, "
            "портативная сборка. Выбор запомнится."
        )
        self.install_button.clicked.connect(self._choose_install)
        row.addWidget(self.install_button)

        self.version_label = QLabel()
        self.version_label.setWordWrap(True)
        self.version_label.setStyleSheet("color: palette(mid);")
        self.version_label.setToolTip(
            "Ключи командной строки у VeraCrypt со временем менялись. Для "
            "старой версии возьмём те, которые она понимает; с совсем старой "
            "сбор не пойдёт."
        )
        outer.addWidget(self.version_label)
        return group

    def _build_workdir_box(self) -> QGroupBox:
        group = QGroupBox("Где создавать контейнеры")
        outer = QVBoxLayout(group)
        row = QHBoxLayout()
        outer.addLayout(row)

        self.workdir_label = QLabel()
        self.workdir_label.setWordWrap(True)
        self.workdir_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.workdir_label.setToolTip(
            "Папка для временных контейнеров — их удаляют сразу после "
            "замера.\n"
            "Замеры NTFS почти не требуют места: контейнер динамический, на "
            "диск ложатся только метаданные. Замер запаса пишет весь набор "
            "файлов, и вот под него место нужно."
        )
        row.addWidget(self.workdir_label, 1)

        button = QPushButton("Выбрать…")
        button.clicked.connect(self._choose_workdir)
        row.addWidget(button)

        self.space_label = QLabel()
        self.space_label.setWordWrap(True)
        self.space_label.setStyleSheet("color: palette(mid);")
        self.space_label.setToolTip(
            "Сколько места нужно самому прожорливому из выбранных шагов и "
            "сколько его есть. Требуемое считается той же моделью "
            "метаданных, которую сбор и калибрует.\n"
            "Шаг, которому не хватило, пропускается — его можно доснять "
            "позже, когда место освободится."
        )
        outer.addWidget(self.space_label)
        return group

    def _build_scope_box(self) -> QGroupBox:
        group = QGroupBox("Что снимать")
        layout = QVBoxLayout(group)

        self.want_ntfs = QCheckBox("Метаданные NTFS — замеры пустых томов")
        self.want_ntfs.setChecked(True)
        self.want_ntfs.setToolTip(
            "Точки для модели метаданных. Класть в контейнер ничего не "
            "нужно: метаданные зависят только от размера тома."
        )
        self.want_ntfs.toggled.connect(self._refresh_scope)
        layout.addWidget(self.want_ntfs)
        layout.addWidget(self._build_ntfs_options())

        self.want_slack = QCheckBox(
            "Запас на копирование — наборы файлов на томе"
        )
        self.want_slack.setChecked(bool(self._filesets))
        self.want_slack.setToolTip(
            "Единственная часть модели, не подтверждённая замерами при разном "
            "числе файлов. Наборы отличаются именно числом файлов: по одному "
            "числу наклон не считается вовсе."
        )
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
        self.everything.setToolTip(
            "Переснять все размеры заново. Имеет смысл после обновления "
            "Windows или смены версии VeraCrypt — форматирует именно их код."
        )
        layout.addWidget(self.everything)

        self.with_self_check = QCheckBox("Начать с самопроверки (1 GiB дважды)")
        self.with_self_check.setChecked(True)
        self.with_self_check.setToolTip(
            "Снять гигабайт двумя способами: динамическим контейнером с "
            "быстрым форматированием и обычным с полным.\n"
            "Совпало — остальное можно снимать динамическими. Разошлось — на "
            "этой машине так нельзя, и лучше узнать это сразу.\n"
            "Обычный контейнер единственный во всём сборе требует целого "
            "гигабайта свободного места."
        )
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
            check = QCheckBox(item.title)
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
        for title, wanted, tip in (
            ("Все", True, "Отметить все наборы."),
            ("Ничего", False, "Снять все отметки."),
        ):
            button = QPushButton(title)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, value=wanted: self._set_all(value))
            row.addWidget(button)
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
        group = QGroupBox("Права")
        row = QHBoxLayout(group)

        self.rights_label = QLabel()
        self.rights_label.setWordWrap(True)
        row.addWidget(self.rights_label, 1)

        self.elevate_button = QPushButton("Перезапустить от администратора")
        self.elevate_button.setToolTip(
            "Без прав администратора Windows спросит подтверждение на каждый "
            "контейнер, и сбор перестанет быть автоматическим.\n"
            "Программа закроется и откроется заново с той же папкой данных."
        )
        self.elevate_button.clicked.connect(self._elevate)
        row.addWidget(self.elevate_button)
        return group

    def _build_progress(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        self.progress = QProgressBar()
        self.progress.setToolTip(
            "Доля записанных байт, а не пройденных шагов. Шаги слишком "
            "разные: пустой терабайтный том снимается за секунды, а набор в "
            "четыре гигабайта пишется минутами, и полоса по числу шагов "
            "врала бы в разы."
        )
        layout.addWidget(self.progress)

        self.progress_label = QLabel()
        self.progress_label.setWordWrap(True)
        self.progress_label.setStyleSheet("color: palette(mid);")
        self.progress_label.setToolTip(
            "Какой шаг идёт, на какой он фазе, сколько эта фаза длится и "
            "сколько прошло всего. Внутри записи набора видно число готовых "
            "файлов и записанный объём."
        )
        layout.addWidget(self.progress_label)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(160)
        self.log.setToolTip(
            "Ход сбора. Текст выделяется и копируется — если что-то пошло не "
            "так, смотреть сюда."
        )
        layout.addWidget(self.log)
        return box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.start_button = QPushButton("Начать")
        self.start_button.clicked.connect(self._start)
        row.addWidget(self.start_button)

        self.stop_button = QPushButton("Остановить")
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip(
            "Остановиться, доделав то, что нельзя бросить. Запись набора "
            "прерывается сразу, а начатое создание контейнера доводится до "
            "конца — иначе остался бы поднятый том и файл на диске."
        )
        self.stop_button.clicked.connect(self._stop)
        row.addWidget(self.stop_button)

        row.addStretch(1)
        self.close_button = QPushButton("Закрыть")
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        return row

    # --- VeraCrypt ---------------------------------------------------------

    def _restore_install(self) -> None:
        remembered = self._stored(PATH_KEY)
        self._install = self.find_install([remembered] if remembered else [])
        self._refresh_install()

    def _refresh_install(self) -> None:
        if self._install is None:
            self.install_label.setText(NOT_FOUND)
            self._supported, notice = False, ""
        else:
            self.install_label.setText(self._install.title)
            self._supported, notice = version_notice(self._install)
        self.version_label.setText(notice)
        self.version_label.setVisible(bool(notice))
        self._refresh_scope()

    def _choose_install(self) -> None:
        chosen = self.ask_directory("Папка VeraCrypt", self._install_start_dir())
        if not chosen:
            return
        found = install_at(chosen)
        if found is None:
            self.report_error("VeraCrypt не найден", missing_report(chosen))
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
        self._refresh_workdir()

    def _refresh_workdir(self) -> None:
        free = self.free_bytes()
        self.workdir_label.setText(
            f"{self._workdir} — свободно {fmt_both(free)}"
            if free
            else f"{self._workdir} — недоступна или места нет вовсе"
        )
        self._refresh_scope()

    def free_bytes(self) -> int:
        return free_space(self._workdir)

    def _choose_workdir(self) -> None:
        chosen = self.ask_directory("Папка для контейнеров", str(self._workdir))
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
            "Программа работает с правами администратора — UAC ничего не "
            "спросит."
            if elevated
            else "Прав администратора нет: Windows спросит подтверждение на "
            "каждый контейнер. Форматирование NTFS без них не идёт."
        )

    def _elevate(self) -> None:
        if not self.confirm(
            "Перезапуск от администратора",
            "Программа закроется и откроется заново, уже с запросом прав. "
            "Несохранённого у неё ничего нет.\nПродолжить?",
        ):
            return
        if not self.relaunch(self._data_dir):
            self.report_error(
                "Не вышло",
                "Windows отклонила запрос прав. Запустите программу через "
                "«Запуск от имени администратора» сами.",
            )
            return
        # Two copies in one data folder would write over each other.
        self.accept()
        self.relaunchRequested.emit()

    # --- plan --------------------------------------------------------------

    def current_models(self) -> tuple[NtfsModel, CopySlackModel]:
        return self._models() if self._models is not None else (NtfsModel(), CopySlackModel())

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
        missing = len([size for size in self._sizes if size not in self._covered])
        self.only_missing.setText(f"Только недостающие размеры ({missing})")
        self.everything.setText(f"Переснять все размеры ({len(self._sizes)})")
        self.ntfs_options.setVisible(self.want_ntfs.isChecked())

        self.want_slack.setVisible(bool(self._filesets))
        self.slack_options.setVisible(
            bool(self._filesets) and self.want_slack.isChecked()
        )
        self._refresh_fileset_labels()

        steps = self.steps()
        self._refresh_scope_summary(steps)
        self.progress.setMaximum(max(self._plan_weight(steps) // MIB, 1))
        self.progress.setValue(0)
        self.start_button.setEnabled(bool(steps) and self._supported)

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
                f"{item.file_count} "
                f"{plural(item.file_count, 'файл', 'файла', 'файлов')}",
                f"{fmt_with_unit(item.alloc_bytes(), UNIT_AUTO)} по кластерам",
                f"нужно {fmt_with_unit(need, UNIT_AUTO)}",
            ]
            if item.key in self._slack_covered:
                parts.append("свой замер уже есть")
            if free and free < need:
                parts.append("НЕ ХВАТАЕТ МЕСТА")
            check = self.fileset_boxes[item.key]
            self._set_note(self.fileset_notes[item.key], ", ".join(parts))
            check.setToolTip(
                f"Контейнер {step.container_mib} MiB. Расчёт обещает "
                f"{step.predicted_mib} MiB, из них {step.predicted_safety_mib} "
                f"MiB страховки; контейнер делается с запасом, чтобы набор "
                f"точно влез, а обещание записывается как есть — на нём "
                f"держится проверка прогноза."
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
            f"Самому прожорливому шагу нужно {fmt_with_unit(peak, UNIT_AUTO)}, "
            f"свободно {fmt_with_unit(free, UNIT_AUTO)}."
            if steps
            else f"Свободно {fmt_with_unit(free, UNIT_AUTO)}."
        )
        if not steps:
            self.scope_summary.setText(
                "Снимать нечего: все размеры уже закрыты своими замерами."
            )
            return
        text = (
            f"Контейнеров будет создано: {len(steps)}. Записать предстоит "
            f"{fmt_with_unit(self._plan_weight(steps), UNIT_AUTO)}, самому "
            f"прожорливому шагу нужно {fmt_with_unit(peak, UNIT_AUTO)}."
        )
        short = [step for step in steps if required_bytes(step, ntfs) > free]
        if free and short:
            text += (
                f" Шагов, которым места не хватает: {len(short)} — они будут "
                f"пропущены, доснять их можно позже."
            )
        self.scope_summary.setText(text)

    # --- collection run ----------------------------------------------------

    def _start(self) -> None:
        if self._install is None:
            self.report_error("VeraCrypt не найден", NOT_FOUND)
            return
        if not is_writable(self._workdir):
            self.report_error(
                "Папка недоступна",
                f"В {self._workdir} нельзя писать — выберите другую.",
            )
            return

        steps = self.steps()
        ntfs, _slack = self.current_models()
        weight = total_bytes(steps, ntfs)
        if not self.confirm(
            "Начать сбор",
            f"Будет создано и удалено контейнеров: {len(steps)}.\n"
            f"Записать предстоит примерно "
            f"{fmt_with_unit(weight, UNIT_AUTO)}. Продолжить?",
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
        self._set_phase("подготовка")
        self._clock.start()
        self._say(f"Сбор начат. VeraCrypt: {self._install.title}.")

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
            self._say("Остановлюсь, как только текущий шаг можно будет бросить…")

    def _on_step_started(self, index: int, title: str) -> None:
        self._index = index
        self._share = 0.0
        self._set_phase("подготовка шага")
        self._advance(index, 0.0)
        self._say(f"[{index + 1}/{len(self._weights)}] {title}")

    def _on_step_progress(self, index: int, progress: Progress) -> None:
        self._index = index
        self._advance(index, progress.share)
        self._set_phase(progress.phase, progress.detail)
        self._show_progress()

    def _set_phase(self, phase: str, detail: str = "") -> None:
        """Remember the phase and when it started.

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
        phase = f"{self._phase}, {self._detail}" if self._detail else self._phase
        parts = [f"[{self._index + 1}/{len(self._weights)}] {phase}"]
        if self._phase_started:
            parts.append(f"фаза {_clock(now - self._phase_started)}")
        elapsed = now - self._started
        parts.append(f"прошло {_clock(elapsed)}")
        done = self.progress.value()
        total = self.progress.maximum()
        if done > total * ETA_AFTER_PERCENT // 100 and done:
            parts.append(f"осталось примерно {_clock(elapsed * (total - done) / done)}")
        self.progress_label.setText(" · ".join(parts))

    def _on_step_finished(self, result: StepResult) -> None:
        self._advance(self._index, 1.0)
        if result.skipped:
            self._skipped += 1
            self._say(f"    пропущен: {result.error}")
            return
        if result.measurement is not None:
            # The measurement goes out even when the self-check fails: it is
            # real, and the second of the pair is precisely the ordinary
            # container.
            self.pointMeasured.emit(result.measurement.as_record(self._note()))
            self._measured += 1
            self._say(self._measurement_line(result))
        if result.error:
            self._failed += 1
            self._say(f"    ошибка: {result.error}")

    def _measurement_line(self, result: StepResult) -> str:
        measurement = result.measurement
        line = f"    метаданные NTFS: {fmt_both(measurement.ntfs_bytes)}"
        slack = measurement.copy_slack_bytes
        if slack is not None:
            record = measurement.as_record()
            miss = record.miss_mib
            line += (
                f"{LINE_BREAK}    запас на копирование: {fmt_both(slack)} "
                f"при {measurement.file_count} "
                f"{plural(measurement.file_count, 'файле', 'файлах', 'файлах')}"
            )
            if slack < 0:
                # Less is used than the data itself — that cannot happen. Most
                # often it means a small file fit right into its MFT record
                # and got no cluster, while Σ ceil(size / cluster) counted it.
                # The measurement will not go into the copy-slack
                # calibration, but its NTFS point is valid, and there is no
                # reason to throw it away.
                line += (
                    f"{LINE_BREAK}    ⚠ запас вышел отрицательным — в "
                    f"калибровку запаса такой замер не идёт; точка NTFS из "
                    f"него остаётся годной"
                )
            if miss is not None:
                line += (
                    f"{LINE_BREAK}    прогноз: обещано "
                    f"{measurement.predicted_mib} MiB, хватило бы "
                    f"{record.minimum_mib} MiB, промах {miss:+d} MiB"
                )
        return line

    def _on_done(self, reason: str) -> None:
        self._clock.stop()
        self._finish_thread()
        self._set_running(False)
        self._phase = self._detail = ""
        self.progress_label.setText("")
        self._say(
            f"Готово. Снято замеров: {self._measured}, неудач: {self._failed}, "
            f"пропущено из-за места: {self._skipped}."
            if not reason
            else f"Сбор прерван. {reason}"
        )
        if self._skipped:
            self._say(
                "Пропущенные шаги можно доснять позже: освободите место и "
                "откройте это окно снова — недостающее подберётся само."
            )
        self._say(
            "Замеры сохранены, таблицы уже обновились."
            if self._measured
            else "Снять не удалось ничего."
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

    def _note(self) -> str:
        version = self._install.version if self._install else ""
        return "Автоматический сбор" + (f", VeraCrypt {version}" if version else "")

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
                "Сбор идёт",
                "Остановить сбор и закрыть окно? Текущий контейнер доделаем "
                "и удалим, окно закроется после этого.",
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
