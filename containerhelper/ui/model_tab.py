"""The Model tab: calibration status and its check."""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..formatting import (
    DEFAULT_UNIT,
    Unit,
    fmt_both,
    fmt_bytes,
    fmt_table_cell,
    unit_suffix,
)
from ..i18n import tr, tr_n
from ..model import MIB, CopySlackModel, MetadataModel
from ..records import (
    Check,
    Record,
    metadata_cross_check,
    slack_cross_check,
    worst_shortfall,
)
from .chart_window import CHART_NTFS, CHART_SLACK
from .table import (
    SortableItem,
    apply_table_height,
    fit_columns,
    set_header_tooltips,
    set_restore_order,
    setup_table,
    with_grip,
)

#: Header, the "column in bytes" flag and tooltip, as catalog keys. Byte
#: columns get the chosen unit appended, and they sort by number, not by text.
CHECK_COLUMNS = (
    ("model.col.record", False, "model.col.record.tip"),
    ("model.col.measured", True, "model.col.measured.tip"),
    ("model.col.predicted", True, "model.col.predicted.tip"),
    ("model.col.deviation", True, "model.col.deviation.tip"),
    ("model.col.basis", False, "model.col.basis.tip"),
)

#: Chart buttons under the check tables: label, window key, tooltip.
CHART_BUTTONS = (
    ("model.chart.ntfs", CHART_NTFS, "model.chart.ntfs.tip"),
    ("model.chart.slack", CHART_SLACK, "model.chart.slack.tip"),
)


class ModelTab(QWidget):
    safetyChanged = Signal(int)
    #: A request to show the chart window — by the window's key.
    chartRequested = Signal(str)

    def __init__(
        self,
        models: Callable[[], tuple[MetadataModel, CopySlackModel]],
        records: Callable[[], Sequence[Record]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._models = models
        self._records = records
        self._unit: Unit = DEFAULT_UNIT
        #: When the safety margin is fitted on the Calculation tab, this field
        #: only shows the result — editing it from here makes no sense.
        self._auto = False
        #: The last computed checks. Kept so the safety margin hint can be
        #: updated without rebuilding both tables on every change of the
        #: value.
        self._checks: tuple[list, list] = ([], [])
        self._safety_mib = 0
        #: Check sections as (title key, caption, explanation, table, summary),
        #: and chart buttons with their keys: `retranslate` walks them.
        self._sections: list[tuple[str, QLabel, QLabel, QTableWidget, QLabel]] = []
        self._chart_buttons: list[tuple[QPushButton, str, str]] = []

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_state())
        layout.addWidget(self._build_safety())

        # No splitter between the sections: it divides a fixed height between
        # neighbours, while what needs to change is the height of the table
        # itself — then the tab grows taller than the window and the scroll
        # gets longer. Each table has its own height grip under its bottom
        # outline.
        layout.addWidget(self._build_checks("model.check.ntfs", "ntfs"))
        layout.addWidget(self._build_checks("model.check.slack", "slack"))
        layout.addLayout(self._build_chart_row())
        layout.addStretch(1)

        self.retranslate()

    def _build_chart_row(self) -> QHBoxLayout:
        """Chart buttons under both check tables.

        Next to the numbers they explain: the table says by how much the
        model missed, and the chart says where exactly and what it looks
        like.
        """
        row = QHBoxLayout()
        for title, key, tip in CHART_BUTTONS:
            button = QPushButton()
            button.clicked.connect(
                lambda _checked=False, name=key: self.chartRequested.emit(name)
            )
            self._chart_buttons.append((button, title, tip))
            row.addWidget(button)
        row.addStretch(1)
        return row

    # --- building ----------------------------------------------------------

    def _build_state(self) -> QGroupBox:
        self.state_group = QGroupBox()
        layout = QVBoxLayout(self.state_group)
        self.ntfs_state = QLabel()
        self.ntfs_state.setWordWrap(True)
        self.slack_state = QLabel()
        self.slack_state.setWordWrap(True)
        layout.addWidget(self.ntfs_state)
        layout.addWidget(self.slack_state)
        return self.state_group

    def _build_safety(self) -> QGroupBox:
        """Display only; the safety margin is deliberately read-only here.

        It depends on the volume size and the file count, and only the
        Calculation tab knows those. A second field here duplicated the first
        and, with auto-selection, rolled back on its own — it looked broken
        because it was.
        """
        self.safety_group = QGroupBox()
        column = QVBoxLayout(self.safety_group)
        self.safety_value = QLabel()
        self.safety_value.setWordWrap(True)
        column.addWidget(self.safety_value)
        self.safety_hint = QLabel()
        self.safety_hint.setWordWrap(True)
        self.safety_hint.setStyleSheet("color: palette(mid);")
        column.addWidget(self.safety_hint)
        return self.safety_group

    def _build_checks(self, title: str, kind: str) -> QWidget:
        """A check section: labels on top, the table last.

        Nothing forces this order any more: the height grip comes with the
        table (`with_grip`) and sits under it wherever the table stands.
        `title` is a catalog key.
        """
        caption = QLabel()
        explanation = QLabel()
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: palette(mid);")

        table = QTableWidget(0, len(CHECK_COLUMNS))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        setup_table(table)
        set_restore_order(table, self.refresh)

        summary = QLabel()
        summary.setWordWrap(True)

        section = QWidget()
        column = QVBoxLayout(section)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(caption)
        column.addWidget(explanation)
        column.addWidget(summary)
        column.addWidget(with_grip(table))

        setattr(self, f"{kind}_table", table)
        setattr(self, f"{kind}_summary", summary)
        setattr(self, f"{kind}_fitted", False)
        self._sections.append((title, caption, explanation, table, summary))
        return section

    def retranslate(self) -> None:
        """Set every static text in the current language, then rebuild the
        text made from data through `refresh`.

        Sorting and column widths survive: `refresh` is the same path every
        update takes.
        """
        self.state_group.setTitle(tr("model.state.title"))
        self.state_group.setToolTip(tr("model.state.tip"))
        self.ntfs_state.setToolTip(tr("model.state.ntfs.tip"))
        self.slack_state.setToolTip(tr("model.state.slack.tip"))
        self.safety_group.setTitle(tr("model.margin.title"))
        self.safety_group.setToolTip(tr("model.margin.tip"))
        self.safety_value.setToolTip(tr("model.margin.value.tip"))
        self.safety_hint.setToolTip(tr("model.margin.hint.tip"))
        for title, caption, explanation, table, summary in self._sections:
            caption.setText(f"<b>{tr(title)}</b>")
            explanation.setText(tr("model.check.explanation"))
            table.setToolTip(tr("model.check.table.tip", title=tr(title)))
            summary.setToolTip(tr("model.check.summary.tip"))
            self._apply_headers(table)
        for button, title, tip in self._chart_buttons:
            button.setText(tr(title))
            button.setToolTip(tr(tip))
        self.refresh()

    def changeEvent(self, event) -> None:  # noqa: N802 — Qt's name
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _apply_headers(self, table: QTableWidget) -> None:
        table.setHorizontalHeaderLabels(
            [
                tr(title) + (unit_suffix(self._unit) if is_bytes else "")
                for title, is_bytes, _tip in CHECK_COLUMNS
            ]
        )
        set_header_tooltips(table, [tr(tip) for _t, _b, tip in CHECK_COLUMNS])

    def set_unit(self, unit: Unit) -> None:
        self._unit = unit
        for table in (self.ntfs_table, self.slack_table):
            self._apply_headers(table)
        self.refresh()

    def set_expand_tables(self, expand: bool) -> None:
        for table in (self.ntfs_table, self.slack_table):
            apply_table_height(table, expand)

    # --- refresh -----------------------------------------------------------

    def set_auto_safety(self, enabled: bool) -> None:
        self._auto = enabled
        self._refresh_safety_hint(*self._checks)

    def set_safety_mib(self, value: int) -> None:
        self._safety_mib = value
        self._refresh_safety_hint(*self._checks)

    def refresh(self) -> None:
        ntfs, slack = self._models()
        records = list(self._records())

        self._refresh_state(ntfs, slack)
        ntfs_checks = metadata_cross_check(records)
        slack_checks = slack_cross_check(records)
        self._checks = (ntfs_checks, slack_checks)
        self._fill_checks(self.ntfs_table, self.ntfs_summary, ntfs_checks)
        self._fill_checks(self.slack_table, self.slack_summary, slack_checks)
        self._refresh_safety_hint(ntfs_checks, slack_checks)

    def _refresh_state(self, ntfs: MetadataModel, slack: CopySlackModel) -> None:
        if ntfs.calibrated:
            low, high = ntfs.covered_range
            self.ntfs_state.setText(
                tr_n(
                    "model.state.ntfs.calibrated",
                    len(ntfs.points),
                    low=fmt_bytes(low),
                    high=fmt_bytes(high),
                    low_gib=f"{low / 1024 ** 3:.1f}",
                    high_gib=f"{high / 1024 ** 3:.1f}",
                    rate=f"{ntfs.rate * 100:.2f}",
                )
            )
        else:
            self.ntfs_state.setText(
                tr(
                    "model.state.ntfs.default",
                    n=len(ntfs.points),
                    base=ntfs.base // MIB,
                    rate=f"{ntfs.rate * 100:.2f}",
                )
            )

        counts = sorted(set(slack.file_counts))
        if slack.per_file_calibrated:
            detail = tr("model.state.slack.range", low=counts[0], high=counts[-1])
        elif slack.calibrated:
            detail = tr_n(
                "model.state.slack.one_count", slack.sample_count, files=counts[0]
            )
        else:
            detail = tr("model.state.slack.none")
        self.slack_state.setText(
            tr(
                "model.state.slack",
                base=fmt_bytes(slack.base),
                per_file=fmt_bytes(slack.per_file),
                detail=detail,
            )
        )

    def _fill_checks(
        self, table: QTableWidget, summary: QLabel, checks: Sequence[Check]
    ) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            values = (
                check.record.name,
                check.measured,
                check.predicted,
                check.deviation,
                tr("model.basis.calibrated" if check.calibrated else "model.basis.default"),
            )
            for column, ((_, is_bytes, _tip), value) in enumerate(
                zip(CHECK_COLUMNS, values)
            ):
                if is_bytes:
                    item = SortableItem(fmt_table_cell(value, self._unit), value)
                else:
                    item = SortableItem(str(value), str(value).lower())
                if column:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 3 and check.deviation > 0:
                    item.setForeground(QColor("#b00020"))
                    item.setToolTip(tr("model.check.under.tip"))
                table.setItem(row, column, item)
        table.setSortingEnabled(True)
        if checks and not self._fitted(table):
            fit_columns(table)

        if not checks:
            summary.setText(tr("model.check.empty"))
            return

        shortfall = worst_shortfall(checks)
        if shortfall > 0:
            summary.setText(tr("model.check.shortfall", shortfall=fmt_both(shortfall)))
        else:
            summary.setText(
                tr(
                    "model.check.no_shortfall",
                    overestimate=fmt_both(-min(check.deviation for check in checks)),
                )
            )

    def _fitted(self, table: QTableWidget) -> bool:
        """Width is fitted once; after that it belongs to the user."""
        kind = "ntfs" if table is self.ntfs_table else "slack"
        already = getattr(self, f"{kind}_fitted")
        setattr(self, f"{kind}_fitted", True)
        return already

    def _refresh_safety_hint(
        self, ntfs_checks: Sequence[Check], slack_checks: Sequence[Check]
    ) -> None:
        worst = max(worst_shortfall(ntfs_checks) + worst_shortfall(slack_checks), 0)
        current = self._safety_mib * MIB

        if not ntfs_checks and not slack_checks:
            self.safety_hint.setText(tr("model.margin.no_records"))
            return

        # The largest underestimate over all records is a diagnostic, not a
        # requirement. The leave-one-out check measures the model without one
        # point, that is, one twice as sparse as the real one; its miss does
        # not shrink as measurements are added, and demanding a safety margin
        # from it for all sizes at once means paying everywhere for the place
        # where the grid is sparsest. How much a particular calculation needs
        # is worked out by auto-selection on the Calculation tab.
        diagnostic = tr("model.margin.diagnostic", worst=fmt_both(worst))
        self.safety_value.setText(
            tr(
                "model.margin.auto" if self._auto else "model.margin.manual",
                value=fmt_both(current),
            )
        )
        self.safety_hint.setText(diagnostic)
