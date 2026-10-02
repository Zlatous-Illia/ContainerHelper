"""One dialog that picks both files and folders at once."""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field

from PySide6.QtCore import (
    QByteArray,
    QDir,
    QEvent,
    QItemSelection,
    QItemSelectionModel,
)
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

from ..i18n import tr

#: The views that hold files and folders: the plain list and the detail view.
#: By name, not by going through every QAbstractItemView: the sidebar is a
#: view too, but it needs drag and drop — that is how bookmarks are put there.
FILE_VIEWS = ("listView", "treeView")

#: An empty folder means "My Computer", the list of drives. Checked: a
#: QFileDialog with `setDirectory("")` shows exactly the drives, not the
#: process's current directory. The program's own folder does not work as the
#: starting one at all: the data lives anywhere but next to it.
COMPUTER = ""


@dataclass
class PickerState:
    """Everything the dialog has to survive between showings.

    Lives outside, because the dialog itself lives for one showing: a setting
    left inside it would not survive even Cancel. The main window keeps it and
    writes it to settings.ini.

    `directory` is the folder **the dialog was showing**, not the one chosen in
    it. The difference is not cosmetic: having chosen folder 2 inside folder 1,
    next time the dialog must open in folder 1 again — next to folder 2 lies
    what gets chosen next. A starting folder taken from the chosen path went
    one level deeper on every showing.
    """

    show_hidden: bool = False
    #: Open where it was closed. Off means always "My Computer".
    remember_dir: bool = True
    directory: str = COMPUTER
    #: The window size as two numbers, not `saveGeometry`.
    #:
    #: `restoreGeometry` silently refuses to work: it compares the width of the
    #: screen the geometry was saved on with the current one, and if they
    #: differ by more than a quarter it **returns false having done nothing**.
    #: Then QDialog sees that nobody has set a size and fits the window to its
    #: contents — from outside this looks exactly like "the size resets on
    #: every launch". Two numbers go through no such checks.
    width: int = 0
    height: int = 0
    #: The list view mode, the column widths of the detail view and the
    #: sidebar — QFileDialog itself can hand all of this over in one piece.
    layout: QByteArray = field(default_factory=QByteArray)

    def start_directory(self) -> str:
        return self.directory if self.remember_dir else COMPUTER

    @property
    def sized(self) -> bool:
        return self.width > 0 and self.height > 0


class PathPicker(QFileDialog):
    """Picking files and folders in one list.

    The native Windows dialogs can do either files or a single folder: under
    the hood these are two different system calls, and they offer no third.
    So Qt's own dialog is used — it has one list that shows both — and exactly
    one thing is changed in it: Select on a folder does not go inside but
    returns the folder as a source. A double-click still goes inside.
    """

    def __init__(self, parent=None, state: PickerState | None = None) -> None:
        self._state = state or PickerState()
        super().__init__(parent, "", self._state.start_directory())
        # The stock ExistingFiles mode: it shows folders too (otherwise there
        # is no walking through them), and multiple selection is already set
        # up in it.
        self.setOption(QFileDialog.DontUseNativeDialog, True)
        self.setFileMode(QFileDialog.ExistingFiles)
        self._chosen: list[str] = []

        self._views: list[QAbstractItemView] = []
        for name in FILE_VIEWS:
            view = self.findChild(QAbstractItemView, name)
            if view is None:
                continue
            # In ExistingFiles mode Qt gives multiple selection only to the
            # list; in the detail view items would have to be selected one at
            # a time.
            view.setSelectionMode(QAbstractItemView.ExtendedSelection)
            # Qt sets InternalMove on the file dialog's views, and a mouse drag
            # starts drag and drop instead of a rubber band: drawing a frame
            # around several names is impossible, only Ctrl and Shift remain.
            view.setDragEnabled(False)
            view.setDragDropMode(QAbstractItemView.NoDragDrop)
            view.selectionModel().selectionChanged.connect(self._sync_name)
            # A reloaded folder clears the selection but sends no
            # selection-changed signal: the line would be left with names from
            # the previous contents.
            model = view.model()
            model.modelReset.connect(self._sync_name)
            loaded = getattr(model, "directoryLoaded", None)
            if loaded is not None:
                loaded.connect(self._sync_name)
            self._views.append(view)

        #: The "Selected:" line. QFileDialog itself owns it, and answers
        #: `selectedFiles()` from it.
        self._name_edit = self.findChild(QLineEdit, "fileNameEdit")
        self._accept_button = self._find_accept_button()
        #: A name was typed into the line by hand. Then and only then does the
        #: line mean anything by itself: in every other case we write it from
        #: the selection, and it cannot be trusted — the folder may have been
        #: reloaded while the names stayed in the line. That is exactly how
        #: Select added files that nobody had selected.
        self._typed = False
        if self._name_edit is not None:
            self._name_edit.textEdited.connect(self._on_typed)

        self._hidden_action = self._find_hidden_action()
        #: Our selection buttons with their title and tooltip keys.
        self._buttons: list[tuple[QPushButton, str, str]] = []
        self._hint: QLabel | None = None
        self._add_controls()
        self._add_hint()
        self.retranslate()
        self._restore_state()

    # --- language ----------------------------------------------------------

    def retranslate(self) -> None:
        """Set our own text; the dialog's stock widgets are Qt's business."""
        self.setWindowTitle(tr("picker.title"))
        self.setLabelText(QFileDialog.Accept, tr("picker.accept"))
        self.setLabelText(QFileDialog.Reject, tr("picker.reject"))
        self.setLabelText(QFileDialog.FileName, tr("picker.selected"))
        for button, title, tip in self._buttons:
            button.setText(tr(title))
            button.setToolTip(tr(tip))
        self.remember_check.setText(tr("picker.remember"))
        self.remember_check.setToolTip(tr("picker.remember.tip"))
        self.hidden_check.setText(tr("picker.hidden"))
        self.hidden_check.setToolTip(tr("picker.hidden.tip"))
        if self._hint is not None:
            self._hint.setText(tr("picker.hint"))

    def event(self, event) -> bool:
        """Retranslate after Qt, not in `changeEvent`.

        On this event the dialog's button box puts back the stock texts of its
        buttons, and it gets the event after the dialog's `changeEvent`:
        retranslated there, Select came back as Open. `QWidget.event` hands
        the event to the children before it returns.
        """
        handled = super().event(event)
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        return handled

    # --- memory between showings -------------------------------------------

    def current_directory(self) -> str:
        """The folder the dialog is showing now. Empty means "My Computer".

        Asked of the list itself, not of `directory()`: on "My Computer" that
        returns not the list of drives but the process's working directory, and
        remembering it would mean remembering someone else's place.
        `directoryEntered` does not work either — a folder change made by the
        program does not raise it at all.
        """
        view = self._active_view()
        if view is None:
            return self.directory().absolutePath()
        return view.model().filePath(view.rootIndex())

    def _find_accept_button(self):
        """The Select button. Needed because enabling it is now our job.

        The dialog enables it when the "Selected" line is edited, but our line
        stays silent while we edit the selection — otherwise the dialog answers
        every edit with autocompletion and resets the selection.
        """
        box = self.findChild(QDialogButtonBox)
        if box is None:
            return None
        for button in box.buttons():
            if box.buttonRole(button) == QDialogButtonBox.AcceptRole:
                return button
        return None

    def showEvent(self, event) -> None:  # noqa: N802 — Qt's name
        """Restore the size of the last showing — after the window is open.

        Here, not in the constructor: on show QDialog fits the size to its
        contents by itself if it thinks nobody has set one.
        """
        super().showEvent(event)
        if self._state.sized and not self._sized:
            self._sized = True
            self.resize(self._state.width, self._state.height)

    def _restore_state(self) -> None:
        """Restore the view of the last showing; the size comes on show.

        Order matters: restoreState sets the list view mode and the column
        widths, and it can change the window size as well. So the size is
        applied after it — by `resize()` in `showEvent`, from two numbers (see
        `PickerState`) — otherwise the window drifts back to what it was when
        the view was saved.
        """
        if not self._state.layout.isEmpty():
            self.restoreState(self._state.layout)
            # restoreState also brings back the folder it was saved in. That is
            # no use to us: where to open is decided by the Remember folder
            # check box.
            self.setDirectory(self._state.start_directory())
        #: The size is set once, on the first showing.
        self._sized = False
        self.set_show_hidden(self._state.show_hidden)
        self.hidden_check.setChecked(self.show_hidden)

    def store_state(self) -> PickerState:
        """Put the state back. Called on Cancel too.

        The view, the window size and both check boxes are changed on purpose,
        and there is no reason to lose that because Cancel was pressed.
        """
        state = self._state
        state.show_hidden = self.show_hidden
        state.remember_dir = self.remember_check.isChecked()
        state.directory = self.current_directory()
        state.width, state.height = self.width(), self.height()
        state.layout = self.saveState()
        return state

    # --- hidden files ------------------------------------------------------

    def _find_hidden_action(self) -> QAction | None:
        """The dialog's own "Show hidden files" item.

        It is the only one of its kind: among the actions whose parent is the
        dialog itself, only it carries a check mark, while "Show Size" and its
        neighbours belong to the detail view's header. It cannot be found by
        its text — the text gets translated.
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
        """Toggle showing hidden files through the dialog's own item.

        Not through our own setFilter: the dialog has the same toggle in the
        list's context menu, and two independent paths would drift apart — the
        check box would say one thing and the list would show another.
        """
        if self.show_hidden == show:
            return
        if self._hidden_action is not None:
            self._hidden_action.trigger()
            return
        # The item was not found, so Qt has rearranged it. What remains is the
        # filter directly: not tied to the menu, but working.
        if show:
            self.setFilter(self.filter() | QDir.Hidden)
        else:
            self.setFilter(self.filter() & ~QDir.Hidden)

    def _sync_hidden_check(self, *_args) -> None:
        """Bring the check box in line with what is actually in the filter."""
        self.hidden_check.setChecked(self.show_hidden)

    # --- selection ---------------------------------------------------------

    def selected_paths(self) -> list[str]:
        """What is selected in the list right now.

        Asked of the list itself, not of `selectedFiles()`: that one answers
        from the "Selected" line, and the line lives a life of its own. After
        the selection was cleared, the old names stayed in it — and Select
        added files that nobody on screen had selected any more. Inverting from
        nothing and back did the same: everything selected, everything cleared,
        and the line remembers everything.
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
        """The "Selected" line stays silent while we edit the selection.

        The dialog answers every edit of this line with autocompletion: it
        selects in the list what is written in the line and deselects what is
        not. Of the names it writes there itself, it writes only files, so
        Select all immediately lost every folder, and a second inversion in a
        row already worked on a different set from the one shown. While our
        edit is in progress, the line emits no signals, and nobody rearranges
        anything.
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
        """Rewrite the "Selected" line with what is actually selected.

        By our own hand and in full — folders included. The dialog puts only
        files there: it treats a folder not as a choice but as a way further
        in, and does not show it in the line at all.
        """
        if self._name_edit is None:
            return
        names = [os.path.basename(path) for path in self.selected_paths()]
        if len(names) == 1:
            text = names[0]
        else:
            # Quotes are QFileDialog's own convention for several names.
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
        """Select is enabled while there is something to select."""
        if self._accept_button is None:
            return
        typed = self._name_edit.text().strip() if self._name_edit else ""
        self._accept_button.setEnabled(bool(self.selected_paths()) or bool(typed))

    def _active_view(self) -> QAbstractItemView | None:
        """The view being looked at now: the plain list or the detail view.

        There are two, and only one is visible at a time. Before the dialog is
        shown, neither is visible — then the list is taken: that is what the
        dialog opens with.
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
        """Invert the selection: deselect what is selected, select the rest.

        A Toggle over the folder's whole rectangle, not a walk over the rows
        one at a time: the selection changes in one signal, and the list is not
        repainted as many times as there are files in the folder.
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

    # --- wiring ------------------------------------------------------------

    def _add_controls(self) -> None:
        """Selection buttons and check boxes — their own row under the list."""
        layout = self.layout()
        if not isinstance(layout, QGridLayout):
            return

        row = QHBoxLayout()
        for title, tip, slot in (
            ("picker.select_all", "picker.select_all.tip", self._select_all),
            ("picker.select_none", "picker.select_none.tip", self._select_none),
            ("picker.invert", "picker.invert.tip", self._invert_selection),
        ):
            button = QPushButton()
            button.clicked.connect(slot)
            row.addWidget(button)
            self._buttons.append((button, title, tip))
        row.addStretch(1)

        self.remember_check = QCheckBox()
        self.remember_check.setChecked(self._state.remember_dir)
        row.addWidget(self.remember_check)

        self.hidden_check = QCheckBox()
        self.hidden_check.toggled.connect(self.set_show_hidden)
        if self._hidden_action is not None:
            # The same toggle is in the list's context menu. Without this link
            # the check box would lie after it until the dialog is closed.
            #
            # triggered, not toggled: toggled arrives before the dialog edits
            # the filter, the check box manages to call set_show_hidden, which
            # sees the old state and toggles the item a second time — the
            # click in the menu does nothing.
            self._hidden_action.triggered.connect(self._sync_hidden_check)
        row.addWidget(self.hidden_check)

        layout.addLayout(row, layout.rowCount(), 0, 1, max(layout.columnCount(), 1))

    def _add_hint(self) -> None:
        """Hint below: without it, Select on a folder looks like a mistake.

        Only as a label, with no tooltip on the dialog itself: every child
        without a tooltip of its own inherits that one, and hovering over any
        file in the list showed the same text that is written at the bottom
        anyway.
        """
        layout = self.layout()
        if not isinstance(layout, QGridLayout):
            return
        hint = self._hint = QLabel()
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(hint, layout.rowCount(), 0, 1, max(layout.columnCount(), 1))

    def accept(self) -> None:
        """Take what is selected without entering the folder.

        The base accept on a folder goes inside it, and then there is nothing
        left to choose a folder with by button. An empty selection leaves the
        dialog open: closing it with nothing is the same as Cancel, only less
        clear.
        """
        # The list's selection outranks the line: the line also carries a name
        # typed by hand, but while something is selected in the list, that is
        # what the choice is about. An empty line with an empty selection is no
        # good at all: `selectedFiles()` then returns the folder itself, and
        # Select would silently take all of it.
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
    """Show the dialog; return the choice, updating the state in place.

    The state is updated on refusal too: the view, the window size and the
    check boxes are changed on purpose, and there is no reason to lose that
    because Cancel was pressed.
    """
    dialog = PathPicker(parent, state)
    accepted = dialog.exec() == QDialog.Accepted
    dialog.store_state()
    return dialog.chosen_paths() if accepted else []
