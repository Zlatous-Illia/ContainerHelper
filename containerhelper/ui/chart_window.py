"""Немодальные окна с графиками.

Окнами, а не пятой вкладкой: вкладки идут порядком работы — «Расчёт», «Записи»,
«Модель», «Калибровка», — и «Графики» в этот порядок не встают. Отдельное окно
к тому же растягивается на весь экран, а вкладке пришлось бы делить высоту с
таблицей.

Окно ничего не знает о хранилище: графики ему приносят готовыми, а собирает их
`charts.py`. Поэтому здесь нет ни одной величины в байтах.
"""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..formatting import DEFAULT_UNIT, Unit
from ..plot import Chart
from .chart import ChartView

#: Ключи окон. Ими же именуется сохранённая геометрия, поэтому строки, а не
#: номера: от перестановки список номеров разъехался бы молча — то же правило,
#: что и для активной вкладки.
CHART_NTFS = "ntfs"
CHART_SLACK = "slack"
CHART_FORECAST = "forecast"
CHART_CALC = "calc"

HINT = (
    "Рамка мышью — приблизить, колесо — масштаб, средняя кнопка — сдвиг, "
    "двойной щелчок или Esc — сброс. Щелчок по легенде прячет серию, "
    "щелчок по точке показывает её целиком."
)

Builder = Callable[[], Chart]


class ChartWindow(QWidget):
    """Одно окно с одним или несколькими графиками."""

    def __init__(
        self,
        key: str,
        title: str,
        builders: Sequence[Builder],
        link_x: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.Window)
        self.key = key
        self._builders = list(builders)
        self._link_x = link_x
        self._unit: Unit = DEFAULT_UNIT
        self.setWindowTitle(title)
        self.resize(760, 300 + 260 * len(self._builders))

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_actions())

        self.splitter = QSplitter(Qt.Vertical)
        self.views: list[ChartView] = []
        for _builder in self._builders:
            view = ChartView()
            view.pointPicked.connect(self._on_picked)
            view.rangeChanged.connect(self._on_range_changed)
            self.splitter.addWidget(view)
            self.views.append(view)
        layout.addWidget(self.splitter, 1)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setToolTip(
            "Что за точка под курсором. Заполняется щелчком по ней — таблица "
            "с числами живёт в главном окне, и подсвечивать в ней строку "
            "из-под другого окна было бы некуда смотреть."
        )
        layout.addWidget(self.detail)

        hint = QLabel(HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(hint)

        self.refresh()

    def _build_actions(self) -> QHBoxLayout:
        row = QHBoxLayout()

        self.reset_button = QPushButton("Сбросить масштаб")
        self.reset_button.setToolTip(
            "Вернуть все графики окна к полному виду. То же делает двойной "
            "щелчок по графику или Esc."
        )
        self.reset_button.clicked.connect(self.reset_zoom)
        row.addWidget(self.reset_button)

        self.copy_button = QPushButton("Копировать картинку")
        self.copy_button.setToolTip(
            "Положить график в буфер обмена. Копируется тот, на который "
            "наводили последним."
        )
        self.copy_button.clicked.connect(lambda: self.active_view().copy_image())
        row.addWidget(self.copy_button)

        self.save_button = QPushButton("Сохранить картинку…")
        self.save_button.setToolTip(
            "PNG, SVG или PDF — по расширению в имени файла. SVG и PDF "
            "векторные: их можно увеличивать без потери."
        )
        self.save_button.clicked.connect(lambda: self.active_view().save_image())
        row.addWidget(self.save_button)

        row.addStretch(1)
        return row

    # --- данные ------------------------------------------------------------

    def refresh(self) -> None:
        """Пересобрать графики. Зовётся, когда изменились записи или модели.

        Без этого окно молча показывало бы вчерашнюю картинку: замер сняли,
        модель поехала, а на графике всё по-старому — и отличить свежее от
        несвежего на глаз нечем.
        """
        for view, builder in zip(self.views, self._builders):
            view.set_chart(builder())
            view.set_unit(self._unit)
        self.detail.setText("")

    def set_unit(self, unit: Unit) -> None:
        self._unit = unit
        for view in self.views:
            view.set_unit(unit)

    def reset_zoom(self) -> None:
        for view in self.views:
            view.reset_zoom()

    # --- связь между графиками ---------------------------------------------

    def active_view(self) -> ChartView:
        """Тот график, на который смотрят. По умолчанию — верхний."""
        for view in self.views:
            if view.hasFocus() or view.underMouse():
                return view
        return self.views[0]

    def _on_range_changed(self) -> None:
        """Держать общую ось X, если графики её делят.

        Общая ось — не украшение: горб на остатках должен стоять ровно под
        своей ступенькой наклона, иначе два графика читаются порознь и связь
        между ними теряется.
        """
        if not self._link_x:
            return
        source = self.sender()
        if not isinstance(source, ChartView):
            return
        span = source.x_span()
        for view in self.views:
            if view is not source:
                view.apply_x(span)

    def _on_picked(self, _key: object, tip: str) -> None:
        self.detail.setText(tip.replace("\n", "   ·   "))
