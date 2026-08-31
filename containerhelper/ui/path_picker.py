"""Один диалог выбора и файлов, и папок сразу."""

from __future__ import annotations

import os

from PySide6.QtCore import QDir, QItemSelection, QItemSelectionModel
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
)

HINT = (
    "Двойной щелчок заходит в папку, «Выбрать» берёт выделенное целиком. "
    "Мышью можно обвести рамкой, Ctrl и Shift выделяют по одному и подряд — "
    "файлы и папки можно смешивать. "
    "Папка внутри другой выбранной папки второй раз не считается."
)

#: Виды, в которых лежат файлы и папки: простой список и подробный. По именам,
#: а не перебором всех QAbstractItemView: боковая панель — тоже вид, но
#: перетаскивание в ней нужно, им туда складывают закладки.
FILE_VIEWS = ("listView", "treeView")


class PathPicker(QFileDialog):
    """Выбор файлов и папок в одном списке.

    Родные диалоги Windows умеют либо файлы, либо одну папку: под капотом это
    два разных системных вызова, и третьего они не предлагают. Поэтому берётся
    собственный диалог Qt — список в нём один и показывает и то, и другое — а
    правится в нём ровно одно: «Выбрать» на папке не заходит внутрь, а отдаёт
    её как источник. Внутрь по-прежнему пускает двойной щелчок.
    """

    def __init__(
        self,
        parent=None,
        directory: str = "",
        show_hidden: bool = False,
    ) -> None:
        super().__init__(parent, "Выберите файлы и папки", directory)
        # Штатный режим ExistingFiles: он показывает и папки (иначе по ним не
        # пройтись), и множественное выделение в нём уже настроено.
        self.setOption(QFileDialog.DontUseNativeDialog, True)
        self.setFileMode(QFileDialog.ExistingFiles)
        self.setLabelText(QFileDialog.Accept, "Выбрать")
        self.setLabelText(QFileDialog.Reject, "Отмена")
        self.setLabelText(QFileDialog.FileName, "Выбрано:")
        self.setToolTip(HINT)
        self._chosen: list[str] = []

        self._views: list[QAbstractItemView] = []
        for name in FILE_VIEWS:
            view = self.findChild(QAbstractItemView, name)
            if view is None:
                continue
            # В режиме ExistingFiles Qt даёт множественное выделение только
            # списку; в подробном виде выделять пришлось бы по одному.
            view.setSelectionMode(QAbstractItemView.ExtendedSelection)
            # Видам файлового диалога Qt ставит InternalMove, и протяжка мышью
            # начинает перетаскивание вместо рамки выделения: обвести мышью
            # несколько имён нельзя вовсе, остаются только Ctrl и Shift.
            view.setDragEnabled(False)
            view.setDragDropMode(QAbstractItemView.NoDragDrop)
            self._views.append(view)

        self._hidden_action = self._find_hidden_action()
        self._add_controls(show_hidden)
        self._add_hint()

    # --- скрытые файлы -----------------------------------------------------

    def _find_hidden_action(self) -> QAction | None:
        """Собственный пункт диалога «показывать скрытые».

        Он там один такой: из действий, чей родитель — сам диалог, галочку
        носит только он, а «Показывать размер» и соседи принадлежат заголовку
        подробного вида. По названию искать нельзя — оно переводится.
        """
        own = [
            action
            for action in self.findChildren(QAction)
            if action.parent() is self and action.isCheckable()
        ]
        return own[0] if len(own) == 1 else None

    @property
    def show_hidden(self) -> bool:
        return bool(self.filter() & QDir.Hidden)

    def set_show_hidden(self, show: bool) -> None:
        """Переключить показ скрытых через собственный пункт диалога.

        Не своим setFilter: тот же переключатель у диалога есть в контекстном
        меню списка, и два независимых пути разошлись бы — галочка говорила бы
        одно, а список показывал другое.
        """
        if self.show_hidden == show:
            return
        if self._hidden_action is not None:
            self._hidden_action.trigger()
            return
        # Пункт не нашёлся — значит, Qt его переустроил. Остаётся фильтр
        # напрямую: без связи с меню, но работающий.
        if show:
            self.setFilter(self.filter() | QDir.Hidden)
        else:
            self.setFilter(self.filter() & ~QDir.Hidden)

    def _sync_hidden_check(self, *_args) -> None:
        """Привести галочку к тому, что на самом деле в фильтре."""
        self.hidden_check.setChecked(self.show_hidden)

    # --- выделение ---------------------------------------------------------

    def _active_view(self) -> QAbstractItemView | None:
        """Вид, на который сейчас смотрят: простой список или подробный.

        Их два, и виден всегда один. До показа диалога не виден ни один —
        тогда берётся список: с него диалог и открывается.
        """
        for view in self._views:
            if view.isVisible():
                return view
        return self._views[0] if self._views else None

    def _select_all(self) -> None:
        view = self._active_view()
        if view is not None:
            view.selectAll()

    def _select_none(self) -> None:
        view = self._active_view()
        if view is not None:
            view.clearSelection()

    def _invert_selection(self) -> None:
        """Перевернуть выделение: выбранное снять, остальное выбрать.

        Toggle по всему прямоугольнику папки, а не обход строк по одной:
        выделение меняется одним сигналом, и список не перерисовывается
        столько раз, сколько в папке файлов.
        """
        view = self._active_view()
        if view is None:
            return
        model = view.model()
        root = view.rootIndex()
        rows, columns = model.rowCount(root), model.columnCount(root)
        if not rows or not columns:
            return
        whole = QItemSelection(
            model.index(0, 0, root), model.index(rows - 1, columns - 1, root)
        )
        view.selectionModel().select(whole, QItemSelectionModel.Toggle)

    # --- обвязка -----------------------------------------------------------

    def _add_controls(self, show_hidden: bool) -> None:
        """Кнопки выделения и галочка скрытых — своей строкой под списком."""
        layout = self.layout()
        if not isinstance(layout, QGridLayout):
            return

        row = QHBoxLayout()
        for title, tip, slot in (
            (
                "Выделить всё",
                "Выбрать всё в этой папке. То же делает Ctrl+A.",
                self._select_all,
            ),
            (
                "Снять выделение",
                "Отпустить выделенное, не закрывая диалог.",
                self._select_none,
            ),
            (
                "Инвертировать",
                "Выделить всё, кроме выделенного сейчас. Так проще взять "
                "папку целиком без двух-трёх лишних имён.",
                self._invert_selection,
            ),
        ):
            button = QPushButton(title)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)
        row.addStretch(1)

        self.hidden_check = QCheckBox("Показывать скрытые")
        self.hidden_check.setToolTip(
            "Показывать скрытые файлы и папки. Настройку Проводника диалог не "
            "наследует, а держит свою — она сохраняется в папке данных рядом "
            "с остальными настройками вида.\n"
            "На расчёт не влияет: внутри выбранной папки скрытые файлы "
            "считаются всегда, обход их не пропускает."
        )
        self.hidden_check.toggled.connect(self.set_show_hidden)
        if self._hidden_action is not None:
            # Тот же переключатель есть в контекстном меню списка. Без этой
            # связи галочка после него врала бы до самого закрытия диалога.
            #
            # Именно triggered, а не toggled: toggled приходит до того, как
            # диалог правит фильтр, галочка успевает дёрнуть set_show_hidden,
            # тот видит старое состояние и переключает пункт второй раз —
            # нажатие в меню не делает ничего.
            self._hidden_action.triggered.connect(self._sync_hidden_check)
        row.addWidget(self.hidden_check)

        self.set_show_hidden(show_hidden)
        self.hidden_check.setChecked(self.show_hidden)
        layout.addLayout(row, layout.rowCount(), 0, 1, max(layout.columnCount(), 1))

    def _add_hint(self) -> None:
        """Подсказка снизу: без неё «Выбрать» на папке выглядит как промах."""
        layout = self.layout()
        if not isinstance(layout, QGridLayout):
            return
        hint = QLabel(HINT)
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(hint, layout.rowCount(), 0, 1, max(layout.columnCount(), 1))

    def accept(self) -> None:
        """Забрать выделенное, не заходя в папку.

        Базовый accept на папке уходит внутрь неё, и выбрать папку кнопкой
        становится нечем. Пустое выделение оставляет диалог открытым: закрывать
        его ни с чем — то же самое, что «Отмена», но менее понятно.
        """
        chosen = [path for path in self.selectedFiles() if os.path.exists(path)]
        if not chosen:
            return
        self._chosen = chosen
        QDialog.accept(self)

    def chosen_paths(self) -> list[str]:
        return list(self._chosen)


def ask_paths(
    parent,
    directory: str = "",
    show_hidden: bool = False,
) -> tuple[list[str], bool]:
    """Показать диалог; вернуть выбранное и состояние галочки скрытых.

    Галочка возвращается и при отказе: её переключили осознанно, и терять это
    из-за нажатой «Отмены» незачем. Хранит её окно — диалог живёт один показ.
    """
    dialog = PathPicker(parent, directory, show_hidden)
    accepted = dialog.exec() == QDialog.Accepted
    return (dialog.chosen_paths() if accepted else []), dialog.show_hidden
