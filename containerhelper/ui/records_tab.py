"""The Records tab: the store of measurements and editing."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..formatting import (
    DASH,
    DEFAULT_UNIT,
    Unit,
    fmt_bytes,
    fmt_table_cell,
    unit_suffix,
)
from ..i18n import tr, tr_n
from ..model import MetadataModel, Payload
from ..records import Record, Store, StoreError, forecast, validate
from .record_dialog import RecordDialog
from .chart_window import CHART_FORECAST
from .table import (
    SortableItem,
    apply_table_height,
    fit_columns,
    set_header_tooltips,
    set_restore_order,
    setup_table,
    with_grip,
)

#: The column kind determines both the formatting and the sort key. String
#: comparison does not work here: "9 000" would land after "10 000 000".
COL_TEXT = "text"
COL_COUNT = "count"
COL_BYTES = "bytes"
#: A signed value in MiB. A kind of its own, because the sign carries all the
#: meaning here: minus means the data would not have fit, and it must not be
#: lost in the common formatting.
COL_SIGNED = "signed"

#: Title key, kind and tooltip key. The kind determines both the formatting
#: and whether the chosen unit is appended to the title. The tooltip is
#: mandatory: the titles are short out of necessity, otherwise the columns do
#: not fit into the window. Keys, not text: the text is taken when shown.
COLUMNS = (
    ("records.col.name", COL_TEXT, "records.col.name.tip"),
    ("records.col.container", COL_COUNT, "records.col.container.tip"),
    ("records.col.capacity", COL_BYTES, "records.col.capacity.tip"),
    ("records.col.metadata", COL_BYTES, "records.col.metadata.tip"),
    ("records.col.deviation", COL_BYTES, "records.col.deviation.tip"),
    ("records.col.files", COL_COUNT, "records.col.files.tip"),
    ("records.col.left", COL_BYTES, "records.col.left.tip"),
    ("records.col.miss", COL_SIGNED, "records.col.miss.tip"),
    ("records.col.model_miss", COL_SIGNED, "records.col.model_miss.tip"),
    ("records.col.status", COL_TEXT, "records.col.status.tip"),
)

#: The role under which a row's first cell holds the record's index in the
#: store. With sorting on, the row number in the table stops matching it, and
#: without this role editing and deleting would hit the wrong record.
STORE_INDEX_ROLE = Qt.UserRole

#: The line break in a tooltip. Kept in a constant: the escaping inside the
#: edit templates for this file has already collapsed once.
LINE_BREAK = chr(10)


class RecordsTab(QWidget):
    recordsChanged = Signal()
    #: A request to show a chart window, by the window's key. The main window
    #: opens it: the tabs know neither about each other nor about the windows.
    chartRequested = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        payload_provider: Callable[[], Payload | None] | None = None,
        container_provider: Callable[[], int | None] | None = None,
        safety_provider: Callable[[], int | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.store = Store(path=Path("records.json"))
        self._unit: Unit = DEFAULT_UNIT
        #: Passed through to the record dialog: the application does not know
        #: the size and the file count by itself, the Calculation tab computes
        #: them.
        self._payload_provider = payload_provider
        self._container_provider = container_provider
        #: How much safety margin the calculation had. Stored in the record for
        #: analysing the miss: without it, an underestimate covered by the
        #: safety margin cannot be told apart from an exact hit.
        self._safety_provider = safety_provider
        #: Showing errors and confirmations is moved into replaceable handlers:
        #: a test has no way to close a modal dialog in the middle of the
        #: logic.
        self.report_error = self._show_error
        self.confirm = self._ask_confirmation
        #: The uncalibrated model is the only baseline against which the
        #: deviation stays meaningful. The calibrated model passes exactly
        #: through its own points, and the deviation in the table would always
        #: be zero.
        self._baseline = MetadataModel()
        #: Open edit windows: id(record) → window. The windows are modeless,
        #: and there must be at most one per record: two windows on one row are
        #: a race won by whoever presses Save last, and there is no way to
        #: notice the lost edit.
        self._editors: dict[int, RecordDialog] = {}
        #: Windows of new calibration points: container size → window. A
        #: separate registry, not a second kind of key in `_editors`: there is
        #: no record to take `id` of yet, and the size is what such a window
        #: measures. Two kinds of key in one `dict[int, RecordDialog]` are
        #: unreadable, and the annotation cannot express which kind a key is.
        self._points: dict[int, RecordDialog] = {}
        #: Windows for new records. Any number of them can be open: until a
        #: record is saved, they have nothing to get in each other's way with.
        self._creators: list[RecordDialog] = []

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_path_row())
        layout.addLayout(self._build_buttons())

        # The table's height belongs to the user: it is dragged by the height
        # grip under the table's own bottom edge.
        layout.addWidget(with_grip(self._build_table()))
        layout.addWidget(self._build_summary())
        layout.addStretch(1)
        self.retranslate()

    def retranslate(self) -> None:
        """Set every static text in the current language, and rebuild the
        table and the summary in it; sorting, selection and widths stay."""
        self.path_caption.setText(tr("records.path"))
        self.path_caption.setToolTip(tr("records.path.tip"))
        self.path_label.setToolTip(tr("records.path.backup.tip"))
        self.choose_button.setText(tr("records.choose"))
        self.choose_button.setToolTip(tr("records.choose.tip"))
        for button, title, tip in self._action_buttons:
            button.setText(tr(title))
            button.setToolTip(tr(tip))
        self.chart_button.setText(tr("records.chart"))
        self.chart_button.setToolTip(tr("records.chart.tip"))
        self.table.setToolTip(tr("records.table.tip"))
        self.summary_label.setToolTip(tr("records.summary.tip"))
        self._apply_headers()
        self.refresh()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    # --- construction ------------------------------------------------------

    def _build_path_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.path_caption = QLabel()
        row.addWidget(self.path_caption)
        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setStyleSheet("color: palette(mid);")
        row.addWidget(self.path_label, 1)

        self.choose_button = QPushButton()
        self.choose_button.clicked.connect(self._choose_path)
        row.addWidget(self.choose_button)
        return row

    def add_calibration_point(self, container_mib: int) -> RecordDialog:
        """Create a calibration point for the given size.

        The size is filled in and the name is left to the record, which builds
        it from the size when shown — all that is needed from the user is the
        measurement of the mounted empty volume.

        The window is modeless, as for an ordinary record: the measurement is
        taken while looking at the mounted volume and at the Calculation tab,
        and a modal window covers both.
        """
        opened = self._points.get(container_mib)
        if opened is not None:
            return self._raise(opened)
        seed = Record(id="", container_mib=container_mib)
        dialog = RecordDialog(seed, parent=self, calibration=True)
        # Keyed by the container size, not id(seed): the point of the window is
        # which size it measures, and a second window for the same size would
        # take a second measurement on top of the first.
        self._points[container_mib] = dialog
        dialog.finished.connect(
            lambda code, key=container_mib, box=dialog: self._point_closed(key, box, code)
        )
        return self._show(dialog)

    def _point_closed(self, key: int, dialog: RecordDialog, code: int) -> None:
        self._points.pop(key, None)
        if code == QDialog.Accepted:
            self.store_calibration_point(dialog.result_record())
        dialog.deleteLater()

    # --- modeless edit windows ---------------------------------------------

    def _show(self, dialog: RecordDialog) -> RecordDialog:
        """Show the window without handing control over to it.

        `show`, not `exec`: while the window is open, the Calculation tab stays
        alive — and it is the source of the size and the file count, so
        pressing Take from Calculation without it is pointless.
        """
        dialog.show()
        return self._raise(dialog)

    @staticmethod
    def _raise(dialog: RecordDialog) -> RecordDialog:
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def _new_dialog(self, record: Record | None = None) -> RecordDialog:
        return RecordDialog(
            record,
            parent=self,
            payload_provider=self._payload_provider,
            container_provider=self._container_provider,
            safety_provider=self._safety_provider,
        )

    def open_record(self, record: Record) -> RecordDialog:
        """Open a record for editing; one already open is raised instead."""
        opened = self._editors.get(id(record))
        if opened is not None:
            return self._raise(opened)
        dialog = self._new_dialog(record)
        self._editors[id(record)] = dialog
        dialog.finished.connect(
            lambda code, item=record, box=dialog: self._editor_closed(item, box, code)
        )
        return self._show(dialog)

    def _editor_closed(self, record: Record, dialog: RecordDialog, code: int) -> None:
        """The window closed. What was saved goes into the very same record.

        The position is found by identity at the moment of saving, not
        remembered at opening: while the window was open, a neighbouring record
        may have been deleted from another window, and the number would already
        point at someone else's row.
        """
        self._editors.pop(id(record), None)
        dialog.deleteLater()
        if code != QDialog.Accepted:
            return
        index = self.store.index_of(record)
        if index is None:
            self.report_error(
                tr("records.lost.title"), tr("records.lost.text", id=record.name)
            )
            return
        self.store.replace_at(index, dialog.result_record())
        self._save()

    def close_editors(self) -> None:
        """Close all edit windows. Called when the records file changes.

        An open window holds a record of the **previous** store: there is
        nowhere to save it in the new one, and showing it next to someone
        else's table would be lying about what is being edited.
        """
        for dialog in [
            *self._editors.values(),
            *self._points.values(),
            *self._creators,
        ]:
            dialog.close()
        self._editors.clear()
        self._points.clear()
        self._creators.clear()

    def store_calibration_point(self, record: Record) -> None:
        """Save a measurement, superseding the previous one of the same kind.

        It goes into the measurements file. A measurement for the same volume
        size replaces the previous one instead of lying next to it: the model
        has no use for two points on one volume. A copy-slack measurement
        supersedes the one of the same file set (`records.slack_key`) — by a
        key of its own, because a shared key would knock out an NTFS point
        with a copy-slack measurement taken at the same volume size, and vice
        versa.

        Saved right away, one measurement at a time: automatic collection runs
        for hours, and a crash halfway must not cost everything already
        measured.
        """
        self.store.put_calibration(record)
        self._save()

    def remove_calibration(self, record: Record) -> None:
        """Remove a measurement from the measurements file, by identity.

        Not by number: the number depends on the order in the file, and that
        changes on every save — put_calibration moves a replaced measurement to
        the end.
        """
        self.store.calibration = [
            item for item in self.store.calibration if item is not record
        ]
        self._save()

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        #: Button, title key, tooltip key: `retranslate` sets the text.
        self._action_buttons: list[tuple[QPushButton, str, str]] = []
        for title, slot, tip in (
            ("records.add", self._add, "records.add.tip"),
            ("records.edit", self._edit, "records.edit.tip"),
            ("records.delete", self._delete, "records.delete.tip"),
        ):
            button = QPushButton()
            button.clicked.connect(slot)
            row.addWidget(button)
            self._action_buttons.append((button, title, tip))

        self.chart_button = QPushButton()
        self.chart_button.clicked.connect(
            lambda: self.chartRequested.emit(CHART_FORECAST)
        )
        row.addWidget(self.chart_button)

        row.addStretch(1)
        return row

    def _build_table(self) -> QTableWidget:
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.doubleClicked.connect(self._edit)

        setup_table(self.table)
        set_restore_order(self.table, self.refresh)
        self._columns_fitted = False
        return self.table

    def _apply_headers(self) -> None:
        """The titles of byte columns carry the name of the chosen unit."""
        labels = [
            tr(title) + (unit_suffix(self._unit) if kind == COL_BYTES else "")
            for title, kind, _tip in COLUMNS
        ]
        self.table.setHorizontalHeaderLabels(labels)
        set_header_tooltips(self.table, [tr(tip) for _t, _k, tip in COLUMNS])

    def set_expand_tables(self, expand: bool) -> None:
        apply_table_height(self.table, expand)

    def set_unit(self, unit: Unit) -> None:
        self._unit = unit
        self._apply_headers()
        self.refresh()

    def _build_summary(self) -> QLabel:
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setAlignment(Qt.AlignTop)
        self.summary_label.setMinimumHeight(48)
        return self.summary_label

    # --- store -------------------------------------------------------------

    def load_from(self, path: str | Path) -> bool:
        self.close_editors()
        try:
            self.store = Store.load(path)
        except StoreError as exc:
            self.report_error(tr("records.open_failed"), str(exc))
            self.store = Store(path=Path(path))
            self.refresh()
            return False
        if self.store.migrated:
            # The measurements came from the old single-file store. They must
            # be written to their own place right away: while the records file
            # holds a second copy, an edit of a measurement goes to one file
            # and a read comes from the other.
            try:
                self.store.save()
            except StoreError as exc:
                self.report_error(tr("records.save_failed"), str(exc))
        self.refresh()
        self.recordsChanged.emit()
        return True

    def _choose_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("records.file_dialog"),
            str(self.store.path),
            "JSON (*.json)",
            options=QFileDialog.DontConfirmOverwrite,
        )
        if path:
            self.load_from(path)

    def save_store(self) -> None:
        """Save and emit the signal. Needed by edits of calibration points."""
        self._save()

    def _save(self) -> None:
        try:
            self.store.save()
        except StoreError as exc:
            self.report_error(tr("records.save_failed"), str(exc))
            return
        self.refresh()
        self.recordsChanged.emit()

    # --- operations on records ---------------------------------------------

    def _selected_index(self) -> int | None:
        """The store index of the selected record.

        With sorting, the row number in the table and the position in the store
        diverge, so the index is taken from the cell's data, not from the row
        number.
        """
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return None
        index = item.data(STORE_INDEX_ROLE)
        return int(index) if index is not None else None

    def _add(self) -> RecordDialog:
        dialog = self._new_dialog()
        self._creators.append(dialog)
        dialog.finished.connect(
            lambda code, box=dialog: self._creator_closed(box, code)
        )
        return self._show(dialog)

    def _creator_closed(self, dialog: RecordDialog, code: int) -> None:
        if dialog in self._creators:
            self._creators.remove(dialog)
        dialog.deleteLater()
        if code == QDialog.Accepted:
            self.store.add(dialog.result_record())
            self._save()

    def _edit(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        self.open_record(self.store.records[index])

    def _delete(self) -> None:
        index = self._selected_index()
        if index is None:
            return
        record = self.store.records[index]
        if not self.confirm(
            tr("records.delete.title"), tr("records.delete.text", id=record.name)
        ):
            return
        # This record's edit window is closed along with it: there would be
        # nowhere left to save it to, and Save in it would look as if it
        # worked.
        opened = self._editors.pop(id(record), None)
        if opened is not None:
            opened.close()
        self.store.remove_at(index)
        self._save()

    def _show_error(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _ask_confirmation(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    # --- display -----------------------------------------------------------

    def refresh(self) -> None:
        self.path_label.setText(str(self.store.path))
        # Calibration points no longer get here at all: they live in a file
        # of their own, Calibration.json, and on their own tab. The filter is
        # no longer needed.
        shown = list(enumerate(self.store.records))

        # Filling with sorting on would reshuffle the rows as it goes, and
        # cells would drift into other rows.
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(shown))

        for row, (store_index, record) in enumerate(shown):
            deviation = None
            if record.metadata_bytes is not None and record.mounted_bytes:
                deviation = record.metadata_bytes - self._baseline.overhead(
                    record.volume_bytes
                )
            issues = validate(record)
            status = tr(
                "records.status.flagged"
                if record.flagged
                else ("records.status.errors" if issues else "records.status.ok")
            )

            values = (
                record.name,
                record.container_mib,
                record.mounted_bytes,
                record.metadata_bytes,
                deviation,
                record.file_count,
                record.left_bytes,
                record.miss_mib,
                record.model_miss_mib,
                status,
            )
            tooltip = ""
            if issues:
                tooltip = LINE_BREAK.join(issue.message for issue in issues)
            elif record.flagged:
                tooltip = tr("records.row.flagged")

            for column, ((_, kind, _tip), value) in enumerate(zip(COLUMNS, values)):
                if kind == COL_BYTES:
                    text = fmt_table_cell(value, self._unit)
                    key = value
                elif kind == COL_COUNT:
                    text = fmt_bytes(value)
                    key = value
                elif kind == COL_SIGNED:
                    text = DASH if value is None else f"{value:+d}"
                    key = value
                else:
                    text = str(value)
                    key = text.lower()
                item = SortableItem(text, key)
                if column:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if tooltip:
                    item.setToolTip(tooltip)
                # Underestimate in red. Here, not in the tooltip: a calculation
                # that would not have fit must be visible without hovering.
                if kind == COL_SIGNED and value is not None and value < 0:
                    item.setForeground(QColor("#b00020"))
                    item.setToolTip(tooltip or tr("records.row.short"))
                if column == 0:
                    item.setData(STORE_INDEX_ROLE, store_index)
                self.table.setItem(row, column, item)

        self.table.setSortingEnabled(True)
        # Once, on the first fill. After that the width is the user's business,
        # and it must not be overwritten on every refresh.
        if not self._columns_fitted and shown:
            fit_columns(self.table)
            self._columns_fitted = True
        # The summary counts every copy record — the same set the table shows.
        self._refresh_summary(self.store.records)

    def _refresh_summary(self, records: list[Record]) -> None:
        from ..records import metadata_points, slack_samples

        points = metadata_points(records)
        samples = slack_samples(records)
        parts = [tr("records.summary.records", count=len(records))]
        if points:
            parts.append(
                tr(
                    "records.summary.points",
                    count=len(points),
                    low=fmt_bytes(min(v for v, _ in points)),
                    high=fmt_bytes(max(v for v, _ in points)),
                )
            )
        else:
            parts.append(tr("records.summary.no_points", count=len(points)))
        if not samples:
            parts.append(tr("records.summary.no_slack", count=len(samples)))
        else:
            # A single file count goes without «от … до».
            low = min(count for count, _ in samples)
            high = max(count for count, _ in samples)
            if low == high:
                parts.append(
                    tr_n("records.summary.slack_same", low, total=len(samples))
                )
            else:
                parts.append(
                    tr(
                        "records.summary.slack_range",
                        count=len(samples),
                        low=low,
                        high=high,
                    )
                )

        # Checking the prediction: the very reason the program exists. Counted
        # only over records where the prediction is recorded and the left space
        # is measured.
        report = forecast(records)
        if not report.checked:
            parts.append(tr("records.forecast.unchecked"))
        elif report.any_short:
            parts.append(
                tr(
                    "records.forecast.short",
                    checked=report.checked,
                    count=len(report.short),
                    names=", ".join(report.short),
                    worst=f"{report.worst_miss:+d}",
                )
            )
        else:
            parts.append(
                tr(
                    "records.forecast.held",
                    checked=report.checked,
                    worst=f"{report.worst_miss:+d}",
                    model=f"{report.worst_model_miss:+d}",
                )
            )
        self.summary_label.setText(" ".join(parts))
