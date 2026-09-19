"""The Records tab: the store of measurements and editing."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
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
    plural,
    size_label,
    unit_suffix,
)
from ..model import NtfsModel, Payload
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

#: Title, kind and tooltip. The kind determines both the formatting and
#: whether the chosen unit is appended to the title. The tooltip is mandatory:
#: the titles are short out of necessity, otherwise the columns do not fit
#: into the window.
COLUMNS = (
    (
        "Имя",
        COL_TEXT,
        "Как запись назвали. Нужно только чтобы узнать её в проверках на "
        "вкладке «Модель».",
    ),
    (
        "Container init, MiB",
        COL_COUNT,
        "Размер, заданный в VeraCrypt при создании контейнера. Больше ёмкости "
        "тома ровно на заголовок VeraCrypt.",
    ),
    (
        "Ёмкость тома",
        COL_BYTES,
        "Сколько показывает смонтированный том. От него и зависят "
        "метаданные NTFS.",
    ),
    (
        "NTFS",
        COL_BYTES,
        "Ёмкость тома минус свободное место на пустом. Считается на лету, в "
        "файл не пишется.",
    ),
    (
        "Откл. от базовой",
        COL_BYTES,
        "Насколько замер разошёлся с моделью по умолчанию (19 MiB + 0.17 % "
        "от тома). С ней, а не с калиброванной: та проходит через свои же "
        "точки, и отклонение всегда было бы нулём.",
    ),
    (
        "Файлов",
        COL_COUNT,
        "Сколько файлов скопировали. Пусто — замер сделан на пустом томе.",
    ),
    (
        "Остаток",
        COL_BYTES,
        "Свободное место после копирования, оно же Left space в VeraCrypt. "
        "По нему калибруется запас на копирование.",
    ),
    (
        "Промах, MiB",
        COL_SIGNED,
        "Что расчёт обещал минус то, чего хватило бы впритык. Минимум "
        "выводится из замеренного остатка: занятое на томе плюс заголовок "
        "VeraCrypt, округлённое вверх.\n"
        "Плюс — перезаклад, минус — данные не влезли бы. Ради второго случая "
        "колонка и заведена: занижение — единственная опасная сторона "
        "расчёта.\n"
        "Пусто — обещание не записано: заполните «Обещано расчётом» в записи "
        "или переносите расчёт кнопкой «Взять с „Расчёта“».",
    ),
    (
        "Промах модели, MiB",
        COL_SIGNED,
        "Тот же промах за вычетом страховки — насколько ошиблись сами "
        "модели.\n"
        "Без него промах неоднозначен: +5 MiB одинаково выглядят и когда "
        "модель точна при страховке 5 MiB, и когда модель занизила на 3, а "
        "8 MiB страховки это скрыли. Второе — предвестник аварии.",
    ),
    (
        "Статус",
        COL_TEXT,
        "«ок» — запись годится для калибровки.\n"
        "«ошибки» — что-то не сходится, подробности в подсказке на строке.\n"
        "«помечена» — сохранена принудительно и в калибровку не идёт.",
    ),
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
        self._baseline = NtfsModel()
        #: Open edit windows: id(record) → window. The windows are modeless,
        #: and there must be at most one per record: two windows on one row are
        #: a race won by whoever presses Save last, and there is no way to
        #: notice the lost edit.
        self._editors: dict[int, RecordDialog] = {}
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

    # --- construction ------------------------------------------------------

    def _build_path_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        caption = QLabel("Файл записей:")
        caption.setToolTip(
            "Записи о копировании. Замеры пустых томов лежат рядом, в "
            "Calibration.json: они про машину, а не про данные, и на другой "
            "машине уже не годятся.\n"
            "Оба файла в папке data рядом с программой — её носят целиком."
        )
        row.addWidget(caption)
        self.path_label = QLabel()
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setStyleSheet("color: palette(mid);")
        self.path_label.setToolTip(
            "Рядом с файлом лежит одна резервная копия с расширением .bak."
        )
        row.addWidget(self.path_label, 1)

        choose = QPushButton("Выбрать…")
        choose.setToolTip(
            "Открыть другой файл записей или завести новый. Существующий не "
            "перезаписывается, а читается.\n"
            "Замеры возьмутся из Calibration.json в той же папке."
        )
        choose.clicked.connect(self._choose_path)
        row.addWidget(choose)
        return row

    def add_calibration_point(self, container_mib: int) -> RecordDialog:
        """Create a calibration point for the given size.

        The name is filled in, and so is the size — all that is needed from the
        user is the measurement of the mounted empty volume.

        The window is modeless, as for an ordinary record: the measurement is
        taken while looking at the mounted volume and at the Calculation tab,
        and a modal window covers both.
        """
        opened = self._editors.get(container_mib)
        if opened is not None:
            return self._raise(opened)
        seed = Record(
            id=f"Калибровка {size_label(container_mib)}", container_mib=container_mib
        )
        dialog = RecordDialog(seed, parent=self, calibration=True)
        # Keyed by the container size, not id(seed): the point of the window is
        # which size it measures, and a second window for the same size would
        # take a second measurement on top of the first.
        self._editors[container_mib] = dialog
        dialog.finished.connect(
            lambda code, key=container_mib, box=dialog: self._point_closed(key, box, code)
        )
        return self._show(dialog)

    def _point_closed(self, key: int, dialog: RecordDialog, code: int) -> None:
        self._editors.pop(key, None)
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
                "Запись не найдена",
                f"«{record.id}» удалили, пока окно правки было открыто. "
                f"Изменения не сохранены.",
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
        for dialog in [*self._editors.values(), *self._creators]:
            dialog.close()
        self._editors.clear()
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
        for title, slot, tip in (
            (
                "Добавить…",
                self._add,
                "Завести запись о копировании: размер контейнера, замеры "
                "тома и остаток после копирования.",
            ),
            (
                "Изменить…",
                self._edit,
                "Открыть выделенную запись. То же делает двойной щелчок по "
                "строке.",
            ),
            (
                "Удалить",
                self._delete,
                "Убрать выделенную запись. Отменить не выйдет, поэтому "
                "спросим подтверждение.",
            ),
        ):
            button = QPushButton(title)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)

        self.chart_button = QPushButton("График промахов…")
        self.chart_button.setToolTip(
            "Обещанный размер против того, которого хватило бы впритык, с "
            "полосой заложенной страховки.\n"
            "Окно немодальное: его можно оставить рядом и смотреть, как "
            "меняется картинка после нового замера."
        )
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

        self.table.setToolTip(
            "Записи о копировании; замеры пустых томов живут на вкладке "
            "«Калибровка».\n"
            "Щелчок по заголовку сортирует: по возрастанию, по убыванию, без "
            "сортировки. Двойной щелчок по строке открывает запись."
        )
        setup_table(self.table)
        set_restore_order(self.table, self.refresh)
        self._apply_headers()
        self._columns_fitted = False
        return self.table

    def _apply_headers(self) -> None:
        """The titles of byte columns carry the name of the chosen unit."""
        labels = [
            title + (unit_suffix(self._unit) if kind == COL_BYTES else "")
            for title, kind, _tip in COLUMNS
        ]
        self.table.setHorizontalHeaderLabels(labels)
        set_header_tooltips(self.table, [tip for _t, _k, tip in COLUMNS])

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
        self.summary_label.setToolTip(
            "Что дают моделям сами записи. Замеры пустых томов сюда не "
            "входят — их покрытие показано на «Калибровке».\n"
            "Запас на копирование калибруют только записи с размером данных и "
            "остатком, а чтобы отделить по-файловую часть, нужны записи с "
            "разным числом файлов."
        )
        return self.summary_label

    # --- store -------------------------------------------------------------

    def load_from(self, path: str | Path) -> bool:
        self.close_editors()
        try:
            self.store = Store.load(path)
        except StoreError as exc:
            self.report_error("Не удалось открыть", str(exc))
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
                self.report_error("Не удалось сохранить", str(exc))
        self.refresh()
        self.recordsChanged.emit()
        return True

    def _choose_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Файл записей",
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
            self.report_error("Не удалось сохранить", str(exc))
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
            "Удалить запись", f"Удалить «{record.id}»? Действие не отменяется."
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
            if record.ntfs_bytes is not None and record.mounted_bytes:
                deviation = record.ntfs_bytes - self._baseline.overhead(
                    record.mounted_bytes
                )
            issues = validate(record)
            status = "помечена" if record.flagged else ("ошибки" if issues else "ок")

            values = (
                record.id,
                record.container_mib,
                record.mounted_bytes,
                record.ntfs_bytes,
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
                tooltip = "Запись помечена"

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
                    item.setToolTip(
                        tooltip
                        or "Обещанного контейнера не хватило бы для этих данных."
                    )
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
        def _files_range(samples: list[tuple[int, int]]) -> str:
            """Range of file counts; a single value goes without «от … до»."""
            low = min(count for count, _ in samples)
            high = max(count for count, _ in samples)
            if low == high:
                word = plural(low, "файле", "файлах", "файлах")
                return f", все при {low} {word}."
            return f", файлов от {low} до {high}."

        from ..records import ntfs_points, slack_samples

        points = ntfs_points(records)
        samples = slack_samples(records)
        parts = [
            f"Записей: {len(records)}.",
            f"Точек для модели NTFS: {len(points)}"
            + (
                f" (тома {fmt_bytes(min(v for v, _ in points))}"
                f"…{fmt_bytes(max(v for v, _ in points))} B)."
                if points
                else " — модель работает на значениях по умолчанию."
            ),
            f"Замеров запаса на копирование: {len(samples)}"
            + (
                _files_range(samples)
                if samples
                else " — по-файловая часть не подтверждена."
            ),
        ]

        # Checking the prediction: the very reason the program exists. Counted
        # only over records where the prediction is recorded and the left space
        # is measured.
        report = forecast(records)
        if not report.checked:
            parts.append(
                "Прогноз ни на чём не проверен: ни у одной записи не заполнено "
                "«Обещано расчётом»."
            )
        elif report.any_short:
            parts.append(
                f"Прогноз проверен на {report.checked}: занизил на "
                f"{len(report.short)} — {', '.join(report.short)}; худший "
                f"промах {report.worst_miss:+d} MiB."
            )
        else:
            parts.append(
                f"Прогноз проверен на {report.checked}: ни разу не занизил, "
                f"наименьший перезаклад {report.worst_miss:+d} MiB "
                f"(из него {report.worst_model_miss:+d} MiB — промах самих "
                f"моделей, остальное страховка)."
            )
        self.summary_label.setText(" ".join(parts))
