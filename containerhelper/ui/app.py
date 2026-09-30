"""The main window."""

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
from ..model import CopySlackModel, MetadataModel, SafetyModel
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
#: Settings of the file and folder picker. In a group of their own: there are
#: five of them, and scattering them over General would mean forgetting half
#: of them at the next edit. The sixth, the hidden-files key, stayed as it
#: was — it was written before the group existed, and moving it would
#: silently clear the check box for those who have it ticked.
PICKER_REMEMBER_KEY = "picker/remember_dir"
PICKER_DIR_KEY = "picker/directory"
PICKER_WIDTH_KEY = "picker/width"
PICKER_HEIGHT_KEY = "picker/height"
PICKER_LAYOUT_KEY = "picker/layout"

#: Tab titles. Kept in constants because the active tab's name goes into the
#: settings: the number used to go there, and one reordering of the tabs was
#: enough for the program to start opening on the wrong page.
TAB_CALC = "Расчёт"
TAB_RECORDS = "Записи"
TAB_MODEL = "Модель"
TAB_CALIBRATION = "Калибровка"

#: Where to open the program when remembering the tab is off. Calculation is
#: what it is opened for nine times out of ten.
DEFAULT_TAB = TAB_CALC

#: Narrower than this, the window stops being usable: form labels get cut and
#: tables collapse into a mess. Beyond that, the tab's horizontal scrolling
#: kicks in.
MIN_WINDOW_WIDTH = 720
MIN_TAB_WIDTH = 680

#: What each tab does. One-word titles say nothing about the order of work,
#: nor about how Calibration differs from Records.
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
        # The settings live next to the data, not in the registry: the portable
        # data folder is carried around whole, and there is no reason to leave
        # a trace outside it.
        self.settings = QSettings(str(settings_path(self.data_dir)), QSettings.IniFormat)

        self._ntfs = MetadataModel()
        self._slack = CopySlackModel()
        self._safety = SafetyModel()
        #: Confirmations go through a replaceable handler, as on the tabs: a
        #: test has no way to close a modal window in the middle of the logic.
        self.confirm = self._ask_confirmation

        self.calc_tab = CalcTab(self.models, self.safety)
        self.records_tab = RecordsTab(
            payload_provider=self.calc_tab.current_payload,
            container_provider=self.calc_tab.current_container_mib,
            safety_provider=self.calc_tab.current_safety_mib,
        )
        # The check covers everything the model rests on: copy records, own
        # measurements and factory points. Otherwise the report would be silent
        # about exactly the factory points that hold up the NTFS curve:
        # twenty-six empty volumes and seven more from the copy-slack
        # measurements.
        self.model_tab = ModelTab(
            self.models, lambda: self.records_tab.store.all_for_model()
        )
        # Both kinds of machine measurements live in one file and are told
        # apart by a property: a copy-slack measurement has both data and left
        # space. The tab gets them separately — their tables differ.
        self.calibration_tab = CalibrationTab(
            lambda: self.records_tab.store.calibration_points(),
            lambda: self.records_tab.store.slack_measurements(),
        )

        #: Open chart windows by key. A window is created on first show and
        #: lives until the program closes: rebuilding it on every show would
        #: mean losing both the zoom and the window size.
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

    # --- data folder -------------------------------------------------------

    def _ask_data_dir(self) -> Path:
        """Find the data folder; ask if the program's folder is not writable.

        Quietly moving to some other directory is not allowed: the portable
        data folder would then silently stop being portable, and on another
        machine the data would not travel with it.
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

    # --- models ------------------------------------------------------------

    def models(self) -> tuple[MetadataModel, CopySlackModel]:
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

    # --- chart windows -----------------------------------------------------

    def _chart_specs(self) -> dict:
        """Window kinds: title, builders, whether the X axis is shared.

        Built anew on every show, because the closures must take today's
        store: `records_tab.store` is reopened when the data folder changes,
        and a store captured once would point to the previous one.
        """
        store = lambda: self.records_tab.store
        return {
            CHART_NTFS: (
                "Метаданные NTFS",
                (
                    # The list of recommended sizes — so that the curve can say
                    # in its caption which of them no measurement covers.
                    lambda: charts.ntfs_curve(store(), RECOMMENDED_MIB),
                    # Share of the volume — the same curve, but in the units
                    # metadata is sized up in by eye.
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
        """Show a chart window; one already open is raised, not duplicated."""
        specs = self._chart_specs()
        if key not in specs:
            return
        window = self._charts.get(key)
        if window is None:
            title, builders, link_x = specs[key]
            window = ChartWindow(key, title, builders, link_x, self)
            window.set_unit(unit_by_key(self.unit_combo.currentData()))
            # Detached charts come back together with the geometry: a window
            # spread out over the screen is arranged once, not on every launch.
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
        """The current-calculation window tracks the calculation, not records.

        Separate from the rest: the calculation changes with every keystroke in
        the size field, and there is no reason to redraw the NTFS curve for
        that.
        """
        window = self._charts.get(CHART_CALC)
        if window is not None and window.isVisible():
            window.refresh()

    # --- calibration points ------------------------------------------------

    def _take_point(self, container_mib: int) -> None:
        self.records_tab.add_calibration_point(container_mib)

    def covered_sizes(self) -> list[int]:
        """Sizes that already have a working own empty-volume measurement."""
        return [
            record.container_mib
            for record in self.records_tab.store.calibration_points()
            if not record.disabled and record.mounted_bytes
        ]

    def covered_filesets(self) -> list[str]:
        """File sets that already have an **own** copy-slack measurement.

        By file-set key, not by file count: two sets with `n = 1` are made
        different in total size on purpose, and both must be measured —
        comparing them is what checks that copy slack does not depend on file
        size.

        Only own ones, as with the sizes: "missing" means what is absent on
        this machine. A factory measurement covers a set only until there is
        an own one, and offering to measure it is right.
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
        """Build the automatic collection dialog and connect it to the store.

        The window creates the dialog, not the tab: collection needs both the
        store to put measurements into and the data folder — it has to be
        passed on to the program itself on an elevated restart, otherwise
        portability ends.
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
        """Remove a copy-slack measurement for good: disabling it is pointless.

        For a calibration point disabling makes sense — under it lies a factory
        value to go back to. A copy-slack measurement may have no factory one
        for the same file count at all, and "disabled" would mean simply hidden
        numbers.
        """
        if not self.confirm(
            "Удалить замер",
            f"Удалить «{record.id}»? Числа исчезнут из файла, "
            f"действие не отменяется.",
        ):
            return
        self.records_tab.remove_calibration(record)

    def _set_point_disabled(self, volume_bytes: int, disabled: bool) -> None:
        """Disable or restore the calibration point on this volume.

        Only the point: copy-slack measurements lie in the same list, and a
        shared pass over volume_bytes would touch them too. The sizes should
        not coincide — the container for a file set is moved off the
        recommended sizes on purpose — but there is no reason to rely on that
        when checking the property is enough.
        """
        store = self.records_tab.store
        for index, record in enumerate(store.calibration):
            if record.is_calibration_point and record.volume_bytes == volume_bytes:
                store.calibration[index] = replace(record, disabled=disabled)
        self.records_tab.save_store()

    def _disable_all_points(self) -> None:
        """Revert all points to factory. Copy-slack measurements are untouched.

        Those may have no factory measurement for the same file count at all,
        and "disable" would mean losing the copy-slack calibration entirely
        without saying a word about it.
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

    # --- view --------------------------------------------------------------

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
        """Open a tab by name; an unknown name opens the first tab.

        By name, not by number. The number changes with any reordering of the
        tabs, and the program then silently opens on the wrong page — with no
        way to notice, because it did open successfully. This also survives
        the old setting that held a number: "2" matches no title and honestly
        falls back to the first tab.
        """
        titles = [self.tabs.tabText(index) for index in range(self.tabs.count())]
        self.tabs.setCurrentIndex(titles.index(title) if title in titles else 0)

    def current_tab_title(self) -> str:
        return self.tabs.tabText(self.tabs.currentIndex())

    # --- preferences -------------------------------------------------------

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
        """Give the picker back its view, size and both check boxes.

        All of this is set in the dialog itself, but the window keeps it: the
        dialog lives for one showing, and a setting left inside it would not
        survive even Cancel.
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
        # With remembering off, the name is not overwritten: in such a session
        # the program opened on Calculation anyway, and writing that would wipe
        # out what was remembered without asking.
        if self.remember_tab.isChecked():
            self.settings.setValue(ACTIVE_TAB_KEY, self.current_tab_title())
        self.settings.setValue(REMEMBER_TAB_KEY, self.remember_tab.isChecked())
        self._store_picker()
        # The chart windows' layout is stored under its own name, not by
        # number: the same rule as for the active tab.
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
