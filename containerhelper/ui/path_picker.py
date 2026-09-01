"""Один диалог выбора и файлов, и папок сразу."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field

from PySide6.QtCore import QByteArray, QDir, QItemSelection, QItemSelectionModel
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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

#: Пустая папка — это «Компьютер», список дисков. Проверено: QFileDialog с
#: `setDirectory("")` показывает именно диски, а не текущий каталог процесса.
#: Папка программы в качестве начальной не годится вовсе: данные лежат где
#: угодно, только не рядом с ней.
COMPUTER = ""


@dataclass
class PickerState:
    """Всё, что диалог обязан пережить между показами.

    Живёт снаружи, потому что сам диалог живёт один показ: настройка,
    оставшаяся в нём, не пережила бы даже «Отмену». Хранит и записывает в
    settings.ini главное окно.

    `directory` — папка, **которую диалог показывал**, а не выбранная в нём.
    Разница не косметическая: выбрав в папке 1 папку 2, в следующий раз надо
    открыться снова в папке 1 — рядом с папкой 2 лежит то, что выбирают
    следующим. Начальная папка, взятая из выбранного пути, уводила на уровень
    вглубь на каждый показ.
    """

    show_hidden: bool = False
    #: Открываться там, где закрылись. Выключено — всегда «Компьютер».
    remember_dir: bool = True
    directory: str = COMPUTER
    #: Размер окна — двумя числами, а не `saveGeometry`.
    #:
    #: `restoreGeometry` отказывается работать молча: она сверяет ширину
    #: экрана, на котором геометрию сохранили, с нынешней, и при расхождении
    #: больше четверти **возвращает false, ничего не сделав**. Дальше QDialog
    #: видит, что размер никто не задавал, и подгоняет окно под содержимое —
    #: со стороны это и выглядит как «размеры сбрасываются каждый запуск».
    #: Два числа таких проверок не проходят.
    width: int = 0
    height: int = 0
    #: Вид списка, ширины столбцов подробного вида и боковая панель —
    #: всё это умеет отдать сам QFileDialog одним куском.
    layout: QByteArray = field(default_factory=QByteArray)

    def start_directory(self) -> str:
        return self.directory if self.remember_dir else COMPUTER

    @property
    def sized(self) -> bool:
        return self.width > 0 and self.height > 0


class PathPicker(QFileDialog):
    """Выбор файлов и папок в одном списке.

    Родные диалоги Windows умеют либо файлы, либо одну папку: под капотом это
    два разных системных вызова, и третьего они не предлагают. Поэтому берётся
    собственный диалог Qt — список в нём один и показывает и то, и другое — а
    правится в нём ровно одно: «Выбрать» на папке не заходит внутрь, а отдаёт
    её как источник. Внутрь по-прежнему пускает двойной щелчок.
    """

    def __init__(self, parent=None, state: PickerState | None = None) -> None:
        self._state = state or PickerState()
        super().__init__(parent, "Выберите файлы и папки", self._state.start_directory())
        # Штатный режим ExistingFiles: он показывает и папки (иначе по ним не
        # пройтись), и множественное выделение в нём уже настроено.
        self.setOption(QFileDialog.DontUseNativeDialog, True)
        self.setFileMode(QFileDialog.ExistingFiles)
        self.setLabelText(QFileDialog.Accept, "Выбрать")
        self.setLabelText(QFileDialog.Reject, "Отмена")
        self.setLabelText(QFileDialog.FileName, "Выбрано:")
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
            view.selectionModel().selectionChanged.connect(self._sync_name)
            # Перечитанная папка снимает выделение, но сигнала о смене
            # выделения при этом не шлёт: строка осталась бы с именами от
            # прежнего содержимого.
            model = view.model()
            model.modelReset.connect(self._sync_name)
            loaded = getattr(model, "directoryLoaded", None)
            if loaded is not None:
                loaded.connect(self._sync_name)
            self._views.append(view)

        #: Строка «Выбрано:». Её держит сам QFileDialog, и по ней же он
        #: отвечает на `selectedFiles()`.
        self._name_edit = self.findChild(QLineEdit, "fileNameEdit")
        self._accept_button = self._find_accept_button()
        #: Имя в строке набрали руками. Тогда и только тогда строка что-то
        #: значит сама по себе: во всех прочих случаях её пишем мы по
        #: выделению, и доверять ей нельзя — папка могла перечитаться, а
        #: имена в ней остаться. Ровно так «Выбрать» и добавляла файлы,
        #: которых никто не выделял.
        self._typed = False
        if self._name_edit is not None:
            self._name_edit.textEdited.connect(self._on_typed)

        self._hidden_action = self._find_hidden_action()
        self._add_controls()
        self._add_hint()
        self._restore_state()

    # --- память между показами ---------------------------------------------

    def current_directory(self) -> str:
        """Папка, которую диалог показывает сейчас. Пусто — «Компьютер».

        Спрашивается у самого списка, а не у `directory()`: на «Компьютере»
        тот отдаёт не список дисков, а рабочий каталог процесса, и запомнить
        его значило бы запомнить чужое место. `directoryEntered` тоже не
        годится — программная смена папки его не поднимает вовсе.
        """
        view = self._active_view()
        if view is None:
            return self.directory().absolutePath()
        return view.model().filePath(view.rootIndex())

    def _find_accept_button(self):
        """Кнопка «Выбрать». Нужна затем, что включать её теперь нам.

        Диалог включает её по правке строки «Выбрано», а строка у нас молчит,
        пока мы правим выделение, — иначе он на каждую правку отвечает
        автодополнением и сбрасывает выделение.
        """
        box = self.findChild(QDialogButtonBox)
        if box is None:
            return None
        for button in box.buttons():
            if box.buttonRole(button) == QDialogButtonBox.AcceptRole:
                return button
        return None

    def showEvent(self, event) -> None:  # noqa: N802 — имя от Qt
        """Вернуть размер прошлого показа — после того, как окно уже открыто.

        Именно здесь, а не в конструкторе: QDialog при показе сам подгоняет
        размер под содержимое, если считает, что его никто не задавал.
        """
        super().showEvent(event)
        if self._state.sized and not self._sized:
            self._sized = True
            self.resize(self._state.width, self._state.height)

    def _restore_state(self) -> None:
        """Вернуть вид и размер прошлого показа.

        Порядок важен: restoreState ставит вид списка и ширины столбцов, а
        restoreGeometry — размер окна. Первый умеет менять и размер, поэтому
        геометрия применяется после него, иначе окно съезжает к тому, каким
        было при сохранении вида.
        """
        if not self._state.layout.isEmpty():
            self.restoreState(self._state.layout)
            # restoreState возвращает и папку, в которой сохранялись. Нам она
            # не годится: где открываться, решает галочка «Запоминать папку».
            self.setDirectory(self._state.start_directory())
        #: Размер ставится один раз, при первом показе.
        self._sized = False
        self.set_show_hidden(self._state.show_hidden)
        self.hidden_check.setChecked(self.show_hidden)

    def store_state(self) -> PickerState:
        """Сложить состояние обратно. Зовётся и при «Отмене».

        Вид, размер окна и обе галочки переключают осознанно, и терять это
        из-за нажатой «Отмены» незачем.
        """
        state = self._state
        state.show_hidden = self.show_hidden
        state.remember_dir = self.remember_check.isChecked()
        state.directory = self.current_directory()
        state.width, state.height = self.width(), self.height()
        state.layout = self.saveState()
        return state

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

    def selected_paths(self) -> list[str]:
        """Что выделено в списке прямо сейчас.

        У самого списка, а не у `selectedFiles()`: тот отвечает по строке
        «Выбрано», а строка живёт своей жизнью. Сняв выделение, в ней
        оставались прежние имена — и «Выбрать» добавляла файлы, которых на
        экране никто уже не выделял. Инверсия из пустоты и обратно проделывала
        то же самое: выделили всё, сняли всё, а строка помнит всё.
        """
        view = self._active_view()
        if view is None:
            return []
        model = view.model()
        return [
            model.filePath(index)
            for index in view.selectionModel().selectedIndexes()
            if index.column() == 0
        ]

    @contextmanager
    def _quiet_edit(self):
        """Строка «Выбрано» молчит, пока мы правим выделение.

        Диалог отвечает на всякую правку этой строки автодополнением: он
        выделяет в списке то, что в ней написано, а чего в ней нет — снимает.
        Своих имён он туда кладёт только файлы, поэтому «Выделить всё» тут же
        теряло все папки, а вторая инверсия подряд работала уже над не тем
        набором, что показан. Пока идёт наша правка, строка не подаёт
        сигналов, и никто ничего не переставляет.
        """
        edit = self._name_edit
        if edit is None:
            yield
            return
        edit.blockSignals(True)
        try:
            yield
        finally:
            edit.blockSignals(False)

    def _sync_name(self, *_args) -> None:
        """Переписать строку «Выбрано» тем, что на самом деле выделено.

        Своими руками и целиком — с папками. Диалог кладёт туда только файлы:
        папку он считает не выбором, а дорогой вглубь, и в строке её не
        показывает вовсе.
        """
        if self._name_edit is None:
            return
        names = [os.path.basename(path) for path in self.selected_paths()]
        if len(names) == 1:
            text = names[0]
        else:
            # Кавычки — соглашение самого QFileDialog для нескольких имён.
            text = " ".join(f'"{name}"' for name in names)
        if self._name_edit.text() != text:
            with self._quiet_edit():
                self._name_edit.setText(text)
        self._typed = False
        self._refresh_accept()

    def _on_typed(self, *_args) -> None:
        self._typed = True
        self._refresh_accept()

    def _refresh_accept(self) -> None:
        """«Выбрать» доступна, пока есть что выбирать."""
        if self._accept_button is None:
            return
        typed = self._name_edit.text().strip() if self._name_edit else ""
        self._accept_button.setEnabled(bool(self.selected_paths()) or bool(typed))

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
        with self._quiet_edit():
            if view is not None:
                view.selectAll()
        self._sync_name()

    def _select_none(self) -> None:
        view = self._active_view()
        with self._quiet_edit():
            if view is not None:
                view.clearSelection()
        self._sync_name()

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
        with self._quiet_edit():
            view.selectionModel().select(whole, QItemSelectionModel.Toggle)
        self._sync_name()

    # --- обвязка -----------------------------------------------------------

    def _add_controls(self) -> None:
        """Кнопки выделения и галочки — своей строкой под списком."""
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

        self.remember_check = QCheckBox("Запоминать папку")
        self.remember_check.setToolTip(
            "Открываться там, где закрылись в прошлый раз. Выключено — "
            "открываться на «Компьютере», списком дисков.\n"
            "Запоминается показанная папка, а не выбранная в ней: рядом с "
            "выбранным обычно лежит и следующее."
        )
        self.remember_check.setChecked(self._state.remember_dir)
        row.addWidget(self.remember_check)

        self.hidden_check = QCheckBox("Показывать скрытые")
        self.hidden_check.setToolTip(
            "Настройку Проводника диалог не наследует, а держит свою — она "
            "сохраняется в папке данных рядом с остальными настройками вида.\n"
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

        layout.addLayout(row, layout.rowCount(), 0, 1, max(layout.columnCount(), 1))

    def _add_hint(self) -> None:
        """Подсказка снизу: без неё «Выбрать» на папке выглядит как промах.

        Только подписью, без подсказки на самом диалоге: та наследуется всеми
        детьми без своей, и наведение на любой файл в списке показывало тот же
        текст, что и так написан внизу.
        """
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
        # Выделение списка главнее строки: она отвечает и за набранное руками
        # имя, но пока в списке что-то выделено, речь именно о нём. Пустая
        # строка при пустом выделении не годится вовсе: `selectedFiles()`
        # отдаёт тогда саму папку, и «Выбрать» молча брала бы её целиком.
        chosen = self.selected_paths()
        if not chosen and self._typed:
            chosen = list(self.selectedFiles())
        chosen = [path for path in chosen if os.path.exists(path)]
        if not chosen:
            return
        self._chosen = chosen
        QDialog.accept(self)

    def chosen_paths(self) -> list[str]:
        return list(self._chosen)


def ask_paths(parent, state: PickerState | None = None) -> list[str]:
    """Показать диалог; вернуть выбранное, обновив состояние на месте.

    Состояние правится и при отказе: вид, размер окна и галочки переключают
    осознанно, и терять это из-за нажатой «Отмены» незачем.
    """
    dialog = PathPicker(parent, state)
    accepted = dialog.exec() == QDialog.Accepted
    dialog.store_state()
    return dialog.chosen_paths() if accepted else []
