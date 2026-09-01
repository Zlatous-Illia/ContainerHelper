"""Главное окно."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import charts
from ..fileset import FILE_SETS
from ..formatting import DEFAULT_UNIT, UNITS, unit_by_key
from ..model import CopySlackModel, NtfsModel, SafetyModel
from ..paths import (
    DataDirError,
    records_path,
    resolve_data_dir,
    settings_path,
)
from ..paths import remember_data_dir
from .calc_tab import CLUSTER_CHOICES, CalcTab
from .calibration_tab import RECOMMENDED_MIB, CalibrationTab
from .chart_window import (
    CHART_CALC,
    CHART_FORECAST,
    CHART_NTFS,
    CHART_SLACK,
    ChartWindow,
)
from .collect_dialog import CollectDialog
from .path_picker import PickerState
from .model_tab import ModelTab
from .records_tab import RecordsTab
from .table import (
    column_widths,
    scrollable,
    set_column_widths,
    set_table_height,
    sort_state,
    restore_sort,
    table_height,
)

UNIT_KEY = "display_unit"
EXPAND_KEY = "expand_tables"
GEOMETRY_KEY = "window_geometry"
STATE_KEY = "window_state"
ACTIVE_TAB_KEY = "active_tab"
REMEMBER_TAB_KEY = "remember_tab"
SHOW_HIDDEN_KEY = "show_hidden_files"
#: Настройки диалога выбора файлов. Своей группой: их четыре, и растащить их
#: по General значило бы забыть половину при следующей правке. Ключ скрытых
#: файлов остался прежним — его писали ещё до появления группы, и переезд
#: молча сбросил бы галочку у тех, у кого она стоит.
PICKER_REMEMBER_KEY = "picker/remember_dir"
PICKER_DIR_KEY = "picker/directory"
PICKER_WIDTH_KEY = "picker/width"
PICKER_HEIGHT_KEY = "picker/height"
PICKER_LAYOUT_KEY = "picker/layout"

#: Названия вкладок. Вынесены в константы, потому что имя активной вкладки
#: уходит в настройки: раньше туда уходил номер, и одной перестановки вкладок
#: хватило, чтобы программа стала открываться не на той странице.
TAB_CALC = "Расчёт"
TAB_RECORDS = "Записи"
TAB_MODEL = "Модель"
TAB_CALIBRATION = "Калибровка"

#: Куда открывать программу, когда запоминание вкладки выключено. Расчёт —
#: то, ради чего её открывают в девяти случаях из десяти.
DEFAULT_TAB = TAB_CALC

#: Уже этого окно перестаёт быть пригодным: подписи форм режутся, а таблицы
#: схлопываются в кашу. Дальше включается горизонтальная прокрутка вкладки.
MIN_WINDOW_WIDTH = 720
MIN_TAB_WIDTH = 680

#: Что делает каждая вкладка. Названия из одного слова не говорят ни о
#: порядке работы, ни о том, чем «Калибровка» отличается от «Записей».
TAB_TIPS = {
    "calc": (
        "Выбрать файлы и папки — или ввести размер руками — и получить "
        "Container init для VeraCrypt.\n"
        "Тут же видно, из чего это число сложилось."
    ),
    "records": (
        "Записи о реальных копированиях: какой контейнер, что в него легло, "
        "сколько осталось. По ним калибруется запас на копирование.\n"
        "Замеры пустых томов не здесь, а на «Калибровке»."
    ),
    "calibration": (
        "Покрытие размеров замерами пустых томов. Данные для такого замера "
        "не нужны — метаданные зависят только от размера тома.\n"
        "Сюда идут, когда расчёт помечен как экстраполяция."
    ),
    "model": (
        "На чём стоят обе модели и насколько они промахиваются на своих же "
        "замерах: каждый предсказан моделью, собранной без него.\n"
        "Править тут нечего, это диагностика."
    ),
}


class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("ContainerHelper — размер контейнеров VeraCrypt")
        self.resize(880, 900)
        self.setMinimumWidth(MIN_WINDOW_WIDTH)

        self.data_dir = data_dir or self._ask_data_dir()
        # Настройки лежат рядом с данными, а не в реестре: портативную папку
        # носят целиком, и след вне неё оставлять нечего.
        self.settings = QSettings(str(settings_path(self.data_dir)), QSettings.IniFormat)

        self._ntfs = NtfsModel()
        self._slack = CopySlackModel()
        self._safety = SafetyModel()
        #: Подтверждения — подменяемым обработчиком, как на вкладках: закрыть
        #: модальное окно посреди логики из теста нечем.
        self.confirm = self._ask_confirmation

        self.calc_tab = CalcTab(self.models, self.safety)
        self.records_tab = RecordsTab(
            payload_provider=self.calc_tab.current_payload,
            container_provider=self.calc_tab.current_container_mib,
            safety_provider=self.calc_tab.current_safety_mib,
        )
        # Проверка идёт по всему, на чём стоит модель: записи о копировании,
        # свои замеры и заводские точки. Иначе отчёт молчал бы как раз о тех
        # двадцати двух точках, которые держат кривую NTFS.
        self.model_tab = ModelTab(
            self.models, lambda: self.records_tab.store.all_for_model()
        )
        # Оба рода машинных замеров живут в одном файле и различаются
        # признаком: у замера запаса есть и данные, и остаток. Вкладке они
        # отдаются порознь — таблицы у них разные.
        self.calibration_tab = CalibrationTab(
            lambda: self.records_tab.store.calibration_points(),
            lambda: self.records_tab.store.slack_measurements(),
        )

        #: Открытые окна графиков по ключу. Окно создаётся при первом
        #: показе и живёт до закрытия программы: пересобирать его на
        #: каждый показ значило бы терять и масштаб, и размер окна.
        self._charts: dict[str, ChartWindow] = {}

        self.records_tab.recordsChanged.connect(self._on_records_changed)
        self.calc_tab.calculationChanged.connect(self._refresh_calc_chart)
        for tab in (
            self.calc_tab,
            self.records_tab,
            self.model_tab,
            self.calibration_tab,
        ):
            tab.chartRequested.connect(self.open_chart)
        self.calc_tab.safetyChanged.connect(self.model_tab.set_safety_mib)
        self.calc_tab.autoSafetyChanged.connect(self.model_tab.set_auto_safety)
        self.calibration_tab.pointRequested.connect(self._take_point)
        self.calibration_tab.collectRequested.connect(self._collect_points)
        self.calibration_tab.disableRequested.connect(self._set_point_disabled)
        self.calibration_tab.resetAllRequested.connect(self._disable_all_points)
        self.calibration_tab.slackRemoveRequested.connect(self._remove_slack)

        self.tabs = QTabWidget()
        for tab, title, tip in (
            (self.calc_tab, TAB_CALC, TAB_TIPS["calc"]),
            (self.records_tab, TAB_RECORDS, TAB_TIPS["records"]),
            (self.model_tab, TAB_MODEL, TAB_TIPS["model"]),
            (self.calibration_tab, TAB_CALIBRATION, TAB_TIPS["calibration"]),
        ):
            index = self.tabs.addTab(scrollable(tab, MIN_TAB_WIDTH), title)
            self.tabs.setTabToolTip(index, tip)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(self._build_view_row())
        outer.addWidget(self.tabs)
        self.setCentralWidget(central)

        self.records_tab.load_from(records_path(self.data_dir))
        self.model_tab.set_safety_mib(self.calc_tab.safety_spin.value())
        self.model_tab.set_auto_safety(self.calc_tab.auto_safety.isChecked())
        self._restore_preferences()

    # --- каталог данных ----------------------------------------------------

    def _ask_data_dir(self) -> Path:
        """Найти папку данных, а если рядом с программой не пишется — спросить.

        Тихо уезжать в чужой каталог нельзя: тогда портативная папка перестаёт
        быть портативной молча, и на другой машине данные не поедут вместе с
        ней.
        """
        try:
            found, _origin = resolve_data_dir()
        except DataDirError as exc:
            found, _origin = None, str(exc)
        if found is not None:
            return found

        QMessageBox.information(
            self,
            "Где хранить данные",
            "Папка рядом с программой недоступна на запись. Выберите каталог "
            "для записей и настроек — он запомнится для этой копии программы.",
        )
        chosen = QFileDialog.getExistingDirectory(self, "Каталог данных")
        target = Path(chosen) if chosen else Path.home() / "ContainerHelper"
        target.mkdir(parents=True, exist_ok=True)
        remember_data_dir(target)
        return target

    # --- модели ------------------------------------------------------------

    def models(self) -> tuple[NtfsModel, CopySlackModel]:
        return self._ntfs, self._slack

    def safety(self) -> SafetyModel:
        return self._safety

    def _on_records_changed(self) -> None:
        store = self.records_tab.store
        self._ntfs, self._slack = store.models()
        self._safety = store.safety()
        self.calc_tab.recalculate()
        self.model_tab.refresh()
        self.calibration_tab.refresh()
        self._refresh_charts()
        if self.expand_tables.isChecked():
            self._on_expand_toggled(True)

    # --- окна графиков -----------------------------------------------------

    def _chart_specs(self) -> dict:
        """Что за окна бывают: заголовок, из чего собрать и делят ли ось X.

        Собирается заново на каждый показ, потому что замыкания должны брать
        сегодняшнее хранилище: `records_tab.store` переоткрывается при смене
        папки данных, и захваченное однажды указывало бы на прежнюю.
        """
        store = lambda: self.records_tab.store
        return {
            CHART_NTFS: (
                "Метаданные NTFS",
                (
                    # Список рекомендуемых размеров — чтобы кривая сказала
                    # подписью, какие из них не покрыты ни одним замером.
                    lambda: charts.ntfs_curve(store(), RECOMMENDED_MIB),
                    # Доля тома — та же кривая, но в единицах, которыми
                    # метаданные меряют на глаз.
                    lambda: charts.ntfs_share(store()),
                    lambda: charts.ntfs_residuals(store()),
                    lambda: charts.ntfs_slopes(store()),
                ),
                True,
            ),
            CHART_SLACK: (
                "Запас на копирование",
                (
                    lambda: charts.slack_curve(store()),
                    lambda: charts.slack_residuals(store()),
                ),
                True,
            ),
            CHART_FORECAST: (
                "Промах прогноза",
                (lambda: charts.forecast_misses(store()),),
                False,
            ),
            CHART_CALC: (
                "Текущий расчёт",
                (
                    self._breakdown_chart,
                    self._cluster_chart,
                ),
                False,
            ),
        }

    def _breakdown_chart(self):
        solution = self.calc_tab.current_solution()
        if solution is None:
            return charts.empty_chart(
                "Из чего сложен контейнер",
                "Расчёт пуст: выберите источники или введите размер.",
            )
        return charts.container_breakdown(solution)

    def _cluster_chart(self):
        sizes = self.calc_tab.current_file_sizes()
        if not sizes:
            return charts.empty_chart(
                "Занятое место от размера кластера",
                "Считается по настоящим размерам файлов — выберите источники.",
            )
        return charts.cluster_tail(sizes, CLUSTER_CHOICES, self.calc_tab.current_cluster())

    def open_chart(self, key: str) -> None:
        """Показать окно графика; уже открытое — поднять, а не создать второе."""
        specs = self._chart_specs()
        if key not in specs:
            return
        window = self._charts.get(key)
        if window is None:
            title, builders, link_x = specs[key]
            window = ChartWindow(key, title, builders, link_x, self)
            window.set_unit(unit_by_key(self.unit_combo.currentData()))
            # Вместе с геометрией возвращаются и отсоединённые графики: окно,
            # разложенное по экрану, собирают один раз, а не каждый запуск.
            window.restore_layout(self.settings)
            self._charts[key] = window
        else:
            window.refresh()
        window.show()
        window.raise_()
        window.activateWindow()

    def _refresh_charts(self) -> None:
        for window in self._charts.values():
            window.refresh()

    def _refresh_calc_chart(self) -> None:
        """Окно текущего расчёта следует за расчётом, а не за записями.

        Отдельно от остальных: расчёт меняется от каждого нажатия в поле
        размера, а перерисовывать из-за этого кривую NTFS незачем.
        """
        window = self._charts.get(CHART_CALC)
        if window is not None and window.isVisible():
            window.refresh()

    # --- точки калибровки --------------------------------------------------

    def _take_point(self, container_mib: int) -> None:
        self.records_tab.add_calibration_point(container_mib)

    def covered_sizes(self) -> list[int]:
        """Размеры, на которых свой замер пустого тома уже есть и работает."""
        return [
            record.container_mib
            for record in self.records_tab.store.calibration_points()
            if not record.disabled and record.mounted_bytes
        ]

    def covered_filesets(self) -> list[str]:
        """Наборы, на которых **свой** замер запаса уже есть.

        Ключами набора, а не числами файлов: два набора с `n = 1` заведены
        разными по объёму нарочно, и снять надо оба — на их сверке держится
        проверка того, что запас от размера файлов не зависит.

        Только свои, как и с размерами: «недостающее» — это то, чего нет на
        этой машине. Заводской замер закрывает набор лишь до тех пор, пока
        своего нет, и предлагать снять его — правильно.
        """
        store = self.records_tab.store
        return sorted(
            {
                record.fileset
                for record in [*store.records, *store.calibration]
                if record.fileset
                and not record.disabled
                and record.copy_slack_measured is not None
            }
        )

    def build_collect_dialog(self) -> CollectDialog:
        """Собрать диалог автоматического сбора и подключить его к хранилищу.

        Диалог заводит окно, а не вкладка: сбору нужны и хранилище, куда
        складывать замеры, и папка данных — её приходится передавать себе же
        при перезапуске от администратора, иначе портативность кончится.
        """
        dialog = CollectDialog(
            sizes=RECOMMENDED_MIB,
            covered=self.covered_sizes(),
            settings=self.settings,
            data_dir=self.data_dir,
            parent=self,
            filesets=FILE_SETS,
            slack_covered=self.covered_filesets(),
            models=self.models,
            safety=self.safety,
            forbidden_sizes=RECOMMENDED_MIB,
        )
        dialog.pointMeasured.connect(self.records_tab.store_calibration_point)
        dialog.relaunchRequested.connect(self.close)
        return dialog

    def _collect_points(self) -> None:
        self.build_collect_dialog().exec()

    def _remove_slack(self, record) -> None:
        """Убрать замер запаса. Насовсем: отключать его незачем.

        У точки калибровки отключение осмысленно — под ней лежит заводское
        значение, к которому можно вернуться. У замера запаса заводского на
        то же число файлов может не быть вовсе, и «отключено» означало бы
        просто спрятанные числа.
        """
        if not self.confirm(
            "Удалить замер",
            f"Удалить «{record.id}»? Числа исчезнут из файла, "
            f"действие не отменяется.",
        ):
            return
        self.records_tab.remove_calibration(record)

    def _set_point_disabled(self, volume_bytes: int, disabled: bool) -> None:
        """Отключить или вернуть точку калибровки на этом томе.

        Только точку: замеры запаса лежат в том же списке, и общий проход по
        mounted_bytes задел бы и их. Совпасть размеры не должны — контейнер
        под набор намеренно уводится с рекомендованных, — но полагаться на
        это, когда достаточно проверить признак, незачем.
        """
        store = self.records_tab.store
        for index, record in enumerate(store.calibration):
            if record.is_calibration_point and record.mounted_bytes == volume_bytes:
                store.calibration[index] = replace(record, disabled=disabled)
        self.records_tab.save_store()

    def _disable_all_points(self) -> None:
        """Вернуться к заводским по всем точкам. Замеров запаса не касается.

        У них заводского на то же число файлов может не быть вовсе, и
        «отключить» означало бы потерять калибровку запаса целиком, ничего об
        этом не сказав.
        """
        store = self.records_tab.store
        live = [
            record
            for record in store.calibration
            if record.is_calibration_point and not record.disabled
        ]
        if not live:
            return
        if not self.confirm(
            "Вернуться к заводским",
            f"Отключить свои замеры пустых томов ({len(live)} шт.) и считать "
            f"по заводским?\n"
            f"Числа остаются в файле — каждый можно включить обратно. Замеры "
            f"запаса на копирование это не затронет.",
        ):
            return
        store.calibration = [
            replace(record, disabled=True) if record.is_calibration_point else record
            for record in store.calibration
        ]
        self.records_tab.save_store()

    # --- вид ---------------------------------------------------------------

    def _build_view_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(8, 6, 8, 0)

        self.expand_tables = QCheckBox("Таблицы во всю высоту")
        self.expand_tables.setToolTip(
            "Растянуть таблицы на все строки. Вкладка станет выше окна, "
            "появится прокрутка. Высоту каждой таблицы можно тянуть и "
            "отдельно — за полоску под ней."
        )
        self.expand_tables.toggled.connect(self._on_expand_toggled)
        row.addWidget(self.expand_tables)

        self.remember_tab = QCheckBox("Запоминать вкладку")
        self.remember_tab.setToolTip(
            f"Открывать программу на той вкладке, где её закрыли.\n"
            f"Выключено — программа всегда открывается на «{DEFAULT_TAB}»."
        )
        self.remember_tab.toggled.connect(self._on_remember_tab_toggled)
        row.addWidget(self.remember_tab)

        row.addStretch(1)
        row.addWidget(QLabel("Единицы:"))
        self.unit_combo = QComboBox()
        for unit in UNITS:
            self.unit_combo.addItem(unit.label, unit.key)
        self.unit_combo.setToolTip(
            "В чём показывать байтовые столбцы. На ввод, хранение и на сам "
            "Container init не влияет."
        )
        self.unit_combo.currentIndexChanged.connect(self._on_unit_changed)
        row.addWidget(self.unit_combo)
        return row

    def view_tabs(self):
        return (self.calc_tab, self.records_tab, self.calibration_tab, self.model_tab)

    def all_tables(self) -> dict[str, object]:
        return {
            "sources": self.calc_tab.source_table,
            "calc": self.calc_tab.table,
            "records": self.records_tab.table,
            "calibration": self.calibration_tab.table,
            "slack_measurements": self.calibration_tab.slack_table,
            "ntfs": self.model_tab.ntfs_table,
            "slack": self.model_tab.slack_table,
        }

    def _on_expand_toggled(self, expand: bool) -> None:
        for tab in self.view_tabs():
            tab.set_expand_tables(expand)
        self.settings.setValue(EXPAND_KEY, expand)

    def _on_unit_changed(self) -> None:
        unit = unit_by_key(self.unit_combo.currentData())
        for tab in self.view_tabs():
            tab.set_unit(unit)
        for window in self._charts.values():
            window.set_unit(unit)
        self.settings.setValue(UNIT_KEY, unit.key)

    def _on_remember_tab_toggled(self, remember: bool) -> None:
        self.settings.setValue(REMEMBER_TAB_KEY, remember)

    def _select_tab(self, title: str) -> None:
        """Открыть вкладку по имени; неизвестное имя — первая вкладка.

        По имени, а не по номеру. Номер меняется от любой перестановки вкладок,
        и программа тогда молча открывается не на той странице — а заметить это
        нечем, потому что открылась-то она успешно. Заодно так переживается и
        старая настройка с номером: «2» не совпадёт ни с одним заголовком и
        честно уедет на первую вкладку.
        """
        titles = [self.tabs.tabText(index) for index in range(self.tabs.count())]
        self.tabs.setCurrentIndex(titles.index(title) if title in titles else 0)

    def current_tab_title(self) -> str:
        return self.tabs.tabText(self.tabs.currentIndex())

    # --- преференции -------------------------------------------------------

    def _restore_preferences(self) -> None:
        stored = self.settings.value(UNIT_KEY, DEFAULT_UNIT.key, type=str)
        index = self.unit_combo.findData(unit_by_key(stored).key)
        self.unit_combo.setCurrentIndex(max(index, 0))
        self._on_unit_changed()

        expand = self.settings.value(EXPAND_KEY, False, type=bool)
        self.expand_tables.setChecked(expand)
        self._on_expand_toggled(expand)

        geometry = self.settings.value(GEOMETRY_KEY)
        if geometry:
            self.restoreGeometry(geometry)

        self._restore_picker()

        remember = self.settings.value(REMEMBER_TAB_KEY, True, type=bool)
        self.remember_tab.setChecked(remember)
        stored = self.settings.value(ACTIVE_TAB_KEY, DEFAULT_TAB, type=str)
        self._select_tab(stored if remember else DEFAULT_TAB)

        for name, table in self.all_tables().items():
            widths = self.settings.value(f"{name}/columns", [], type=list)
            set_column_widths(table, [int(value) for value in widths] if widths else [])
            height = self.settings.value(f"{name}/height", 0, type=int)
            if height:
                set_table_height(table, height)
            section = self.settings.value(f"{name}/sort_section", -1, type=int)
            order = self.settings.value(f"{name}/sort_order", 0, type=int)
            if section >= 0:
                restore_sort(table, section, order)

    def _restore_picker(self) -> None:
        """Вернуть диалогу выбора его вид, размер и обе галочки.

        Всё это ставят в самом диалоге, а хранит окно: диалог живёт один
        показ, и настройка, оставшаяся в нём, не пережила бы даже «Отмену».
        """
        self.calc_tab.set_picker_state(
            PickerState(
                show_hidden=self.settings.value(SHOW_HIDDEN_KEY, False, type=bool),
                remember_dir=self.settings.value(PICKER_REMEMBER_KEY, True, type=bool),
                directory=self.settings.value(PICKER_DIR_KEY, "", type=str),
                width=self.settings.value(PICKER_WIDTH_KEY, 0, type=int),
                height=self.settings.value(PICKER_HEIGHT_KEY, 0, type=int),
                layout=QByteArray(self.settings.value(PICKER_LAYOUT_KEY, QByteArray())),
            )
        )

    def _store_picker(self) -> None:
        state = self.calc_tab.picker_state
        self.settings.setValue(SHOW_HIDDEN_KEY, state.show_hidden)
        self.settings.setValue(PICKER_REMEMBER_KEY, state.remember_dir)
        self.settings.setValue(PICKER_DIR_KEY, state.directory)
        self.settings.setValue(PICKER_WIDTH_KEY, state.width)
        self.settings.setValue(PICKER_HEIGHT_KEY, state.height)
        self.settings.setValue(PICKER_LAYOUT_KEY, state.layout)

    def _store_preferences(self) -> None:
        self.settings.setValue(GEOMETRY_KEY, self.saveGeometry())
        # При выключенном запоминании имя не перезаписывается: программа в
        # такой сессии всё равно открылась на «Расчёте», и записать его значило
        # бы затереть запомненное, ничего не спросив.
        if self.remember_tab.isChecked():
            self.settings.setValue(ACTIVE_TAB_KEY, self.current_tab_title())
        self.settings.setValue(REMEMBER_TAB_KEY, self.remember_tab.isChecked())
        self._store_picker()
        # Расположение окон графиков — своим именем, а не номером: то же
        # правило, что и для активной вкладки.
        for window in self._charts.values():
            window.save_layout(self.settings)
        for name, table in self.all_tables().items():
            self.settings.setValue(f"{name}/columns", column_widths(table))
            self.settings.setValue(f"{name}/height", table_height(table))
            section, order = sort_state(table)
            self.settings.setValue(f"{name}/sort_section", section)
            self.settings.setValue(f"{name}/sort_order", order)
        self.settings.sync()

    def _ask_confirmation(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def closeEvent(self, event) -> None:
        self._store_preferences()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
