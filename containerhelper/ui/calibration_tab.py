"""The Calibration tab: coverage of the size range by measured points.

Separate from the Records tab, because the purpose is different. A calibration
point is a measurement of an empty volume: nothing was put into the container,
no file of the right size is needed, the metadata depends on the size of the
volume, not on its contents. Copy records serve an entirely different model —
the copy slack.
"""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
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
from ..model import MIB, VC_HEADERS_BYTES, volume_of
from ..records import Record, factory_points, forecast, gives_metadata_point
from .chart_window import CHART_NTFS
from .table import (
    fit_columns,
    fit_widget_columns,
    set_header_tooltips,
    setup_table,
    with_grip,
)

#: Container sizes at which it makes sense to have a point. Below 512 MiB a
#: container is useless; above 1 TiB is beyond what the program was written
#: for. No upward extrapolation is left at all: it was the model's weakest
#: spot, because the baseline slope of 0.17 % against the real growth of one
#: byte per 32 KiB of volume overestimated the metadata at a terabyte thirteen
#: times over.
#:
#: 768, 1536, 3072 and 6144 were added later; the measurements taken at them
#: have since been copied into the factory data, so every row now has a
#: factory measurement. They halve the four segments with a twofold step —
#: the only ones where the grid was sparser than the curve changes. They were
#: added after a measurement at 1610 MiB landed 202 672 B above the 1024…2048
#: chord, with the safety floor, not the calculation, covering it. Halving is
#: not enough either: the new 1536 point lay 233 472 B above that same chord,
#: and 1610 still lies 2 944 B above the 1536…2048 one with the bound at zero,
#: so MIN_SAFETY_BYTES stays. Above 8 GiB the segments are already denser,
#: and past 64 GiB the curve is almost flat.
RECOMMENDED_MIB = (
    512, 768, 1024, 1536, 2048, 3072, 4096, 6144, 8192,
    12288, 16384, 20480, 24576, 32768,
    40960, 49152, 65536, 81920, 102400,
    153600, 204800, 262144, 393216, 524288, 786432, 1048576,
)

#: Header key, "column in bytes" and tooltip key. The tooltip is mandatory:
#: the header itself is short, otherwise the columns do not fit in the
#: window. An empty header stays empty.
COLUMNS = (
    ("calibration.col.size", False, "calibration.col.size.tip"),
    ("calibration.col.gib", False, "calibration.col.gib.tip"),
    ("calibration.col.source", False, "calibration.col.source.tip"),
    ("calibration.col.metadata", True, "calibration.col.metadata.tip"),
    ("", False, "calibration.col.actions.tip"),
)

#: Columns of the copy-slack measurement table: header key, "in bytes",
#: tooltip key. The prediction check lives here too — these measurements are
#: the only ones whose prediction was recorded by the program itself, not by
#: a person.
SLACK_COLUMNS = (
    ("calibration.slack_col.fileset", False, "calibration.slack_col.fileset.tip"),
    ("calibration.slack_col.files", False, "calibration.slack_col.files.tip"),
    ("calibration.slack_col.alloc", True, "calibration.slack_col.alloc.tip"),
    ("calibration.slack_col.slack", True, "calibration.slack_col.slack.tip"),
    ("calibration.slack_col.miss", False, "calibration.slack_col.miss.tip"),
    (
        "calibration.slack_col.model_miss",
        False,
        "calibration.slack_col.model_miss.tip",
    ),
    ("", False, "calibration.slack_col.remove.tip"),
)

#: Three row colours for four states: a disabled own measurement takes the
#: factory colour, because the factory value is what the model uses there.
#: The colour only hints; the state is always written out in words: colour
#: alone is a poor thing to rely on.
COLOUR_OWN = QColor("#1b7f3b")
COLOUR_FACTORY = QColor("#8a6d1f")
COLOUR_MISSING = QColor("#9a9a9a")

#: Row states, as keys: translated where shown.
SOURCE_OWN = "calibration.source.own"
SOURCE_FACTORY = "calibration.source.factory"
SOURCE_DISABLED = "calibration.source.disabled"
SOURCE_MISSING = "calibration.source.missing"

#: Line break in a tooltip. A constant, because the escape inside the edit
#: templates for this file has already collapsed once.
LINE_BREAK = chr(10)


class CalibrationTab(QWidget):
    """Shows the coverage and lets the missing points be measured."""

    pointRequested = Signal(int)
    #: Open automatic collection. The window owns both the store and the data
    #: folder, so it is the window that creates the dialog, not the tab.
    collectRequested = Signal()
    #: Disable or restore the own measurement at this volume size. The volume
    #: size must be 64-bit: Qt's int is a four-byte C++ int, and everything
    #: from 4 GiB up overflows in it. The truncated value matched no
    #: measurement, and the button silently did nothing.
    disableRequested = Signal("qint64", bool)
    resetAllRequested = Signal()
    #: A request to show the chart window — by the window's key.
    chartRequested = Signal(str)
    #: Delete a copy-slack measurement. The record itself is passed, not its
    #: index: the index depends on the order in the file, and that changes
    #: with every save.
    slackRemoveRequested = Signal(object)

    def __init__(
        self,
        points: Callable[[], Sequence[Record]],
        slack: Callable[[], Sequence[Record]] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._points = points
        #: Copy-slack measurements live in the same file as the points: both
        #: describe the machine, not the data. They are told apart by a
        #: property, not by a field — a copy-slack measurement has both data
        #: and left space.
        self._slack = slack or (lambda: [])
        self._unit: Unit = DEFAULT_UNIT
        self._fitted = False
        self._slack_fitted = False

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_intro())
        layout.addWidget(self._build_actions())
        self._box = with_grip(self._build_table())
        layout.addWidget(self._box)
        layout.addWidget(self._build_summary())
        layout.addWidget(self._build_slack_section())
        layout.addStretch(1)

        self.retranslate()

    def retranslate(self) -> None:
        self._intro.setTitle(tr("calibration.intro.title"))
        self._intro_text.setText(tr("calibration.intro.text"))
        if self._origin is not None:
            count = len(factory_points())
            self._origin.setText(
                tr_n(
                    "calibration.intro.factory",
                    count,
                    source=tr("factory.source"),
                    note=tr("factory.note"),
                )
            )

        self.collect_button.setText(tr("calibration.collect"))
        self.collect_button.setToolTip(tr("calibration.collect.tip"))
        self.reset_all_button.setText(tr("calibration.reset_all"))
        self.reset_all_button.setToolTip(tr("calibration.reset_all.tip"))
        self.chart_button.setText(tr("calibration.chart"))
        self.chart_button.setToolTip(tr("calibration.chart.tip"))

        self.table.setToolTip(tr("calibration.table.tip"))
        self.summary.setToolTip(tr("calibration.summary.tip"))
        self._slack_caption.setText(tr("calibration.slack.caption"))
        self._slack_explanation.setText(tr("calibration.slack.explanation"))
        self.slack_summary.setToolTip(tr("calibration.slack.summary.tip"))
        self.slack_table.setToolTip(tr("calibration.slack.table.tip"))
        self._apply_headers()
        self._apply_slack_headers()
        # Cells, row buttons and summaries are built from data: the same
        # refresh rebuilds them, and it never refits the widths from scratch.
        self.refresh()

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    # --- building ----------------------------------------------------------

    def _build_intro(self) -> QGroupBox:
        self._intro = QGroupBox()
        layout = QVBoxLayout(self._intro)
        self._intro_text = QLabel()
        self._intro_text.setWordWrap(True)
        self._intro_text.setStyleSheet("color: palette(mid);")
        layout.addWidget(self._intro_text)

        self._origin: QLabel | None = None
        if factory_points():
            self._origin = QLabel()
            self._origin.setWordWrap(True)
            self._origin.setStyleSheet("color: palette(mid);")
            layout.addWidget(self._origin)
        return self._intro

    def _build_actions(self) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)

        self.collect_button = QPushButton()
        self.collect_button.clicked.connect(self.collectRequested.emit)
        layout.addWidget(self.collect_button)

        self.reset_all_button = QPushButton()
        self.reset_all_button.clicked.connect(self.resetAllRequested.emit)
        layout.addWidget(self.reset_all_button)

        self.chart_button = QPushButton()
        self.chart_button.clicked.connect(
            lambda: self.chartRequested.emit(CHART_NTFS)
        )
        layout.addWidget(self.chart_button)

        layout.addStretch(1)
        return row

    def _build_table(self) -> QTableWidget:
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        # The row order is the size order; there is nothing to sort. The
        # buttons in the cells would not survive sorting anyway.
        setup_table(self.table, sortable=False, min_rows=6)
        return self.table

    def _build_summary(self) -> QLabel:
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        return self.summary

    def _build_slack_section(self) -> QWidget:
        """Copy-slack measurements — the second kind of machine measurement.

        Here, not on the Records tab: they live in the same file as the
        calibration points and describe the same machine. Copy records
        describe the data and move together with it.
        """
        section = QWidget()
        column = QVBoxLayout(section)
        column.setContentsMargins(0, 0, 0, 0)

        self._slack_caption = QLabel()
        column.addWidget(self._slack_caption)

        self._slack_explanation = QLabel()
        self._slack_explanation.setWordWrap(True)
        self._slack_explanation.setStyleSheet("color: palette(mid);")
        column.addWidget(self._slack_explanation)

        self.slack_summary = QLabel()
        self.slack_summary.setWordWrap(True)
        column.addWidget(self.slack_summary)

        self.slack_table = QTableWidget(0, len(SLACK_COLUMNS))
        self.slack_table.verticalHeader().setVisible(False)
        self.slack_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.slack_table.setSelectionMode(QTableWidget.NoSelection)
        # The row order is the order of measuring. The buttons in the cells
        # would not survive sorting anyway.
        setup_table(self.slack_table, sortable=False, min_rows=3)
        column.addWidget(with_grip(self.slack_table))
        return section

    def _apply_headers(self) -> None:
        self.table.setHorizontalHeaderLabels(
            [
                (tr(title) if title else "")
                + (unit_suffix(self._unit) if is_bytes else "")
                for title, is_bytes, _tip in COLUMNS
            ]
        )
        set_header_tooltips(self.table, [tr(tip) for _t, _b, tip in COLUMNS])

    def _apply_slack_headers(self) -> None:
        self.slack_table.setHorizontalHeaderLabels(
            [
                (tr(title) if title else "")
                + (unit_suffix(self._unit) if is_bytes else "")
                for title, is_bytes, _tip in SLACK_COLUMNS
            ]
        )
        set_header_tooltips(
            self.slack_table, [tr(tip) for _t, _b, tip in SLACK_COLUMNS]
        )

    # --- refresh -----------------------------------------------------------

    def set_unit(self, unit: Unit) -> None:
        self._unit = unit
        self._apply_headers()
        self._apply_slack_headers()
        self.refresh()

    def set_expand_tables(self, expand: bool) -> None:
        from .table import apply_table_height

        for table in (self.table, self.slack_table):
            apply_table_height(table, expand)

    def refresh(self) -> None:
        own = {
            record.volume_bytes: record
            for record in self._points()
            if gives_metadata_point(record)
        }
        factory = {point.volume_bytes: point for point in factory_points()}

        self.table.setRowCount(len(RECOMMENDED_MIB))
        counts = {"own": 0, "factory": 0, "missing": 0}

        for row, size_mib in enumerate(RECOMMENDED_MIB):
            volume = volume_of(size_mib * MIB)
            record = own.get(volume)
            point = factory.get(volume)

            if record is not None and not record.disabled:
                source, colour, ntfs = SOURCE_OWN, COLOUR_OWN, record.metadata_bytes
                counts["own"] += 1
            elif point is not None:
                source, colour, ntfs = SOURCE_FACTORY, COLOUR_FACTORY, point.metadata_bytes
                counts["factory"] += 1
                if record is not None:
                    source = SOURCE_DISABLED
            else:
                source, colour, ntfs = SOURCE_MISSING, COLOUR_MISSING, None
                counts["missing"] += 1

            cells = (
                fmt_bytes(size_mib),
                f"{size_mib / 1024:g}",
                tr(source),
                fmt_table_cell(ntfs, self._unit),
            )
            tooltip = self._row_tooltip(size_mib, volume, tr(source), record)
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setForeground(colour)
                item.setToolTip(tooltip)
                self.table.setItem(row, column, item)

            self.table.setCellWidget(
                row, 4, self._row_button(size_mib, record, point)
            )

        if not self._fitted:
            fit_columns(self.table)
            self.table.horizontalHeader().setSectionResizeMode(
                4, QHeaderView.Interactive
            )
            self._fitted = True
        # Fitting to contents does not measure the buttons in cells at all,
        # and the column holding them came out narrower than the buttons.
        fit_widget_columns(self.table)
        self._refresh_summary(counts)
        self._refresh_slack()

    def _refresh_slack(self) -> None:
        records = list(self._slack())
        self.slack_table.setRowCount(len(records))
        for row, record in enumerate(records):
            miss, model_miss = record.miss_mib, record.model_miss_mib
            cells = (
                record.name,
                fmt_bytes(record.file_count),
                fmt_table_cell(record.payload_alloc, self._unit),
                fmt_table_cell(record.copy_slack_measured, self._unit),
                self._signed(miss),
                self._signed(model_miss),
            )
            tooltip = self._slack_tooltip(record)
            #: Which column values are worth tinting. An underestimate in
            #: red: that is the one dangerous side of the calculation, and it
            #: has to be seen in the table, not in a tooltip.
            signed = {4: miss, 5: model_miss}
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                item.setToolTip(tooltip)
                value = signed.get(column)
                if value is not None and value < 0:
                    item.setForeground(QColor("#b00020"))
                self.slack_table.setItem(row, column, item)
            self.slack_table.setCellWidget(row, 6, self._slack_button(record))
        fit_widget_columns(self.slack_table)

        if records and not self._slack_fitted:
            fit_columns(self.slack_table)
            self.slack_table.horizontalHeader().setSectionResizeMode(
                6, QHeaderView.Interactive
            )
            self._slack_fitted = True
        self._refresh_slack_summary(records)

    @staticmethod
    def _signed(value: int | None) -> str:
        """Signed miss: an overestimate's plus is as visible as a minus."""
        return DASH if value is None else f"{value:+d}"

    def _slack_tooltip(self, record: Record) -> str:
        lines = [
            tr(
                "calibration.slack.row.tip",
                container=fmt_bytes(record.container_mib),
                mounted=fmt_bytes(record.mounted_bytes),
                cluster=fmt_bytes(record.cluster_bytes),
            )
        ]
        if record.predicted_mib is not None:
            lines.append(
                tr(
                    "calibration.slack.row.predicted",
                    predicted=fmt_bytes(record.predicted_mib),
                    safety=fmt_bytes(record.predicted_safety_mib or 0),
                    minimum=fmt_bytes(record.minimum_mib),
                )
            )
        else:
            lines.append(tr("calibration.slack.row.unpredicted"))
        if record.shown_note:
            lines.append(record.shown_note)
        return LINE_BREAK.join(lines)

    def _slack_button(self, record: Record) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        button = QPushButton(tr("calibration.slack.remove"))
        button.setToolTip(tr("calibration.slack.remove.tip"))
        button.clicked.connect(
            lambda _=False, item=record: self.slackRemoveRequested.emit(item)
        )
        layout.addWidget(button)
        layout.addStretch(1)
        return box

    def _refresh_slack_summary(self, records: Sequence[Record]) -> None:
        if not records:
            self.slack_summary.setText(tr("calibration.slack.summary.none"))
            return
        counts = sorted({record.file_count for record in records if record.file_count})
        if counts:
            parts = [
                tr(
                    "calibration.slack.summary.counts_range",
                    records=len(records),
                    counts=len(counts),
                    low=counts[0],
                    high=counts[-1],
                )
            ]
        else:
            parts = [
                tr(
                    "calibration.slack.summary.counts",
                    records=len(records),
                    counts=len(counts),
                )
            ]
        if len(counts) < 2:
            parts.append(tr("calibration.slack.summary.slope"))

        report = forecast(records)
        if report.checked:
            if report.any_short:
                parts.append(
                    tr(
                        "calibration.slack.summary.short",
                        short=len(report.short),
                        checked=report.checked,
                        names=", ".join(report.short),
                        worst=f"{report.worst_miss:+d}",
                    )
                )
            else:
                parts.append(
                    tr(
                        "calibration.slack.summary.held",
                        checked=report.checked,
                        worst=f"{report.worst_miss:+d}",
                        model=f"{report.worst_model_miss:+d}",
                    )
                )
        self.slack_summary.setText(" ".join(parts))

    def _row_tooltip(self, size_mib: int, volume: int, source: str, record) -> str:
        """What is behind a row: volume size, state and the own record's name.

        The volume size is not shown in the row, yet it is exactly what serves
        as the key: a measurement is tied to the volume, not to Container init.
        """
        lines = [
            tr(
                "calibration.row.tip",
                container=fmt_bytes(size_mib),
                volume=fmt_bytes(volume),
                headers=fmt_bytes(VC_HEADERS_BYTES),
                source=source,
            )
        ]
        if record is not None:
            lines.append(tr("calibration.row.own", id=record.name))
        return LINE_BREAK.join(lines)

    def _row_button(self, size_mib: int, record, point) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        if record is not None:
            take = QPushButton(tr("calibration.row.retake"))
            take.setToolTip(tr("calibration.row.retake.tip", size=size_mib))
        else:
            take = QPushButton(tr("calibration.row.take"))
            take.setToolTip(tr("calibration.row.take.tip", size=size_mib))
        take.clicked.connect(lambda _=False, mib=size_mib: self.pointRequested.emit(mib))
        layout.addWidget(take)

        if record is not None and point is not None:
            volume = volume_of(size_mib * MIB)
            if record.disabled:
                restore = QPushButton(tr("calibration.row.restore"))
                restore.setToolTip(tr("calibration.row.restore.tip"))
                restore.clicked.connect(
                    lambda _=False, v=volume: self.disableRequested.emit(v, False)
                )
                layout.addWidget(restore)
            else:
                reset = QPushButton(tr("calibration.row.reset"))
                reset.setToolTip(tr("calibration.row.reset.tip"))
                reset.clicked.connect(
                    lambda _=False, v=volume: self.disableRequested.emit(v, True)
                )
                layout.addWidget(reset)
        return box

    def _refresh_summary(self, counts: dict[str, int]) -> None:
        self.reset_all_button.setEnabled(counts["own"] > 0)
        self.summary.setText(
            tr(
                "calibration.summary",
                own=counts["own"],
                factory=counts["factory"],
                missing=counts["missing"],
            )
        )
