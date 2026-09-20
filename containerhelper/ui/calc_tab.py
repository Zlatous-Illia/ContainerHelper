"""The Calculation tab: from the input data size to the container size."""

from __future__ import annotations

import os
from typing import Callable, Sequence

from PySide6.QtCore import QItemSelection, QItemSelectionModel, QMimeData, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..model import (
    DEFAULT_CLUSTER_BYTES,
    DEFAULT_SAFETY_BYTES,
    MIB,
    CopySlackModel,
    NtfsModel,
    Payload,
    SafetyAdvice,
    SafetyModel,
    Solution,
    round_up,
    solve_container_mib,
)
from ..formatting import (
    DASH,
    DEFAULT_UNIT,
    UNIT_MIB,
    Unit,
    fmt_both,
    fmt_bytes,
    fmt_table_cell,
    parse_bytes,
    unit_suffix,
)
from ..sizes import ScanResult, scan_paths
from .path_picker import PickerState, ask_paths
from .table import (
    apply_table_height,
    digits_only,
    fit_columns,
    fit_field,
    set_header_tooltips,
    setup_table,
    with_grip,
)

CLUSTER_CHOICES = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)

#: Columns of the per-source split: header, "in bytes", tooltip.
SOURCE_COLUMNS = (
    (
        "Источник",
        False,
        "Что выбрали. Полный путь — в подсказке к строке.",
    ),
    (
        "Тип",
        False,
        "Папка считается целиком, со всем вложенным. Файл — сам по себе.",
    ),
    (
        "Файлов",
        False,
        "Сколько файлов внутри. Чем их больше, тем больше запас на "
        "копирование: на каждый нужна запись MFT и место в индексе.",
    ),
    (
        "Папок",
        False,
        "Сколько вложенных папок обошли. На объём почти не влияет, зато "
        "видно, что обход дошёл до конца.",
    ),
    (
        "Логический размер",
        True,
        "Сумма размеров файлов — столько же показывает Проводник.",
    ),
    (
        "По кластерам",
        True,
        "Сколько данные займут на томе: каждый файл округляется вверх до "
        "кластера. На мелких файлах заметно больше логического размера.",
    ),
)

#: Tooltips for the breakdown columns. The second column is always in bytes —
#: it is the column for reconciliation — and the third follows the chosen
#: unit.
BREAKDOWN_TIPS = (
    "Слагаемое размера контейнера. Строки идут в том порядке, в каком "
    "складываются, «Итого» — их сумма.",
    "Значение в байтах. Единице отображения не подчиняется: по нему сверяют "
    "с тем, что показывают VeraCrypt и Проводник.",
    "То же в выбранной единице. Для «B» показывает MiB — два одинаковых "
    "столбца ни к чему.",
)

#: Tooltips for the breakdown rows — keyed by the term's name. A number in a
#: row with no explanation of where it comes from cannot be checked.
BREAKDOWN_ROW_TIPS = {
    "Полезные данные (по кластерам)": (
        "Сами данные, но каждый файл округлён вверх до кластера — место "
        "файловая система выдаёт только кластерами."
    ),
    "    в том числе кластерный хвост": (
        "Сколько из строки выше ушло на округление. Растёт с числом файлов "
        "и размером кластера."
    ),
    "Заголовок VeraCrypt": (
        "266 240 B — разница между файлом контейнера и ёмкостью тома. На "
        "всех замерах одна и та же."
    ),
    "Метаданные NTFS": (
        "Что файловая система забирает себе: $MFT, $LogFile, $Bitmap и "
        "прочее. Зависит от размера тома, а не от содержимого. Берётся из "
        "замеров на вкладке «Калибровка»."
    ),
    "Запас на копирование": (
        "Сколько файлы занимают сверх своего кластерного размера: запись "
        "MFT на каждый и разрастание индексов каталогов."
    ),
    "Страховочный запас": (
        "Поправка на погрешность моделей. В режиме «Авто» считается под "
        "этот размер тома и это число файлов."
    ),
    "Итого контейнер": (
        "Всё перечисленное, округлённое вверх до целых MiB. Это число и "
        "вводят в VeraCrypt."
    ),
    "Ожидаемый остаток (Left space)": (
        "Сколько места должно остаться на томе после копирования. Проверить "
        "можно кнопкой «Замерить остаток» в записи."
    ),
}

#: Input limits. Bytes — up to a petabyte with digit-group separators, cluster
#: — up to 65536. Without them the field takes a number no storage device
#: exists for.
MAX_BYTES_CHARS = 24
MAX_CLUSTER_CHARS = 7

#: The longest meaningful value for each field — the widget's width is
#: measured by it. A screen-wide cluster field lies about what can be typed
#: there: nothing above 65536 exists.
SAMPLE_BYTES = "1 125 899 906 842 624"
SAMPLE_CLUSTER = "65 536"

#: Line break in a tooltip. A constant, because escaping inside edit
#: templates has already collapsed once.
LINE_BREAK = chr(10)

#: The label under an empty source table. One string for two places: it is
#: set both when building and on every update, and if the two drifted apart
#: they would name the same state in different ways.
NO_SOURCE_HINT = (
    "Источник не выбран: перетащите сюда файлы и папки, выберите их кнопкой "
    "или введите размер вручную."
)

ModelProvider = Callable[[], tuple[NtfsModel, CopySlackModel]]
SafetyProvider = Callable[[], SafetyModel]


class CalcTab(QWidget):
    safetyChanged = Signal(int)
    autoSafetyChanged = Signal(bool)
    #: Recalculated. The chart window of the current calculation updates on
    #: it: it draws exactly what is in the breakdown table right now.
    calculationChanged = Signal()
    #: A request to show the chart window. The main window opens it — the
    #: tabs know nothing about each other or about windows.
    chartRequested = Signal(str)

    def __init__(
        self,
        models: ModelProvider,
        safety: SafetyProvider | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._models = models
        #: Where to get safety margin advice from. None — the fitting is not
        #: available, the field stays manual, as it was before the fitting
        #: appeared.
        self._safety = safety
        self._advice: SafetyAdvice | None = None
        #: The result of the last scan. None — the size was typed by hand,
        #: and there is no per-source split at all.
        self._scan: ScanResult | None = None
        #: The last solution — the same one shown in the breakdown table.
        #: Kept so the chart draws what was shown, not a fresh recalculation:
        #: a recalculation could reach a different answer if the model
        #: changed in the meantime.
        self._solution: Solution | None = None
        self._unit: Unit = DEFAULT_UNIT
        self._sources_fitted = False
        #: What the picker must survive between showings: the view, the
        #: window size, both check boxes and the displayed folder. The main
        #: window keeps it and writes it to the settings — the dialog lives
        #: for one showing.
        self._picker = PickerState()

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_source_group())
        layout.addWidget(self._build_params_group())
        layout.addWidget(self._build_result_group())

        layout.addWidget(with_grip(self._build_breakdown()))
        layout.addWidget(self._build_notes())
        layout.addStretch(1)

        self.setAcceptDrops(True)
        # Input fields accept drops themselves and would paste a dropped path
        # as text right into the size. The field refusing lets the event
        # through — to the tab, where the path becomes a source.
        for field in self.findChildren(QLineEdit):
            field.setAcceptDrops(False)

        self.recalculate()

    # --- building the interface ------------------------------------------

    def _build_source_group(self) -> QGroupBox:
        group = QGroupBox("Исходные данные")
        # No tooltip on the group itself: every widget without its own
        # inherits it, and hovering over any label inside showed a retelling
        # of what is already written under the source table.
        outer = QVBoxLayout(group)
        outer.addLayout(self._build_source_buttons())

        self.source_box = with_grip(self._build_source_table())
        outer.addWidget(self.source_box)

        self.source_label = QLabel(NO_SOURCE_HINT)
        self.source_label.setWordWrap(True)
        self.source_label.setStyleSheet("color: palette(mid);")
        # No tooltip: the label is the summary, and the tooltip retold it.
        outer.addWidget(self.source_label)

        form = QFormLayout()
        self.size_edit = QLineEdit()
        self.size_edit.setPlaceholderText("например 10 941 734 967")
        self.size_edit.setToolTip(
            "Размер данных в байтах; разделители разрядов можно оставить.\n"
            "После выбора источников заполняется сам. Если исправить руками, "
            "связь с выбранными путями теряется, и число файлов придётся "
            "указать самому."
        )
        self.size_edit.setMaxLength(MAX_BYTES_CHARS)
        digits_only(self.size_edit)
        fit_field(self.size_edit, SAMPLE_BYTES)
        self.size_edit.textEdited.connect(self._on_manual_edit)
        form.addRow("Размер, байт:", self.size_edit)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 100_000_000)
        self.count_spin.setValue(1)
        self.count_spin.setToolTip(
            "Сколько файлов. От этого зависит запас: каждый файл занимает "
            "запись MFT и место в индексе каталога."
        )
        self.count_spin.setMaximumWidth(150)
        self.count_spin.valueChanged.connect(self._on_manual_edit)
        form.addRow("Файлов:", self.count_spin)
        outer.addLayout(form)

        return group

    def _build_source_buttons(self) -> QHBoxLayout:
        """One Select button for both kinds of source.

        The split into "file" and "folder" was not the user's choice but a
        retelling of the fact that Windows' native dialogs are built on two
        different system calls. A data set does not divide that way: both
        folders and single files go into a container, and usually together.
        """
        row = QHBoxLayout()

        self.pick_button = QPushButton("Выбрать…")
        self.pick_button.setToolTip(
            "Выбрать файлы и папки — одним списком, любым набором. Заменяет "
            "то, что выбрано сейчас.\n"
            "Папка считается целиком. Вложенные друг в друга пути дважды не "
            "считаются."
        )
        self.pick_button.clicked.connect(self._pick_sources)
        row.addWidget(self.pick_button)

        self.add_button = QPushButton("Добавить…")
        self.add_button.setToolTip(
            "То же окно, но выбранное добавится к уже набранному. За один "
            "раз диалог показывает только одну папку, а данные бывают из "
            "разных мест."
        )
        self.add_button.clicked.connect(self._add_sources)
        row.addWidget(self.add_button)

        self.drop_button = QPushButton("Убрать")
        self.drop_button.setToolTip(
            "Убрать выделенные строки. Ctrl+A выделяет все — так набор "
            "очищается целиком и можно вернуться к ручному вводу."
        )
        self.drop_button.clicked.connect(self._drop_sources)
        row.addWidget(self.drop_button)

        # Selection buttons — the same three as in the picker. All of this is
        # reachable from the keyboard anyway, but there is no point hunting
        # for Ctrl+A in a table that is rarely used, and "invert" has no
        # shortcut at all.
        self.select_buttons: list[QPushButton] = []
        for title, tip, slot in (
            (
                "Выделить всё",
                "Выделить все источники. То же делает Ctrl+A.",
                self.source_table_select_all,
            ),
            (
                "Снять выделение",
                "Снять выделение со всех строк — «Убрать» после этого "
                "выключается.",
                self.source_table_select_none,
            ),
            (
                "Инвертировать",
                "Выделить всё, кроме выделенного сейчас. Так убирают всё, "
                "кроме одного-двух нужных источников.",
                self.source_table_invert,
            ),
        ):
            button = QPushButton(title)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
            self.select_buttons.append(button)

        row.addStretch(1)
        return row

    # --- selection in the source table ------------------------------------

    def source_table_select_all(self) -> None:
        self.source_table.selectAll()

    def source_table_select_none(self) -> None:
        self.source_table.clearSelection()

    def source_table_invert(self) -> None:
        """Invert the selection: deselect the selected rows, select the rest.

        Toggle over the whole table rectangle, not a walk over rows with
        selectRow: without Ctrl held down, selectRow drops the previous
        selection, and only the last row would remain of the inversion.
        """
        rows, columns = self.source_table.rowCount(), self.source_table.columnCount()
        if not rows or not columns:
            return
        model = self.source_table.model()
        whole = QItemSelection(model.index(0, 0), model.index(rows - 1, columns - 1))
        self.source_table.selectionModel().select(
            whole, QItemSelectionModel.Toggle | QItemSelectionModel.Rows
        )

    def _build_source_table(self) -> QTableWidget:
        self.source_table = QTableWidget(0, len(SOURCE_COLUMNS))
        self.source_table.verticalHeader().setVisible(False)
        self.source_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.source_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.source_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.source_table.setToolTip(
            "Что принёс каждый источник. Строки идут в порядке выбора."
        )
        # Row order is the order of selection; sorting would only confuse
        # here, because the row to remove must be exactly the one being looked
        # at.
        setup_table(self.source_table, sortable=False, min_rows=3)
        self.source_table.itemSelectionChanged.connect(self._refresh_drop_button)
        self._apply_source_headers()
        return self.source_table

    def _apply_source_headers(self) -> None:
        self.source_table.setHorizontalHeaderLabels(
            [
                title + (unit_suffix(self._unit) if is_bytes else "")
                for title, is_bytes, _tip in SOURCE_COLUMNS
            ]
        )
        set_header_tooltips(self.source_table, [tip for _t, _b, tip in SOURCE_COLUMNS])

    def _build_params_group(self) -> QGroupBox:
        group = QGroupBox("Параметры")
        form = QFormLayout(group)

        self.cluster_combo = QComboBox()
        self.cluster_combo.setEditable(True)
        for value in CLUSTER_CHOICES:
            self.cluster_combo.addItem(str(value), value)
        self.cluster_combo.setCurrentText(str(DEFAULT_CLUSTER_BYTES))
        self.cluster_combo.lineEdit().setMaxLength(MAX_CLUSTER_CHARS)
        digits_only(self.cluster_combo)
        fit_field(self.cluster_combo.lineEdit(), SAMPLE_CLUSTER)
        self.cluster_combo.setMaximumWidth(
            self.cluster_combo.lineEdit().maximumWidth() + 34
        )
        self.cluster_combo.setToolTip(
            "Шаг, которым файловая система выдаёт место: файл в 1 байт "
            "занимает целый кластер, файл в 4097 при кластере 4096 — два.\n"
            "На одном большом файле выбор почти ничего не меняет, на 500 "
            "мелких — 2 MiB против 32 MiB.\n"
            "Кластеры есть в любой файловой системе, но метаданные мы мерили "
            "только на NTFS."
        )
        self.cluster_combo.currentTextChanged.connect(self.recalculate)
        form.addRow("Размер кластера, B:", self.cluster_combo)

        self.safety_spin = QSpinBox()
        self.safety_spin.setRange(0, 1024)
        self.safety_spin.setValue(DEFAULT_SAFETY_BYTES // MIB)
        self.safety_spin.setSuffix(" MiB")
        self.safety_spin.setToolTip(
            "Запас поверх расчёта — на случай, если модели чуть промахнулись."
        )
        self.safety_spin.setMaximumWidth(110)
        self.safety_spin.valueChanged.connect(self.recalculate)
        self.safety_spin.valueChanged.connect(self.safetyChanged.emit)

        safety_row = QHBoxLayout()
        safety_row.addWidget(self.safety_spin)
        self.auto_safety = QCheckBox("Авто")
        self.auto_safety.setToolTip(
            "Считать запас под этот размер и число файлов, а не держать одно "
            "число на все расчёты."
        )
        # The state is set before the signal is connected: toggled during
        # building would fire recalculate while half the widgets do not exist
        # yet.
        self.auto_safety.setChecked(self._safety is not None)
        self.auto_safety.setEnabled(self._safety is not None)
        self.safety_spin.setReadOnly(self.auto_safety.isChecked())
        self.auto_safety.toggled.connect(self._on_auto_toggled)
        safety_row.addWidget(self.auto_safety)
        safety_row.addStretch(1)
        form.addRow("Страховочный запас:", safety_row)

        self.safety_note = QLabel()
        self.safety_note.setWordWrap(True)
        self.safety_note.setStyleSheet("color: palette(mid);")
        form.addRow("", self.safety_note)

        return group

    def _on_auto_toggled(self, enabled: bool) -> None:
        self.safety_spin.setReadOnly(enabled)
        self.autoSafetyChanged.emit(enabled)
        self.recalculate()

    def set_safety_mib(self, value: int) -> None:
        """Take the value set on the Model tab."""
        if self.safety_spin.value() != value:
            self.safety_spin.blockSignals(True)
            self.safety_spin.setValue(value)
            self.safety_spin.blockSignals(False)
            self.recalculate()

    def _build_result_group(self) -> QGroupBox:
        group = QGroupBox("Container init")
        row = QHBoxLayout(group)

        self.result_label = QLabel("—")
        font = QFont(self.result_label.font())
        font.setPointSize(font.pointSize() + 10)
        font.setBold(True)
        self.result_label.setFont(font)
        self.result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self.result_label)

        self.result_units = QLabel("MiB")
        row.addWidget(self.result_units)
        row.addStretch(1)

        self.copy_button = QPushButton("Копировать")
        self.copy_button.setToolTip("Скопировать число MiB для ввода в VeraCrypt.")
        self.copy_button.clicked.connect(self._copy_result)
        row.addWidget(self.copy_button)

        return group

    def _build_breakdown(self) -> QTableWidget:
        self.table = QTableWidget(0, 3)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setToolTip(
            "Из чего сложился Container init. Строки идут в порядке "
            "сложения, поэтому не сортируются.\n"
            "Наведите на строку — там написано, откуда взялось слагаемое."
        )
        # Row order here is the content itself, hence no sorting.
        setup_table(self.table, sortable=False)
        self._apply_breakdown_headers()
        self._columns_fitted = False
        return self.table

    def _apply_breakdown_headers(self) -> None:
        self.table.setHorizontalHeaderLabels(
            ["Слагаемое", "Байт", self._secondary_unit().label]
        )
        set_header_tooltips(self.table, BREAKDOWN_TIPS)

    def _secondary_unit(self) -> Unit:
        """The unit of the breakdown's third column.

        The second column is always in bytes — it is the column for
        reconciliation. So choosing "B" for the third column would give two
        identical numbers, and it is mapped to MiB, as it was before the
        switch appeared.
        """
        return UNIT_MIB if self._unit.factor <= 1 else self._unit

    def set_expand_tables(self, expand: bool) -> None:
        for table in (self.source_table, self.table):
            apply_table_height(table, expand)

    def set_unit(self, unit: Unit) -> None:
        self._unit = unit
        self._apply_breakdown_headers()
        self._apply_source_headers()
        self.recalculate()

    def _build_notes(self) -> QLabel:
        self.notes_label = QLabel()
        self.notes_label.setWordWrap(True)
        self.notes_label.setTextFormat(Qt.RichText)
        self.notes_label.setAlignment(Qt.AlignTop)
        self.notes_label.setMinimumHeight(48)
        return self.notes_label

    # --- data source -------------------------------------------------------

    def _ask_paths(self) -> list[str]:
        """Show the picker; it edits the state in place.

        The starting folder is chosen by the state itself, not by the selected
        path. It cannot be taken from the selection: after selecting folder 2
        inside folder 1, the next showing moved inside folder 2 — and so one
        level deeper every time.
        """
        return ask_paths(self, self._picker)

    def _pick_sources(self) -> None:
        chosen = self._ask_paths()
        if chosen:
            self._rescan(chosen)

    def _add_sources(self) -> None:
        chosen = self._ask_paths()
        if chosen:
            self._rescan(self._current_paths() + chosen)

    @property
    def picker_state(self) -> PickerState:
        """The picker's state. The main window reads and writes it."""
        return self._picker

    def set_picker_state(self, state: PickerState) -> None:
        self._picker = state

    def _drop_sources(self) -> None:
        """Remove the selected sources and recalculate from the rest."""
        rows = {index.row() for index in self.source_table.selectionModel().selectedRows()}
        if not rows:
            return
        kept = [
            path
            for row, path in enumerate(self._current_paths())
            if row not in rows
        ]
        if kept:
            self._rescan(kept)
        else:
            self._clear_sources()

    def _current_paths(self) -> list[str]:
        return self._scan.paths if self._scan else []

    def _rescan(self, paths: Sequence[str]) -> None:
        """Scan the selection and fill in the fields.

        The scan is synchronous: even on hundreds of thousands of files
        os.scandir finishes in seconds, and a background thread would cost
        more here than it saved.
        """
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = scan_paths(paths, self._cluster())
        finally:
            QApplication.restoreOverrideCursor()

        self._scan = result
        payload = result.payload
        self.size_edit.setText(fmt_bytes(payload.logical_bytes))
        self.count_spin.blockSignals(True)
        self.count_spin.setValue(max(payload.file_count, 1))
        self.count_spin.blockSignals(False)
        self.recalculate()

    def _clear_sources(self) -> None:
        """Everything removed — reset the fields too, not only the table.

        Otherwise a removed source leaves its size and file count behind,
        and the calculation keeps counting what is no longer in the set: the
        label says "no source selected", while Container init stays as it
        was. There is no way to tell this from manual input — it uses the
        same fields.
        """
        self._scan = None
        self.size_edit.clear()
        # One, not zero: a data set has no fewer than one file, and the
        # field's lower bound already says so.
        self.count_spin.blockSignals(True)
        self.count_spin.setValue(self.count_spin.minimum())
        self.count_spin.blockSignals(False)
        self.recalculate()

    # --- drag and drop -----------------------------------------------------

    @staticmethod
    def _dropped_paths(mime: QMimeData) -> list[str]:
        """Dropped paths. Anything without a file on disk is thrown away.

        Drops come not only from Explorer: a link from a browser and an
        attachment from mail arrive with the same mime type, but there is no
        path behind them, and only a path can be a source.
        """
        if not mime.hasUrls():
            return []
        paths = [url.toLocalFile() for url in mime.urls()]
        return [path for path in paths if path and os.path.exists(path)]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Accept the drag if there are paths on disk behind it."""
        if self._dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    #: Acceptance is confirmed on every cursor move too: the base
    #: dragMoveEvent implementation does nothing, and without this the cursor
    #: over the tab would show a "forbidden" sign.
    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event: QDropEvent) -> None:
        """What is dropped adds to the set, it does not replace it.

        Replacing is the Select… button, and it shows a dialog. A drop asks
        nothing, and silently losing what was gathered is not allowed: an
        extra source is removed with one button, a lost one has to be
        gathered again.
        """
        paths = self._dropped_paths(event.mimeData())
        if not paths:
            return
        event.acceptProposedAction()
        self._rescan(self._current_paths() + paths)

    def _on_manual_edit(self, *_args) -> None:
        """A manual edit detaches the calculation from the scanned paths."""
        self._scan = None
        self.recalculate()

    # --- per-source split -------------------------------------------------

    def _refresh_drop_button(self) -> None:
        selected = bool(self.source_table.selectionModel().selectedRows())
        self.drop_button.setEnabled(selected)

    def _refresh_sources(self) -> None:
        """The source table and its summary.

        Hidden when there is nothing to select.
        """
        sources = self._scan.sources if self._scan else []
        self.source_box.setVisible(bool(sources))
        self.drop_button.setVisible(bool(sources))
        # The selection buttons hide together with the table: there is nothing
        # to select in a hidden one, and a row of buttons during manual input
        # would be empty noise.
        for button in self.select_buttons:
            button.setVisible(bool(sources))

        cluster = self._cluster()
        self.source_table.setRowCount(len(sources))
        for row, source in enumerate(sources):
            cells = (
                source.name,
                "папка" if source.is_dir else "файл",
                fmt_bytes(source.file_count),
                fmt_bytes(source.dir_count) if source.is_dir else DASH,
                fmt_table_cell(source.logical_bytes, self._unit),
                fmt_table_cell(source.alloc_bytes(cluster), self._unit),
            )
            tooltip = source.path
            if source.errors:
                tooltip += f"{LINE_BREAK}Не прочитано путей: {len(source.errors)}."
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setToolTip(tooltip)
                self.source_table.setItem(row, column, item)

        if sources and not self._sources_fitted:
            fit_columns(self.source_table)
            self._sources_fitted = True
        self._refresh_drop_button()
        self._refresh_source_label(cluster)

    def _refresh_source_label(self, cluster: int) -> None:
        if self._scan is None:
            self.source_label.setText(NO_SOURCE_HINT)
            return

        stats = self._scan.stats(cluster)
        parts = [
            f"Выбрано: {stats.sources} "
            f"({stats.folders} папок, {stats.files} файлов).",
            f"Внутри файлов: {fmt_bytes(stats.file_count)}, "
            f"вложенных папок: {fmt_bytes(stats.dir_count)}.",
            f"Логический размер {fmt_bytes(stats.logical_bytes)} B, "
            f"по кластерам {fmt_bytes(stats.alloc_bytes)} B "
            f"(хвост {fmt_bytes(stats.cluster_tail)} B).",
        ]
        if stats.file_count:
            parts.append(
                f"Файл: самый большой {fmt_bytes(stats.largest_bytes)} B, "
                f"самый малый {fmt_bytes(stats.smallest_bytes)} B, "
                f"в среднем {fmt_bytes(stats.average_bytes)} B."
            )
        if stats.empty_files:
            parts.append(
                f"Пустых файлов: {fmt_bytes(stats.empty_files)} — места по "
                f"кластерам не занимают, но запись MFT каждому нужна."
            )
        self.source_label.setText(" ".join(parts))

    # --- handing out -------------------------------------------------------

    def current_payload(self) -> Payload | None:
        """What the tab has calculated right now — for carrying into a record.

        Handed out whole, together with alloc_bytes: for a folder of many
        files only it describes the space actually taken.
        """
        payload = self._payload()
        if payload is None or payload.logical_bytes <= 0:
            return None
        return payload

    def current_container_mib(self) -> int | None:
        """The calculated Container init.

        So it need not be typed into a record by hand.
        """
        value = parse_bytes(self.result_label.text())
        return value or None

    def current_safety_mib(self) -> int:
        """How much safety margin is built into this calculation.

        Goes into the record together with the prediction: without it the
        miss cannot be split into model error and deliberate margin, and it
        must be split — an underestimate covered by the safety margin looks
        healthy right up to the day the safety margin is not enough.
        """
        return self.safety_spin.value()

    def current_sources(self) -> list[str]:
        """Paths the payload came from. Empty — the size was typed by hand.

        A list, not a single path: there can now be several sources, and
        returning the first of them would lie about the second.
        """
        return self._current_paths()

    def current_stats(self):
        """Statistics on the selection — None with manual input."""
        return self._scan.stats(self._cluster()) if self._scan else None

    def current_solution(self) -> Solution | None:
        """The breakdown shown right now — for the bar chart."""
        return self._solution

    def current_file_sizes(self) -> list[int]:
        """Sizes of the selected files. Empty — the size was typed by hand.

        The cluster tail chart is the only one computed from the real files,
        not from the model, and without their sizes it has nothing to show.
        """
        return list(self._scan.sizes) if self._scan else []

    def current_cluster(self) -> int:
        return self._cluster()

    # --- calculation -------------------------------------------------------

    def _cluster(self) -> int:
        value = parse_bytes(self.cluster_combo.currentText())
        if not value or value <= 0:
            return DEFAULT_CLUSTER_BYTES
        return value

    def _payload(self) -> Payload | None:
        cluster = self._cluster()
        if self._scan is not None:
            return self._scan.with_cluster(cluster)

        logical = parse_bytes(self.size_edit.text())
        if logical is None:
            return None
        return Payload(
            logical_bytes=logical,
            alloc_bytes=round_up(logical, cluster),
            file_count=self.count_spin.value(),
            cluster_bytes=cluster,
        )

    def _advise_safety(self, payload: Payload, ntfs, slack) -> None:
        """Fit the safety margin to this calculation and put it in the field.

        The advice depends on the volume size, and the volume on the safety
        margin, so first we solve with what is set now, then refine. A second
        pass is not needed: the safety margin shifts the volume by single
        megabytes, and the advice is computed over a segment between
        measurements gigabytes wide.
        """
        if self._safety is None or not self.auto_safety.isChecked():
            return
        probe = solve_container_mib(
            payload, ntfs=ntfs, slack=slack,
            safety_bytes=self.safety_spin.value() * MIB,
        )
        self._advice = self._safety().advise(probe.volume_bytes, payload.file_count)
        # Directly, without set_safety_mib: that one restarts recalculate, and
        # we have already been called from inside it.
        if self.safety_spin.value() != self._advice.total_mib:
            self.safety_spin.blockSignals(True)
            self.safety_spin.setValue(self._advice.total_mib)
            self.safety_spin.blockSignals(False)
            self.safetyChanged.emit(self._advice.total_mib)

    def recalculate(self) -> None:
        self._refresh_sources()
        payload = self._payload()
        if payload is None or payload.logical_bytes <= 0:
            self.result_label.setText("—")
            self.table.setRowCount(0)
            self.notes_label.setText("")
            self._refresh_safety_note()
            self._solution = None
            self.calculationChanged.emit()
            return

        ntfs, slack = self._models()
        self._advise_safety(payload, ntfs, slack)
        solution = solve_container_mib(
            payload,
            ntfs=ntfs,
            slack=slack,
            safety_bytes=self.safety_spin.value() * MIB,
        )

        self._solution = solution
        self.result_label.setText(fmt_bytes(solution.container_mib))
        self._fill_breakdown(solution)
        self._fill_notes(solution, payload)
        self._refresh_safety_note()
        self.calculationChanged.emit()

    def _refresh_safety_note(self) -> None:
        """Explain where the number came from, not just show it."""
        if not self.auto_safety.isChecked():
            self.safety_note.setText("Задан вручную.")
            return
        if self._safety is None or self._advice is None:
            self.safety_note.setText("Подбирается по записям — расчёт пока пуст.")
            return

        advice = self._advice
        parts = [
            f"NTFS {fmt_both(advice.ntfs_bytes)} — {advice.ntfs_reason};",
            f"копирование {fmt_both(advice.slack_bytes)} — {advice.slack_reason}.",
        ]
        if advice.basis:
            parts.append("Записи рядом: " + ", ".join(advice.basis) + ".")
        self.safety_note.setText(" ".join(parts))

    def _fill_breakdown(self, solution) -> None:
        rows = [
            ("Полезные данные (по кластерам)", solution.payload_alloc),
            ("    в том числе кластерный хвост", solution.cluster_tail),
            ("Заголовок VeraCrypt", solution.vc_header),
            ("Метаданные NTFS", solution.ntfs_bytes),
            ("Запас на копирование", solution.copy_slack),
            ("Страховочный запас", solution.safety_bytes),
            ("Итого контейнер", solution.container_bytes),
            ("Ожидаемый остаток (Left space)", solution.predicted_left_bytes),
        ]

        self.table.setRowCount(len(rows))
        for row, (name, value) in enumerate(rows):
            tooltip = BREAKDOWN_ROW_TIPS.get(name, "")
            title = QTableWidgetItem(name)
            if name.startswith("Итого"):
                font = QFont(title.font())
                font.setBold(True)
                title.setFont(font)
            title.setToolTip(tooltip)
            self.table.setItem(row, 0, title)

            secondary = fmt_table_cell(value, self._secondary_unit())
            for column, text in ((1, fmt_bytes(value)), (2, secondary)):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setToolTip(tooltip)
                self.table.setItem(row, column, item)

        if not self._columns_fitted:
            fit_columns(self.table)
            self._columns_fitted = True

    def _fill_notes(self, solution, payload: Payload) -> None:
        notes: list[str] = []

        if solution.ntfs_extrapolated:
            notes.append(
                "Размер вне диапазона замеров, метаданные NTFS посчитаны "
                "экстраполяцией. Снимите замер рядом с этим размером — хватит "
                "пустого контейнера."
            )
        if solution.slack_unverified:
            notes.append(
                f"Запас на копирование для {payload.file_count} файлов "
                f"({fmt_both(solution.copy_slack)}) взят по умолчанию: "
                f"записей с несколькими файлами пока нет."
            )
        if self._scan is None and payload.file_count > 1:
            notes.append(
                "Размер введён вручную для нескольких файлов. Округление "
                "до кластера посчитано один раз на всю сумму, а не на каждый "
                "файл, поэтому на деле выйдет больше. Выберите папку — "
                "посчитаем точно."
            )
        errors = self._scan.errors if self._scan else []
        if errors:
            shown = "<br>".join(errors[:5])
            more = (
                f"<br>…и ещё {len(errors) - 5}"
                if len(errors) > 5
                else ""
            )
            notes.append(f"Часть данных не прочитана, расчёт занижен:<br>{shown}{more}")

        self.notes_label.setText(
            "<br><br>".join(f"⚠ {note}" for note in notes) if notes else ""
        )

    def _copy_result(self) -> None:
        """Clipboard gets the bare MiB number — typed into VeraCrypt as is."""
        value = parse_bytes(self.result_label.text())
        if value is not None:
            QGuiApplication.clipboard().setText(str(value))
