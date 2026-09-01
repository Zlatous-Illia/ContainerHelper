"""Диалог автоматического сбора замеров через VeraCrypt.

Логика сбора живёт в `collect.py` и Qt не знает вовсе. Здесь только то, ради
чего диалог и нужен: найти VeraCrypt (а не найдя — спросить), показать, что
происходит, и дать остановить.

Шаги крутятся в отдельном потоке. Создание и монтирование контейнера — это
секунды, на полном форматировании минуты, а запись набора в четыре гигабайта
и того дольше; в потоке окна всё это встало бы намертво, и «Остановить»
нажать было бы нечем.
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

#: Ключи в settings.ini. Путь к VeraCrypt запоминается только тот, что указан
#: руками: стандартные места ищутся заново каждый раз и запоминать их незачем.
PATH_KEY = "veracrypt/path"
WORKDIR_KEY = "veracrypt/workdir"

NOT_FOUND = (
    "VeraCrypt не нашёлся ни в «C:\\Program Files\\VeraCrypt», ни в "
    "«C:\\Program Files (x86)\\VeraCrypt». Укажите папку, где лежат "
    "«{format_exe}» и «{mount_exe}»."
).format(format_exe=FORMAT_NAMES[0], mount_exe=MOUNT_NAMES[0])

#: Перенос строки в подсказках. Константой, как и в остальных вкладках:
#: экранирование внутри шаблонов правки уже один раз схлопывалось.
LINE_BREAK = chr(10)

#: Как часто прогресс уходит в окно. Набор из десяти тысяч файлов дал бы
#: десять тысяч сигналов через границу потока и забил бы очередь событий
#: раньше, чем окно успело бы их отрисовать.
PROGRESS_INTERVAL = 0.1

#: С какой доли пути оценка оставшегося времени перестаёт быть гаданием.
#: Раньше неё скорость меряется по первым секундам создания контейнера и
#: врёт в разы.
ETA_AFTER_PERCENT = 5

#: Ниже этого содержимое диалога перестаёт быть читаемым: подписи наборов
#: схлопываются, а строка кнопок под ними уезжает за край. Дальше включается
#: горизонтальная прокрутка — то же решение, что и у вкладок главного окна.
MIN_CONTENT_WIDTH = 640

#: Отступ подписей под галочкой — тем же числом, что и у самих галочек.
NOTE_INDENT = 20

#: Как часто окно перерисовывает часы само по себе, миллисекунды. Время шло
#: только вместе с прогрессом, а прогресс приходит от шага: на полном
#: форматировании и на монтировании его нет вовсе, и часы стояли минутами —
#: единственный признак того, что программа жива, замирал ровно тогда, когда
#: он и был нужен.
CLOCK_INTERVAL = 1000

ModelProvider = Callable[[], "tuple[NtfsModel, CopySlackModel]"]
SafetyProvider = Callable[[], SafetyModel]


def _clock(seconds: float) -> str:
    """Секунды в «м:сс» или «ч:мм:сс» — как их читают глазом."""
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class CollectWorker(QObject):
    """Крутит шаги в своём потоке и рассказывает о них сигналами."""

    stepStarted = Signal(int, str)
    stepFinished = Signal(object)
    #: Номер шага и что внутри него происходит. Throttling здесь же: без него
    #: сигналов было бы столько же, сколько файлов в наборе.
    stepProgress = Signal(int, object)
    note = Signal(str)
    #: Пусто — прошло до конца. Иначе причина остановки.
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
        """Остановить сбор.

        Между шагами — сразу. Внутри шага отмена доходит до записи набора:
        четыре гигабайта пишутся минутами, и ждать их конца незачем, недописанный
        набор всё равно выбрасывается вместе с контейнером. А вот запущенный
        VeraCrypt не прервать, не оставив за собой поднятый том и файл на
        терабайт, — создание и форматирование доводятся до конца.
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
            # Сигнал обязан прозвучать даже на неожиданной ошибке: иначе окно
            # останется «в работе» навсегда, и закрыть его будет нечем.
            self.done.emit(reason)


class CollectDialog(QDialog):
    """Найти VeraCrypt, спланировать сбор и провести его."""

    #: Снятый замер. Уходит наружу сразу же, по одному: сбор идёт часами, и
    #: падение посередине не должно стоить всего, что уже снято.
    pointMeasured = Signal(object)
    #: Перезапуск от администратора состоялся — окно должно закрыться, иначе
    #: две копии станут писать в одну папку данных.
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
        #: Ключи наборов, на которых свой замер уже есть. Ключами, а не
        #: числами файлов: два набора с n = 1 различаются объёмом, и
        #: снимать надо оба — на их сверке держится проверка того, что
        #: запас от размера файлов не зависит.
        self._slack_covered = tuple(slack_covered)
        self._models = models
        self._safety = safety
        #: Размеры, на которые замеру запаса садиться нельзя: это строки
        #: таблицы покрытия, и кнопка «К заводскому» в такой строке отключала
        #: бы заодно и замер запаса.
        self._forbidden = tuple(forbidden_sizes)
        self._settings = settings
        self._data_dir = data_dir
        #: Ставится до сборки виджетов: перерисовка области сбора
        #: спрашивает свободное место, а она случается уже при поиске
        #: VeraCrypt.
        self._workdir = Path(tempfile.gettempdir())
        self._install = None
        #: Годится ли найденная версия для сбора: у совсем старой нет ключей,
        #: без которых он не идёт.
        self._supported = False
        self._thread: QThread | None = None
        self._worker: CollectWorker | None = None
        self._measured = 0
        self._failed = 0
        self._skipped = 0
        #: Окно попросили закрыть посреди сбора: закроется, когда шаг кончится.
        self._closing = False
        #: Вес шагов и их нарастающая сумма — знаменатель и точки отсчёта для
        #: полосы прогресса.
        self._weights: list[int] = []
        self._before: list[int] = [0]
        self._total_weight = 0
        self._started = 0.0
        self._index = 0
        #: Что идёт сейчас и с какой секунды. Фаза отдельно от подробности:
        #: «запись файлов» держится минутами, а число записанных байт в ней
        #: меняется десять раз в секунду, и часы фазы сбрасывались бы вместе
        #: с ним.
        self._phase = ""
        self._detail = ""
        self._phase_started = 0.0
        #: Наибольшая доля текущего шага. Полоса не имеет права пятиться.
        self._share = 0.0

        #: Подменяемые обработчики: модальное окно посреди логики нечем
        #: закрыть из теста, а перезапуск от администратора нечем отменить.
        #: Часы окна. Идут сами, а не от прогресса: см. CLOCK_INTERVAL.
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

        # Всё, кроме кнопок, живёт под прокруткой. Окно просит 1178 пикселей
        # высоты — больше, чем есть у экрана, — и без прокрутки Qt сплющивает
        # подписи: у строки с ценой набора высота становилась нулевой, и текст
        # исчезал целиком. Кнопки остаются снаружи: «Остановить» обязана быть
        # под рукой, не прокручивая окно.
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

    # --- построение --------------------------------------------------------

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
        # Без подсказки: она пересказывала подпись самого переключателя.
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
        """Галочка на каждый набор: «замерить все» и «замерить некоторые».

        Списком галочек, а не таблицей: набор описывается одной строкой, а
        выбирать надо мышью и по одному — как раз тогда, когда часть наборов
        уже снята или не помещается на диск.
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(NOTE_INDENT, 0, 0, 0)

        self.fileset_boxes: dict[str, QCheckBox] = {}
        #: Цена набора и его состояние — отдельной подписью под галочкой.
        #: В самой галочке им не место: QCheckBox не переносит строк, а строка
        #: «10 000 файлов по 1 KiB — 10000 файлов, 39.06 MiB по кластерам,
        #: нужно 61.5 MiB, свой замер уже есть» просит 1224 пикселя при окне
        #: в 660 и обрезалась ровно на самом важном — на «уже есть».
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

    # --- рабочая папка -----------------------------------------------------

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

    # --- права -------------------------------------------------------------

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
        # Две копии в одной папке данных писали бы поверх друг друга.
        self.accept()
        self.relaunchRequested.emit()

    # --- план --------------------------------------------------------------

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
        """План целиком: сначала пустые тома, потом наборы от дешёвых к дорогим.

        Наборы после точек NTFS намеренно. Они и дороже по месту, и точнее
        считаются, когда модель метаданных уже уточнена свежими замерами:
        именно ею оценивается контейнер под набор.
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
        """Дописать к каждому набору его цену и состояние.

        Число файлов и объём — то, ради чего набор выбирают; требуемое место
        и «уже снят» — то, из-за чего от него отказываются.
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
        """Подпись под галочкой набора вместе с высотой под перенос.

        Высоту приходится ставить руками: под прокруткой раскладка ужимает
        переносимую подпись до одной строки и ниже — у QLabel минимальная
        высота не зависит от ширины, — и вторая строка исчезала целиком.
        Ширина берётся не текущая, а гарантированная: уже неё содержимое не
        станет, дальше включается горизонтальная прокрутка.
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

    # --- ход сбора ---------------------------------------------------------

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
        """Запомнить фазу и когда она началась.

        Часы фазы нужны ровно там, где нет прогресса: «создание контейнера» на
        полном форматировании держится минутами, и без бегущей секунды окно
        неотличимо от повисшего.
        """
        if phase != self._phase:
            self._phase = phase
            self._phase_started = time.monotonic()
        self._detail = detail

    def _advance(self, index: int, share: float) -> None:
        """Полоса — доля пройденного веса от веса всего плана.

        Долю шага считает сам Progress тем же весом, каким взвешен план:
        байты записи плюс поправка на создание файлов.

        Доля запоминается по наибольшей за шаг, и вот почему. Вне записи она
        нулевая — сколько байт VeraCrypt уложил при создании контейнера,
        снаружи не видно, — а после записи идут ещё четыре фазы: сверка,
        перемонтирование, замер остатка, уборка. Не запоминай мы достигнутое,
        полоса на каждой из них откатывалась бы к началу шага.
        """
        if index >= len(self._before) - 1:
            return
        self._share = max(self._share, share)
        done = self._before[index] + int(self._weights[index] * self._share)
        self.progress.setValue(min(done // MIB, self.progress.maximum()))

    def _show_progress(self) -> None:
        """Собрать строку под полосой из того, что известно сейчас.

        Ничего не принимает и зовётся откуда угодно — и от прогресса шага, и
        от таймера раз в секунду: иначе часы шли бы только тогда, когда шаг о
        себе сообщает, а молчит он как раз в самых долгих местах.
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
            # Замер уходит наружу даже при неудавшейся самопроверке: он
            # настоящий, и второй из пары — как раз обычный контейнер.
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
                # Занято меньше самих данных — так не бывает. Чаще всего это
                # значит, что мелкий файл уместился прямо в запись MFT и
                # кластера не получил, а Σ ceil(size / cluster) его посчитал.
                # Замер в калибровку запаса не пойдёт, но точка NTFS из него
                # годится, и выбрасывать её незачем.
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
        """Закрыть поток. Зовётся, когда run() уже вернулся, и не ждёт."""
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

    # --- служебное ---------------------------------------------------------

    def running(self) -> bool:
        return self._thread is not None

    def reject(self) -> None:
        """Закрытие во время сбора не рвёт текущий шаг, а дожидается его.

        Не блокируя окно: закрытие откладывается до конца шага. Оборвать
        VeraCrypt на середине значило бы оставить поднятый том и файл на
        терабайт, а ждать блокирующим wait() — заморозить окно на минуты.
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
        # Сразу на диск: путь к VeraCrypt ищут руками один раз, и терять его
        # из-за того, что программу закрыли не по-хорошему, обидно.
        if self._settings is not None:
            self._settings.setValue(key, value)
            self._settings.sync()

    def _ask_directory(self, title: str, start: str) -> str:
        return QFileDialog.getExistingDirectory(self, title, start)

    def _show_error(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _ask_confirmation(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.Yes
