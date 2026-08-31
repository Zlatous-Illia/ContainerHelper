"""Вкладка «Расчёт»: от размера исходных данных к размеру контейнера."""

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
from .path_picker import ask_paths
from .table import (
    apply_table_height,
    fit_columns,
    fit_field,
    set_header_tooltips,
    setup_table,
    with_grip,
)

CLUSTER_CHOICES = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)

#: Столбцы разбивки по источникам: заголовок, «в байтах», подсказка.
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

#: Подсказки к столбцам разложения. Второй столбец всегда в байтах — это
#: колонка для сверки, — а третий следует выбранной единице.
BREAKDOWN_TIPS = (
    "Слагаемое размера контейнера. Строки идут в том порядке, в каком "
    "складываются, «Итого» — их сумма.",
    "Значение в байтах. Единице отображения не подчиняется: по нему сверяют "
    "с тем, что показывают VeraCrypt и Проводник.",
    "То же в выбранной единице. Для «B» показывает MiB — два одинаковых "
    "столбца ни к чему.",
)

#: Подсказки к строкам разложения — по названию слагаемого. Число в строке
#: без объяснения, откуда оно, проверить нечем.
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

#: Пределы ввода. Байты — до петабайта с разделителями разрядов, кластер — до
#: 65536. Без них в поле влезает число, для которого нет носителя.
MAX_BYTES_CHARS = 24
MAX_CLUSTER_CHARS = 7

#: Самое длинное осмысленное значение для каждого поля — по нему и меряется
#: ширина виджета. Поле кластера во весь экран врёт о том, что туда можно
#: вписать: больше 65536 не бывает.
SAMPLE_BYTES = "1 125 899 906 842 624"
SAMPLE_CLUSTER = "65 536"

#: Перенос строки в подсказке. Константой, потому что экранирование внутри
#: шаблонов правки уже один раз схлопывалось.
LINE_BREAK = chr(10)

#: Подпись под пустой таблицей источников. Одной строкой на два места:
#: она стоит и при сборке, и при каждом обновлении, а разойдясь, назвала бы
#: одно и то же состояние по-разному.
NO_SOURCE_HINT = (
    "Источник не выбран: перетащите сюда файлы и папки, выберите их кнопкой "
    "или введите размер вручную."
)

ModelProvider = Callable[[], tuple[NtfsModel, CopySlackModel]]
SafetyProvider = Callable[[], SafetyModel]


class CalcTab(QWidget):
    safetyChanged = Signal(int)
    autoSafetyChanged = Signal(bool)
    #: Пересчитали. По нему обновляется окно с графиками текущего расчёта:
    #: оно рисует ровно то, что стоит сейчас в таблице разложения.
    calculationChanged = Signal()
    #: Просьба показать окно с графиками. Открывает его главное окно —
    #: вкладки друг о друге и об окнах не знают.
    chartRequested = Signal(str)

    def __init__(
        self,
        models: ModelProvider,
        safety: SafetyProvider | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._models = models
        #: Откуда брать совет по страховке. None — подбор недоступен, поле
        #: остаётся ручным, как было до его появления.
        self._safety = safety
        self._advice: SafetyAdvice | None = None
        #: Итог последнего обхода. None — размер введён руками, и разбивки по
        #: источникам нет вовсе.
        self._scan: ScanResult | None = None
        #: Последнее решение — то же, что показано в таблице разложения.
        #: Хранится, чтобы график рисовал показанное, а не пересчитанное
        #: заново: пересчёт мог бы прийти к другому ответу, если между делом
        #: сменилась модель.
        self._solution: Solution | None = None
        self._unit: Unit = DEFAULT_UNIT
        self._sources_fitted = False
        #: Показывать ли скрытые файлы в диалоге выбора. На расчёт не влияет:
        #: внутри выбранной папки скрытые считаются всегда.
        self._show_hidden = False

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_source_group())
        layout.addWidget(self._build_params_group())
        layout.addWidget(self._build_result_group())

        layout.addWidget(with_grip(self._build_breakdown()))
        layout.addWidget(self._build_notes())
        layout.addStretch(1)

        self.setAcceptDrops(True)
        # Поля ввода принимают перетаскивание сами и вставили бы брошенный
        # путь текстом прямо в размер. Отказ поля пускает событие дальше —
        # во вкладку, где путь и станет источником.
        for field in self.findChildren(QLineEdit):
            field.setAcceptDrops(False)

        self.recalculate()

    # --- построение интерфейса --------------------------------------------

    def _build_source_group(self) -> QGroupBox:
        group = QGroupBox("Исходные данные")
        group.setToolTip(
            "Что кладём в контейнер. Файлы и папки можно выбирать "
            "вперемешку, бросать сюда мышью из Проводника, а можно просто "
            "ввести размер."
        )
        outer = QVBoxLayout(group)
        outer.addLayout(self._build_source_buttons())

        self.source_box = with_grip(self._build_source_table())
        outer.addWidget(self.source_box)

        self.source_label = QLabel(NO_SOURCE_HINT)
        self.source_label.setWordWrap(True)
        self.source_label.setStyleSheet("color: palette(mid);")
        self.source_label.setToolTip(
            "Сводка по выбранному. В расчёт идут два числа: объём по "
            "кластерам и количество файлов."
        )
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
        """Одна кнопка выбора на оба рода источников.

        Разделение на «файл» и «папку» было не выбором пользователя, а
        пересказом того, что родные диалоги Windows устроены двумя разными
        системными вызовами. Набор данных так не делится: в контейнер кладут и
        папки, и отдельные файлы, и обычно вместе.
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

        # Кнопки выделения — те же три, что и в диалоге выбора. Клавиатурой всё
        # это доступно и так, но искать Ctrl+A в таблице, которой обычно не
        # пользуются, незачем, а «инвертировать» горячей клавиши не имеет вовсе.
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

    # --- выделение в таблице источников ------------------------------------

    def source_table_select_all(self) -> None:
        self.source_table.selectAll()

    def source_table_select_none(self) -> None:
        self.source_table.clearSelection()

    def source_table_invert(self) -> None:
        """Перевернуть выделение: выделенные строки снять, остальные выбрать.

        Toggle по всему прямоугольнику таблицы, а не обход строк с selectRow:
        та без зажатого Ctrl сбрасывает предыдущее выделение, и от инверсии
        осталась бы одна последняя строка.
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
        # Порядок строк — порядок выбора; сортировка тут только запутала бы,
        # потому что удалять надо ровно ту строку, на которую смотрят.
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
        # Состояние выставляется до подключения сигнала: toggled на этапе
        # сборки дёрнул бы recalculate, когда половины виджетов ещё нет.
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
        """Принять значение, выставленное на вкладке «Модель»."""
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
        # Порядок строк здесь и есть содержание, поэтому без сортировки.
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
        """Единица третьего столбца разложения.

        Второй столбец всегда в байтах — это колонка для сверки. Поэтому
        выбор «B» в третьем столбце дал бы два одинаковых числа, и он
        отображается на MiB, как было до появления переключателя.
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

    # --- источник данных ---------------------------------------------------

    def _start_directory(self) -> str:
        """Открывать диалог там, где выбирали в прошлый раз."""
        paths = self._scan.paths if self._scan else []
        if not paths:
            return ""
        first = paths[0]
        return first if os.path.isdir(first) else os.path.dirname(first)

    def _ask_paths(self) -> list[str]:
        """Показать диалог выбора и запомнить, чем он закрылся.

        Галочку «показывать скрытые» диалог переживает: сам он живёт один
        показ, а состояние хранится тут и сохраняется окном в настройки — иначе
        её пришлось бы ставить каждый раз заново.
        """
        chosen, self._show_hidden = ask_paths(
            self, self._start_directory(), self._show_hidden
        )
        return chosen

    def _pick_sources(self) -> None:
        chosen = self._ask_paths()
        if chosen:
            self._rescan(chosen)

    def _add_sources(self) -> None:
        chosen = self._ask_paths()
        if chosen:
            self._rescan(self._current_paths() + chosen)

    @property
    def show_hidden(self) -> bool:
        """Показывать ли скрытые в диалоге выбора. Читает и пишет окно."""
        return self._show_hidden

    def set_show_hidden(self, show: bool) -> None:
        self._show_hidden = bool(show)

    def _drop_sources(self) -> None:
        """Убрать выделенные источники и пересчитать по оставшимся."""
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
        """Обойти выбранное и заполнить поля.

        Обход синхронный: даже на сотнях тысяч файлов os.scandir укладывается в
        секунды, а фоновый поток здесь стоил бы больше, чем экономил.
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
        """Убрали всё — обнулить и поля, а не только таблицу.

        Иначе от снятого источника остаётся его размер и число файлов, и
        расчёт продолжает считать по тому, чего в наборе больше нет:
        подпись говорит «источник не выбран», а Container init стоит
        прежний. Отличить это от ручного ввода нечем — там те же поля.
        """
        self._scan = None
        self.size_edit.clear()
        # Единица, а не ноль: меньше одного файла набора не бывает, и
        # нижняя граница поля это уже говорит.
        self.count_spin.blockSignals(True)
        self.count_spin.setValue(self.count_spin.minimum())
        self.count_spin.blockSignals(False)
        self.recalculate()

    # --- перетаскивание ----------------------------------------------------

    @staticmethod
    def _dropped_paths(mime: QMimeData) -> list[str]:
        """Пути из брошенного. Всё, за чем нет файла на диске, отбрасываем.

        Бросают не только из Проводника: ссылка из браузера и вложение из
        почты приходят тем же mime-типом, но пути за ними нет, а источником
        бывает только путь.
        """
        if not mime.hasUrls():
            return []
        paths = [url.toLocalFile() for url in mime.urls()]
        return [path for path in paths if path and os.path.exists(path)]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Принять перетаскивание, если за ним стоят пути на диске."""
        if self._dropped_paths(event.mimeData()):
            event.acceptProposedAction()

    #: Согласие подтверждается и на каждом движении курсора: базовая
    #: реализация dragMoveEvent не делает ничего, и без неё курсор над
    #: вкладкой показывал бы запрет.
    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event: QDropEvent) -> None:
        """Брошенное дополняет набор, а не заменяет его.

        Замена — это кнопка «Выбрать…», и она показывает диалог. Бросок не
        спрашивает ничего, а молча потерять набранное нельзя: лишний
        источник убирается одной кнопкой, потерянный набирается заново.
        """
        paths = self._dropped_paths(event.mimeData())
        if not paths:
            return
        event.acceptProposedAction()
        self._rescan(self._current_paths() + paths)

    def _on_manual_edit(self, *_args) -> None:
        """Ручная правка отвязывает расчёт от просканированных путей."""
        self._scan = None
        self.recalculate()

    # --- разбивка по источникам -------------------------------------------

    def _refresh_drop_button(self) -> None:
        selected = bool(self.source_table.selectionModel().selectedRows())
        self.drop_button.setEnabled(selected)

    def _refresh_sources(self) -> None:
        """Таблица источников и сводка по ним. Прячется, когда выбирать нечего."""
        sources = self._scan.sources if self._scan else []
        self.source_box.setVisible(bool(sources))
        self.drop_button.setVisible(bool(sources))
        # Кнопки выделения прячутся вместе с таблицей: выделять в спрятанной
        # нечего, а строка кнопок при ручном вводе была бы пустым шумом.
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

    # --- отдача наружу -----------------------------------------------------

    def current_payload(self) -> Payload | None:
        """Что сейчас посчитано на вкладке — для переноса в запись.

        Отдаётся целиком, вместе с alloc_bytes: для папки из многих файлов
        только он и описывает реальный занимаемый объём.
        """
        payload = self._payload()
        if payload is None or payload.logical_bytes <= 0:
            return None
        return payload

    def current_container_mib(self) -> int | None:
        """Посчитанный Container init — чтобы не вбивать его в запись руками."""
        value = parse_bytes(self.result_label.text())
        return value or None

    def current_safety_mib(self) -> int:
        """Сколько страховки заложено в этот расчёт.

        Уходит в запись вместе с обещанием: без неё промах не разложить на
        погрешность моделей и намеренный запас, а разложить надо — занижение,
        прикрытое страховкой, выглядит здоровым ровно до того дня, когда
        страховки не хватит.
        """
        return self.safety_spin.value()

    def current_sources(self) -> list[str]:
        """Пути, с которых снят payload. Пусто — размер введён вручную.

        Список, а не один путь: источников теперь бывает несколько, и
        возвращать из них первый значило бы врать о втором.
        """
        return self._current_paths()

    def current_stats(self):
        """Статистика по выбранному — None при ручном вводе."""
        return self._scan.stats(self._cluster()) if self._scan else None

    def current_solution(self) -> Solution | None:
        """Показанное сейчас разложение — для графика-полосы."""
        return self._solution

    def current_file_sizes(self) -> list[str]:
        """Размеры выбранных файлов. Пусто — размер введён руками.

        График кластерного хвоста единственный считается по настоящим файлам,
        а не по модели, и без их размеров ему нечего показывать.
        """
        return list(self._scan.sizes) if self._scan else []

    def current_cluster(self) -> int:
        return self._cluster()

    # --- расчёт ------------------------------------------------------------

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
        """Подобрать страховку под этот расчёт и выставить её в поле.

        Совет зависит от размера тома, а том — от страховки, поэтому сначала
        решаем с тем, что стоит сейчас, а потом уточняем. Второй проход не
        нужен: страховка меняет том на единицы мегабайт, а совет считается по
        отрезку между замерами шириной в гигабайты.
        """
        if self._safety is None or not self.auto_safety.isChecked():
            return
        probe = solve_container_mib(
            payload, ntfs=ntfs, slack=slack,
            safety_bytes=self.safety_spin.value() * MIB,
        )
        self._advice = self._safety().advise(probe.volume_bytes, payload.file_count)
        # Напрямую, без set_safety_mib: тот перезапускает recalculate, а нас
        # уже вызвали изнутри него.
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
        """Объяснить, откуда взялось число, а не просто показать его."""
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
        """В буфер уходит голое число MiB — его вводят в VeraCrypt как есть."""
        value = parse_bytes(self.result_label.text())
        if value is not None:
            QGuiApplication.clipboard().setText(str(value))
