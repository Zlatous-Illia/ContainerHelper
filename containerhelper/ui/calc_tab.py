"""The Calculation tab: from the input data size to the container size."""

from __future__ import annotations

import os
from typing import Callable, Sequence

from PySide6.QtCore import (
    QEvent,
    QItemSelection,
    QItemSelectionModel,
    QMimeData,
    Qt,
    Signal,
)
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
    MetadataModel,
    Payload,
    SafetyAdvice,
    SafetyModel,
    Solution,
    fit_safety,
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
from ..i18n import tr, tr_n
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

#: Columns of the per-source split: header key, "in bytes", tooltip key.
SOURCE_COLUMNS = (
    ("calc.src.col.source", False, "calc.src.col.source.tip"),
    ("calc.src.col.kind", False, "calc.src.col.kind.tip"),
    ("calc.src.col.files", False, "calc.src.col.files.tip"),
    ("calc.src.col.folders", False, "calc.src.col.folders.tip"),
    ("calc.src.col.logical", True, "calc.src.col.logical.tip"),
    ("calc.src.col.alloc", True, "calc.src.col.alloc.tip"),
)

#: The three selection buttons over the source table: title key, tooltip
#: key. Their slots are paired in the order given here.
SELECT_BUTTONS = (
    ("calc.select.all", "calc.select.all.tip"),
    ("calc.select.none", "calc.select.none.tip"),
    ("calc.select.invert", "calc.select.invert.tip"),
)

#: Tooltips for the breakdown columns. The second column is always in bytes —
#: it is the column for reconciliation — and the third follows the chosen
#: unit.
BREAKDOWN_TIPS = (
    "calc.col.component.tip",
    "calc.col.bytes.tip",
    "calc.col.unit.tip",
)

#: Tooltips for the breakdown rows — keyed by the component's key. A number
#: in a row with no explanation of where it comes from cannot be checked.
BREAKDOWN_ROW_TIPS = {
    "calc.row.payload": "calc.row.payload.tip",
    "calc.row.cluster_tail": "calc.row.cluster_tail.tip",
    "calc.row.vc_header": "calc.row.vc_header.tip",
    "calc.row.metadata": "calc.row.metadata.tip",
    "calc.row.copy_slack": "calc.row.copy_slack.tip",
    "calc.row.safety": "calc.row.safety.tip",
    "calc.row.total": "calc.row.total.tip",
    "calc.row.left": "calc.row.left.tip",
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
NO_SOURCE_HINT = "calc.source.none"

ModelProvider = Callable[[], tuple[MetadataModel, CopySlackModel]]
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

        # Sets the static text and recalculates, which fills the rest.
        self.retranslate()

    def retranslate(self) -> None:
        """Set the tab's text in the current language.

        The text built from data is rebuilt by `recalculate`, which keeps the
        selection, the column widths and what was typed: it only refills the
        cells and labels.
        """
        self.source_group.setTitle(tr("calc.group.source"))
        self.size_label.setText(tr("calc.size"))
        self.size_edit.setPlaceholderText(tr("calc.size.placeholder"))
        self.size_edit.setToolTip(tr("calc.size.tip"))
        self.count_label.setText(tr("calc.count"))
        self.count_spin.setToolTip(tr("calc.count.tip"))
        self.pick_button.setText(tr("calc.pick"))
        self.pick_button.setToolTip(tr("calc.pick.tip"))
        self.add_button.setText(tr("calc.add"))
        self.add_button.setToolTip(tr("calc.add.tip"))
        self.drop_button.setText(tr("calc.drop"))
        self.drop_button.setToolTip(tr("calc.drop.tip"))
        for button, (title, tip) in zip(self.select_buttons, SELECT_BUTTONS):
            button.setText(tr(title))
            button.setToolTip(tr(tip))
        self.source_table.setToolTip(tr("calc.src.tip"))
        self.params_group.setTitle(tr("calc.group.params"))
        self.cluster_label.setText(tr("calc.cluster"))
        self.cluster_combo.setToolTip(tr("calc.cluster.tip"))
        self.safety_label.setText(tr("calc.safety"))
        self.safety_spin.setToolTip(tr("calc.safety.tip"))
        self.auto_safety.setText(tr("calc.auto_safety"))
        self.auto_safety.setToolTip(tr("calc.auto_safety.tip"))
        self.copy_button.setText(tr("calc.copy"))
        self.copy_button.setToolTip(tr("calc.copy.tip"))
        self.table.setToolTip(tr("calc.breakdown.tip"))
        self._apply_source_headers()
        self._apply_breakdown_headers()
        self.recalculate()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    # --- building the interface ------------------------------------------

    def _build_source_group(self) -> QGroupBox:
        group = self.source_group = QGroupBox()
        # No tooltip on the group itself: every widget without its own
        # inherits it, and hovering over any label inside showed a retelling
        # of what is already written under the source table.
        outer = QVBoxLayout(group)
        outer.addLayout(self._build_source_buttons())

        self.source_box = with_grip(self._build_source_table())
        outer.addWidget(self.source_box)

        self.source_label = QLabel()
        self.source_label.setWordWrap(True)
        self.source_label.setStyleSheet("color: palette(mid);")
        # No tooltip: the label is the summary, and the tooltip retold it.
        outer.addWidget(self.source_label)

        form = QFormLayout()
        self.size_edit = QLineEdit()
        self.size_edit.setMaxLength(MAX_BYTES_CHARS)
        digits_only(self.size_edit)
        fit_field(self.size_edit, SAMPLE_BYTES)
        self.size_edit.textEdited.connect(self._on_manual_edit)
        self.size_label = QLabel()
        self.size_label.setBuddy(self.size_edit)
        form.addRow(self.size_label, self.size_edit)

        self.count_spin = QSpinBox()
        self.count_spin.setRange(1, 100_000_000)
        self.count_spin.setValue(1)
        self.count_spin.setMaximumWidth(150)
        self.count_spin.valueChanged.connect(self._on_manual_edit)
        self.count_label = QLabel()
        self.count_label.setBuddy(self.count_spin)
        form.addRow(self.count_label, self.count_spin)
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

        self.pick_button = QPushButton()
        self.pick_button.clicked.connect(self._pick_sources)
        row.addWidget(self.pick_button)

        self.add_button = QPushButton()
        self.add_button.clicked.connect(self._add_sources)
        row.addWidget(self.add_button)

        self.drop_button = QPushButton()
        self.drop_button.clicked.connect(self._drop_sources)
        row.addWidget(self.drop_button)

        # Selection buttons — the same three as in the picker. All of this is
        # reachable from the keyboard anyway, but there is no point hunting
        # for Ctrl+A in a table that is rarely used, and "invert" has no
        # shortcut at all.
        self.select_buttons: list[QPushButton] = []
        for slot in (
            self.source_table_select_all,
            self.source_table_select_none,
            self.source_table_invert,
        ):
            button = QPushButton()
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
                tr(title) + (unit_suffix(self._unit) if is_bytes else "")
                for title, is_bytes, _tip in SOURCE_COLUMNS
            ]
        )
        set_header_tooltips(
            self.source_table, [tr(tip) for _t, _b, tip in SOURCE_COLUMNS]
        )

    def _build_params_group(self) -> QGroupBox:
        group = self.params_group = QGroupBox()
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
        self.cluster_combo.currentTextChanged.connect(self.recalculate)
        self.cluster_label = QLabel()
        self.cluster_label.setBuddy(self.cluster_combo)
        form.addRow(self.cluster_label, self.cluster_combo)

        self.safety_spin = QSpinBox()
        self.safety_spin.setRange(0, 1024)
        self.safety_spin.setValue(DEFAULT_SAFETY_BYTES // MIB)
        self.safety_spin.setSuffix(" MiB")
        self.safety_spin.setMaximumWidth(110)
        self.safety_spin.valueChanged.connect(self.recalculate)
        self.safety_spin.valueChanged.connect(self.safetyChanged.emit)

        safety_row = QHBoxLayout()
        safety_row.addWidget(self.safety_spin)
        self.auto_safety = QCheckBox()
        # The state is set before the signal is connected: toggled during
        # building would fire recalculate while half the widgets do not exist
        # yet.
        self.auto_safety.setChecked(self._safety is not None)
        self.auto_safety.setEnabled(self._safety is not None)
        self.safety_spin.setReadOnly(self.auto_safety.isChecked())
        self.auto_safety.toggled.connect(self._on_auto_toggled)
        safety_row.addWidget(self.auto_safety)
        safety_row.addStretch(1)
        self.safety_label = QLabel()
        form.addRow(self.safety_label, safety_row)

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

        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self._copy_result)
        row.addWidget(self.copy_button)

        return group

    def _build_breakdown(self) -> QTableWidget:
        self.table = QTableWidget(0, 3)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        # Row order here is the content itself, hence no sorting.
        setup_table(self.table, sortable=False)
        self._apply_breakdown_headers()
        self._columns_fitted = False
        return self.table

    def _apply_breakdown_headers(self) -> None:
        self.table.setHorizontalHeaderLabels(
            [
                tr("calc.col.component"),
                tr("calc.col.bytes"),
                self._secondary_unit().label,
            ]
        )
        set_header_tooltips(self.table, [tr(tip) for tip in BREAKDOWN_TIPS])

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
                tr("calc.kind.folder") if source.is_dir else tr("calc.kind.file"),
                fmt_bytes(source.file_count),
                fmt_bytes(source.dir_count) if source.is_dir else DASH,
                fmt_table_cell(source.logical_bytes, self._unit),
                fmt_table_cell(source.alloc_bytes(cluster), self._unit),
            )
            tooltip = source.path
            if source.errors:
                tooltip += LINE_BREAK + tr(
                    "calc.src.unread", count=len(source.errors)
                )
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
            self.source_label.setText(tr(NO_SOURCE_HINT))
            return

        stats = self._scan.stats(cluster)
        parts = [
            tr(
                "calc.source.selected",
                sources=stats.sources,
                folders=stats.folders,
                files=stats.files,
            ),
            tr(
                "calc.source.inside",
                files=fmt_bytes(stats.file_count),
                folders=fmt_bytes(stats.dir_count),
            ),
            tr(
                "calc.source.sizes",
                logical=fmt_bytes(stats.logical_bytes),
                alloc=fmt_bytes(stats.alloc_bytes),
                tail=fmt_bytes(stats.cluster_tail),
            ),
        ]
        if stats.file_count:
            parts.append(
                tr(
                    "calc.source.file_sizes",
                    largest=fmt_bytes(stats.largest_bytes),
                    smallest=fmt_bytes(stats.smallest_bytes),
                    average=fmt_bytes(stats.average_bytes),
                )
            )
        if stats.empty_files:
            parts.append(
                tr("calc.source.empty_files", count=fmt_bytes(stats.empty_files))
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

        `fit_safety` starts from a fixed seed, not from the field: the field
        holds the previous calculation's answer, and seeding with it gave one
        input two answers in turn.
        """
        if self._safety is None or not self.auto_safety.isChecked():
            return
        self._advice = fit_safety(payload, ntfs, slack, self._safety())
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
            self.safety_note.setText(tr("calc.safety.manual"))
            return
        if self._safety is None or self._advice is None:
            self.safety_note.setText(tr("calc.safety.empty"))
            return

        advice = self._advice
        parts = [
            tr(
                "calc.safety.metadata",
                size=fmt_both(advice.metadata_bytes),
                reason=advice.ntfs_reason,
            ),
            tr(
                "calc.safety.slack",
                size=fmt_both(advice.slack_bytes),
                reason=advice.slack_reason,
            ),
        ]
        if advice.basis:
            parts.append(tr("calc.safety.basis", names=", ".join(advice.basis)))
        self.safety_note.setText(" ".join(parts))

    def _fill_breakdown(self, solution) -> None:
        rows = [
            ("calc.row.payload", solution.payload_alloc),
            ("calc.row.cluster_tail", solution.cluster_tail),
            ("calc.row.vc_header", solution.vc_header),
            ("calc.row.metadata", solution.metadata_bytes),
            ("calc.row.copy_slack", solution.copy_slack),
            ("calc.row.safety", solution.safety_bytes),
            ("calc.row.total", solution.container_bytes),
            ("calc.row.left", solution.predicted_left_bytes),
        ]

        self.table.setRowCount(len(rows))
        for row, (key, value) in enumerate(rows):
            tooltip = tr(BREAKDOWN_ROW_TIPS[key])
            title = QTableWidgetItem(tr(key))
            if key == "calc.row.total":
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
            notes.append(tr("calc.note.extrapolated"))
        if solution.slack_unverified:
            notes.append(
                tr_n(
                    "calc.note.slack_default",
                    payload.file_count,
                    size=fmt_both(solution.copy_slack),
                )
            )
        if self._scan is None and payload.file_count > 1:
            notes.append(tr("calc.note.manual_many"))
        errors = self._scan.errors if self._scan else []
        if errors:
            shown = "<br>".join(errors[:5])
            more = (
                tr("calc.note.unread.more", count=len(errors) - 5)
                if len(errors) > 5
                else ""
            )
            notes.append(tr("calc.note.unread", shown=shown, more=more))

        self.notes_label.setText(
            "<br><br>".join(f"⚠ {note}" for note in notes) if notes else ""
        )

    def _copy_result(self) -> None:
        """Clipboard gets the bare MiB number — typed into VeraCrypt as is."""
        value = parse_bytes(self.result_label.text())
        if value is not None:
            QGuiApplication.clipboard().setText(str(value))
