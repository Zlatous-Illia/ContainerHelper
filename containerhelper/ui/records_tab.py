"""Вкладка «Записи»: хранилище замеров и правка."""

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

#: Род столбца определяет и форматирование, и ключ сортировки. Строковое
#: сравнение здесь не годится: «9 000» встало бы после «10 000 000».
COL_TEXT = "text"
COL_COUNT = "count"
COL_BYTES = "bytes"
#: Величина со знаком в MiB. Отдельный род, потому что знак здесь несёт весь
#: смысл: минус означает, что данные не влезли бы, и потерять его в общем
#: форматировании нельзя.
COL_SIGNED = "signed"

#: Заголовок, род и подсказка. Род определяет и форматирование, и то,
#: дописывать ли к заголовку выбранную единицу. Подсказка обязательна:
#: заголовки короткие по необходимости, иначе столбцы не влезают в окно.
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

#: Роль, в которой у первой ячейки строки лежит индекс записи в хранилище.
#: При включённой сортировке номер строки в таблице перестаёт совпадать с ним,
#: и без этой роли правка и удаление попадали бы не в ту запись.
STORE_INDEX_ROLE = Qt.UserRole

#: Перенос строки в подсказке. Вынесен в константу: экранирование внутри
#: шаблонов правки этого файла уже один раз схлопывалось.
LINE_BREAK = chr(10)


class RecordsTab(QWidget):
    recordsChanged = Signal()
    #: Просьба показать окно с графиками — по ключу окна. Открывает
    #: его главное окно: вкладки друг о друге и об окнах не знают.
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
        #: Проводится в диалог записи: размер и число файлов приложение само
        #: не знает, их считает вкладка «Расчёт».
        self._payload_provider = payload_provider
        self._container_provider = container_provider
        #: Сколько страховки было в расчёте. Хранится в записи ради разбора
        #: промаха: без неё занижение, прикрытое страховкой, не отличить от
        #: точного попадания.
        self._safety_provider = safety_provider
        #: Показ ошибок и подтверждений вынесен в подменяемые обработчики:
        #: модальный диалог посреди логики нечем закрыть из теста.
        self.report_error = self._show_error
        self.confirm = self._ask_confirmation
        #: Некалиброванная модель — единственная база, относительно которой
        #: отклонение остаётся осмысленным. Калиброванная проходит точно через
        #: свои же точки, и отклонение в таблице всегда было бы нулём.
        self._baseline = NtfsModel()
        #: Открытые окна правки: id(записи) → окно. Окна немодальны, и на одну
        #: запись их должно быть не больше одного: два окна на одну строку —
        #: это гонка, где выигрывает нажавший «Сохранить» последним, а
        #: потерянную правку заметить нечем.
        self._editors: dict[int, RecordDialog] = {}
        #: Окна новых записей. Их можно открыть сколько угодно: пока запись не
        #: сохранена, мешать друг другу им нечем.
        self._creators: list[RecordDialog] = []

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_path_row())
        layout.addLayout(self._build_buttons())

        # Высота таблицы принадлежит пользователю: тянется за разделитель,
        # ручка которого приходится на нижний контур самой таблицы.
        layout.addWidget(with_grip(self._build_table()))
        layout.addWidget(self._build_summary())
        layout.addStretch(1)

    # --- построение --------------------------------------------------------

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
        """Завести точку калибровки на заданный размер.

        Имя подставляется, размер тоже — от пользователя нужен только замер
        смонтированного пустого тома.

        Окно немодальное, как и у обычной записи: замер снимают, глядя на
        смонтированный том и на вкладку «Расчёт», а модальное окно закрывает
        и то, и другое.
        """
        opened = self._editors.get(container_mib)
        if opened is not None:
            return self._raise(opened)
        seed = Record(
            id=f"Калибровка {size_label(container_mib)}", container_mib=container_mib
        )
        dialog = RecordDialog(seed, parent=self, calibration=True)
        # Ключом — размер контейнера, а не id(seed): смысл окна в том, какой
        # размер оно замеряет, и второе окно на тот же размер сняло бы второй
        # замер поверх первого.
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

    # --- немодальные окна правки -------------------------------------------

    def _show(self, dialog: RecordDialog) -> RecordDialog:
        """Показать окно, не отдавая ему управление.

        `show`, а не `exec`: пока окно открыто, вкладка «Расчёт» остаётся
        живой — а она и есть источник размера и числа файлов, и «Взять
        с „Расчёта“» без неё нажимать бессмысленно.
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
        """Открыть запись на правку. Уже открытую — поднять, а не открыть заново."""
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
        """Окно закрылось. Сохранённое кладётся в ту же самую запись.

        Место ищется тождеством в момент сохранения, а не запоминается при
        открытии: пока окно было открыто, соседнюю запись могли удалить из
        другого окна, и номер показывал бы уже на чужую строку.
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
        """Закрыть все окна правки. Зовётся при смене файла записей.

        Открытое окно держит запись **прежнего** хранилища: сохранить её в
        новое некуда, а показывать её рядом с чужой таблицей — врать о том,
        что правится.
        """
        for dialog in [*self._editors.values(), *self._creators]:
            dialog.close()
        self._editors.clear()
        self._creators.clear()

    def store_calibration_point(self, record: Record) -> None:
        """Положить замер в файл замеров, вытеснив прежний того же рода.

        Замер на тот же размер тома заменяет прежний, а не ложится рядом: две
        точки на одном томе модели не нужны. Замер запаса вытесняет замер с
        тем же числом файлов — своим ключом, потому что общий выбивал бы
        точку NTFS замером запаса, снятым на том же размере тома, и наоборот.

        Сохранение сразу же, по одному замеру: автоматический сбор идёт
        часами, и падение посередине не должно стоить всего, что уже снято.
        """
        self.store.put_calibration(record)
        self._save()

    def remove_calibration(self, record: Record) -> None:
        """Убрать замер из файла замеров. По тождеству, а не по номеру.

        Номер зависит от порядка в файле, а тот меняется при каждом
        сохранении: put_calibration переставляет заменённый замер в конец.
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
        """Заголовки байтовых столбцов несут название выбранной единицы."""
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

    # --- хранилище ---------------------------------------------------------

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
            # Замеры приехали из старого однофайлового хранилища. Записать их
            # на своё место надо сразу: пока файл записей держит вторую копию,
            # правка замера уедет в один файл, а чтение — в другой.
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
        """Сохранить и разослать сигнал. Нужно правкам точек калибровки."""
        self._save()

    def _save(self) -> None:
        try:
            self.store.save()
        except StoreError as exc:
            self.report_error("Не удалось сохранить", str(exc))
            return
        self.refresh()
        self.recordsChanged.emit()

    # --- операции над записями ---------------------------------------------

    def _selected_index(self) -> int | None:
        """Индекс выбранной записи в хранилище.

        При сортировке номер строки в таблице и позиция в store расходятся,
        поэтому индекс берётся из данных ячейки, а не из номера строки.
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
        # Окно правки этой записи закрывается заодно: сохранять его было бы
        # уже некуда, а «Сохранить» в нём выглядело бы работающим.
        opened = self._editors.pop(id(record), None)
        if opened is not None:
            opened.close()
        self.store.remove_at(index)
        self._save()

    def _show_error(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def _ask_confirmation(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    # --- отображение -------------------------------------------------------

    def refresh(self) -> None:
        self.path_label.setText(str(self.store.path))
        # Точки калибровки сюда больше не попадают вовсе: они живут своим
        # ключом в файле и своей вкладкой. Фильтр стал не нужен.
        shown = list(enumerate(self.store.records))

        # Заполнение при включённой сортировке перемешивало бы строки прямо по
        # ходу, и ячейки уезжали бы в чужие строки.
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
                # Занижение красным. Здесь, а не в подсказке: расчёт, который
                # не влез бы, надо видеть, не наводя мышь.
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
        # Один раз при первом наполнении. Дальше ширина — дело пользователя, и
        # затирать её на каждом обновлении нельзя.
        if not self._columns_fitted and shown:
            fit_columns(self.table)
            self._columns_fitted = True
        # Сводка всегда по всему хранилищу: фильтр прячет строки, а не данные.
        self._refresh_summary(self.store.records)

    def _refresh_summary(self, records: list[Record]) -> None:
        def _files_range(samples: list[tuple[int, int]]) -> str:
            """Разброс по числу файлов; при одном значении — без «от … до»."""
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

        # Проверка прогноза: то, ради чего программа и существует. Считается
        # только по записям, где обещание расчёта записано и остаток замерен.
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
