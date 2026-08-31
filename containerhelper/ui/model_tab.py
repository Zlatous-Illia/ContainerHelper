"""Вкладка «Модель»: состояние калибровки и её проверка."""

from __future__ import annotations

from typing import Callable, Sequence

from PySide6.QtCore import Qt, Signal
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
    plural,
    unit_suffix,
)
from ..model import MIB, CopySlackModel, NtfsModel
from ..records import (
    Check,
    Record,
    ntfs_cross_check,
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

#: Заголовок, признак «столбец в байтах» и подсказка. К байтовым дописывается
#: выбранная единица, и сортируются они по числу, а не по тексту.
CHECK_COLUMNS = (
    (
        "Запись",
        False,
        "Чей это замер. Сама запись лежит на вкладке «Записи» или "
        "«Калибровка».",
    ),
    (
        "Измерено",
        True,
        "Что показал настоящий том. С этим и сравниваем.",
    ),
    (
        "Предсказано",
        True,
        "Что дала бы модель, если бы этого замера у неё не было. Иначе она "
        "прошла бы точно через свою же точку, и проверять было бы нечего.",
    ),
    (
        "Отклонение",
        True,
        "Измерено минус предсказано. Плюс — модель занизила, и разницу "
        "должен покрыть запас. Минус — заложила лишнего.",
    ),
    (
        "Основа",
        False,
        "«калибровка» — предсказание опирается на другие замеры.\n"
        "«по умолчанию» — замеров не хватило, работала формула по умолчанию.",
    ),
)


class ModelTab(QWidget):
    safetyChanged = Signal(int)
    #: Просьба показать окно с графиками — по ключу окна.
    chartRequested = Signal(str)

    def __init__(
        self,
        models: Callable[[], tuple[NtfsModel, CopySlackModel]],
        records: Callable[[], Sequence[Record]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._models = models
        self._records = records
        self._unit: Unit = DEFAULT_UNIT
        #: Когда запас подбирается на вкладке «Расчёт», это поле только
        #: показывает результат — править его отсюда бессмысленно.
        self._auto = False
        #: Последние посчитанные проверки. Держатся, чтобы подсказку о запасе
        #: можно было обновить, не пересобирая обе таблицы на каждое изменение
        #: значения.
        self._checks: tuple[list, list] = ([], [])
        self._safety_mib = 0

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_state())
        layout.addWidget(self._build_safety())

        # Каждая секция кончается своей таблицей, поэтому ручка разделителя
        # приходится ровно на её нижний контур.
        # Разделителя тут больше нет. Он делит фиксированную высоту между
        # соседями, а менять надо высоту самой таблицы — тогда вкладка
        # становится выше окна и прокрутка удлиняется. У каждой таблицы своя
        # ручка под нижним контуром.
        layout.addWidget(self._build_checks("Проверка модели NTFS", "ntfs"))
        layout.addWidget(self._build_checks("Проверка запаса на копирование", "slack"))
        layout.addLayout(self._build_chart_row())
        layout.addStretch(1)

        self.refresh()

    def _build_chart_row(self) -> QHBoxLayout:
        """Кнопки графиков под обеими таблицами проверки.

        Рядом с числами, которые они объясняют: таблица говорит, на сколько
        модель промахнулась, а график — где именно и на что это похоже.
        """
        row = QHBoxLayout()
        for title, key, tip in (
            (
                "График метаданных…",
                CHART_NTFS,
                "Кривая NTFS, промах проверкой исключением и наклон каждого "
                "отрезка — в одном окне, с общей осью.",
            ),
            (
                "График запаса…",
                CHART_SLACK,
                "Измеренный запас против числа файлов и прямая модели, а под "
                "ними — промах проверкой исключением.",
            ),
        ):
            button = QPushButton(title)
            button.setToolTip(
                tip + "\nОкно немодальное: его можно оставить рядом и "
                "смотреть, как меняется картинка после нового замера."
            )
            button.clicked.connect(
                lambda _checked=False, name=key: self.chartRequested.emit(name)
            )
            row.addWidget(button)
        row.addStretch(1)
        return row

    # --- построение --------------------------------------------------------

    def _build_state(self) -> QGroupBox:
        group = QGroupBox("Состояние калибровки")
        group.setToolTip(
            "Сколько замеров держат обе модели и какой диапазон те "
            "покрывают."
        )
        layout = QVBoxLayout(group)
        self.ntfs_state = QLabel()
        self.ntfs_state.setWordWrap(True)
        self.ntfs_state.setToolTip(
            "Метаданные NTFS в зависимости от размера тома. Внутри "
            "покрытого замерами диапазона считаем по ним, снаружи — базовым "
            "приростом от края.\n"
            "Добавить точку можно на вкладке «Калибровка»."
        )
        self.slack_state = QLabel()
        self.slack_state.setWordWrap(True)
        self.slack_state.setToolTip(
            "Сколько файлы занимают сверх своего кластерного размера: "
            "постоянная часть плюс столько-то на каждый файл.\n"
            "По-файловую часть видно только по записям с разным числом "
            "файлов; на одном и том же она остаётся догадкой."
        )
        layout.addWidget(self.ntfs_state)
        layout.addWidget(self.slack_state)
        return group

    def _build_safety(self) -> QGroupBox:
        """Только показ. Крутить запас отсюда нельзя намеренно.

        Он зависит от размера тома и числа файлов, а их знает лишь вкладка
        «Расчёт». Второе поле здесь дублировало первое и при автоподборе
        откатывалось само — выглядело сломанным, потому что таким и было.
        """
        group = QGroupBox("Страховочный запас")
        group.setToolTip(
            "Здесь запас только показан. Правится на «Расчёте» — он зависит "
            "от размера тома и числа файлов, а их знает только она."
        )
        column = QVBoxLayout(group)
        self.safety_value = QLabel()
        self.safety_value.setWordWrap(True)
        self.safety_value.setToolTip(
            "Сколько заложено поверх расчёта и откуда это число — посчитано "
            "автоматически или задано руками."
        )
        column.addWidget(self.safety_value)
        self.safety_hint = QLabel()
        self.safety_hint.setWordWrap(True)
        self.safety_hint.setStyleSheet("color: palette(mid);")
        self.safety_hint.setToolTip(
            "Это справка, а не требование. Проверка меряет модель без одной "
            "точки, то есть более редкую, чем настоящая, и её промах не "
            "спадает от новых замеров. Держать по нему запас на всех размерах "
            "значит платить везде за самое пустое место сетки."
        )
        column.addWidget(self.safety_hint)
        return group

    def _build_checks(self, title: str, kind: str) -> QWidget:
        """Секция проверки: подписи сверху, таблица последней.

        Таблица идёт последней намеренно. Ручка разделителя садится на нижний
        край секции, и если после таблицы поставить ещё подпись, тянуть
        придётся за серую границу возле текста, а не за чёрный контур
        таблицы. Итог поэтому переехал наверх, под заголовок.
        """
        caption = QLabel(f"<b>{title}</b>")
        explanation = QLabel(
            "Каждый замер предсказан моделью, собранной без него самого — "
            "иначе отклонение всегда было бы нулевым."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: palette(mid);")

        table = QTableWidget(0, len(CHECK_COLUMNS))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.setToolTip(
            f"{title}: каждый замер против модели, собранной без него.\n"
            f"Щелчок по заголовку сортирует: по возрастанию, по убыванию, "
            f"без сортировки."
        )
        setup_table(table)
        set_restore_order(table, self.refresh)

        summary = QLabel()
        summary.setWordWrap(True)
        summary.setToolTip(
            "Где модель занизила сильнее всего. Занижение опасно, его должен "
            "покрыть запас; перезаклад стоит только лишнего места."
        )

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
        self._apply_headers(table)
        return section

    def _apply_headers(self, table: QTableWidget) -> None:
        table.setHorizontalHeaderLabels(
            [
                title + (unit_suffix(self._unit) if is_bytes else "")
                for title, is_bytes, _tip in CHECK_COLUMNS
            ]
        )
        set_header_tooltips(table, [tip for _t, _b, tip in CHECK_COLUMNS])

    def set_unit(self, unit: Unit) -> None:
        self._unit = unit
        for table in (self.ntfs_table, self.slack_table):
            self._apply_headers(table)
        self.refresh()

    def set_expand_tables(self, expand: bool) -> None:
        for table in (self.ntfs_table, self.slack_table):
            apply_table_height(table, expand)

    # --- обновление --------------------------------------------------------

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
        ntfs_checks = ntfs_cross_check(records)
        slack_checks = slack_cross_check(records)
        self._checks = (ntfs_checks, slack_checks)
        self._fill_checks(self.ntfs_table, self.ntfs_summary, ntfs_checks)
        self._fill_checks(self.slack_table, self.slack_summary, slack_checks)
        self._refresh_safety_hint(ntfs_checks, slack_checks)

    def _refresh_state(self, ntfs: NtfsModel, slack: CopySlackModel) -> None:
        if ntfs.calibrated:
            low, high = ntfs.covered_range
            self.ntfs_state.setText(
                f"<b>NTFS:</b> {len(ntfs.points)} "
                f"{plural(len(ntfs.points), 'точка', 'точки', 'точек')}, "
                f"диапазон томов "
                f"{fmt_bytes(low)}…{fmt_bytes(high)} B "
                f"({low / 1024 ** 3:.1f}…{high / 1024 ** 3:.1f} GiB). "
                f"Внутри диапазона работают измерения, снаружи — базовый прирост "
                f"{ntfs.rate * 100:.2f} % от края замеров."
            )
        else:
            self.ntfs_state.setText(
                f"<b>NTFS:</b> точек {len(ntfs.points)}, нужно минимум две. "
                f"Работает модель по умолчанию: {ntfs.base // MIB} MiB + "
                f"{ntfs.rate * 100:.2f} % от размера тома."
            )

        counts = sorted(set(slack.file_counts))
        if slack.per_file_calibrated:
            detail = f"файлов в замерах: от {counts[0]} до {counts[-1]}"
        elif slack.calibrated:
            detail = (
                f"все {slack.sample_count} "
                f"{plural(slack.sample_count, 'замер', 'замера', 'замеров')} "
                f"при n = {counts[0]}, по-файловая часть осталась "
                f"предположением"
            )
        else:
            detail = "замеров нет, обе части — значения по умолчанию"
        self.slack_state.setText(
            f"<b>Запас на копирование:</b> {fmt_bytes(slack.base)} B + "
            f"{fmt_bytes(slack.per_file)} B на файл; {detail}."
        )

    def _fill_checks(
        self, table: QTableWidget, summary: QLabel, checks: Sequence[Check]
    ) -> None:
        table.setSortingEnabled(False)
        table.setRowCount(len(checks))
        for row, check in enumerate(checks):
            values = (
                check.record.id,
                check.measured,
                check.predicted,
                check.deviation,
                "калибровка" if check.calibrated else "по умолчанию",
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
                    item.setToolTip("Модель занизила — это должен покрыть запас.")
                table.setItem(row, column, item)
        table.setSortingEnabled(True)
        if checks and not self._fitted(table):
            fit_columns(table)

        if not checks:
            summary.setText("Записей для проверки пока нет.")
            return

        shortfall = worst_shortfall(checks)
        if shortfall > 0:
            summary.setText(
                f"Наибольшая недооценка: {fmt_both(shortfall)}. "
                f"Запас должен быть не меньше."
            )
        else:
            summary.setText(
                f"Модель нигде не занизила; наибольший перезаклад "
                f"{fmt_both(-min(check.deviation for check in checks))}."
            )

    def _fitted(self, table: QTableWidget) -> bool:
        """Ширину подгоняем один раз, дальше она принадлежит пользователю."""
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
            self.safety_hint.setText("Проверить не на чем: записей нет.")
            return

        # Наибольшая недооценка по всем записям — не требование, а диагностика.
        # Проверка исключением меряет модель без одной точки, то есть вдвое
        # более редкую, чем настоящая; её промах не спадает от добавления
        # замеров и требовать по нему страховку на все размеры сразу значит
        # платить везде за то место, где сетка реже всего. Сколько нужно
        # конкретному расчёту, считает автоподбор на вкладке «Расчёт».
        diagnostic = (
            f"Худшая недооценка при проверке исключением: {fmt_both(worst)}. "
            f"Это оценка модели без одной точки, а не требование к запасу."
        )
        where = (
            "подбирается под каждый расчёт на вкладке «Расчёт»"
            if self._auto
            else "задан вручную на вкладке «Расчёт»"
        )
        self.safety_value.setText(f"<b>{fmt_both(current)}</b> — {where}.")
        self.safety_hint.setText(diagnostic)
