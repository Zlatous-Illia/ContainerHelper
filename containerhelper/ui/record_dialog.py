"""Adding and editing a measurement record."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

PayloadProvider = Callable[[], "Payload | None"]

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..formatting import DASH, fmt_both, fmt_bytes, parse_bytes
from ..i18n import tr, tr_n
from ..model import DEFAULT_CLUSTER_BYTES, VC_HEADERS_BYTES, Payload

#: Input limits. Bytes — up to a petabyte with digit-group separators;
#: MiB — up to a terabyte container; cluster — up to 65536; files — up to
#: a hundred million, beyond that the folder scan runs into time, not into
#: the field.
MAX_BYTES_CHARS = 24
MAX_MIB_CHARS = 10
MAX_CLUSTER_CHARS = 7
MAX_COUNT_CHARS = 11
#: The name and the note are free text, but not endless: the name appears in
#: tables and in chart labels, and the note goes whole into the row tooltip.
MAX_ID_CHARS = 80
MAX_NOTE_CHARS = 400
from ..sizes import scan_volume
from ..records import Record, collection_note, generated_name, validate
from .calc_tab import CLUSTER_CHOICES
from .language import repeated_change
from .measure_dialog import MeasureDialog
from .table import digits_only, plain_text


class RecordDialog(QDialog):
    """Edits only the measured fields.

    The VeraCrypt header, the NTFS metadata, the used space and the copy slack
    are shown alongside but not edited: they are derived from what was entered
    and do not go into the file.
    """

    def __init__(
        self,
        record: Record | None = None,
        parent=None,
        payload_provider: PayloadProvider | None = None,
        container_provider: Callable[[], "int | None"] | None = None,
        safety_provider: Callable[[], "int | None"] | None = None,
        calibration: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setMinimumWidth(560)
        self._record = record or Record(id="", container_mib=1024)
        #: Where to get the size and the file count. The application does not
        #: copy files and cannot learn them on its own: the only one that
        #: already knows them is the Calculation tab, where that same data set
        #: was chosen.
        self._payload_provider = payload_provider
        self._container_provider = container_provider
        #: How much safety margin the prediction included. Also from the
        #: Calculation tab: that is where it is chosen, and the record keeps it
        #: to analyse the miss.
        self._safety_provider = safety_provider
        #: Simplified mode for a calibration point: nothing is put into the
        #: container, so the data and left-space fields only get in the way.
        self._calibration = calibration
        #: Form row captions and their keys, set by `retranslate`.
        self._row_labels: list[tuple[QLabel, str]] = []
        #: Numeric fields: they share one placeholder.
        self._number_edits: list[QLineEdit] = []
        #: What a button said above the issues (the check of the copied data
        #: and the like), as a function of the issues text. Kept to be said
        #: again in another language: the check of the volume is not repeated,
        #: and a lost mismatch warning would let a spoilt record be saved.
        #: Any edit of a field clears it, as it always cleared the label.
        self._notice: Callable[[str], str] | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_fields())
        layout.addWidget(self._build_derived())
        layout.addWidget(self._build_issues())

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel, parent=self)
        # Our own button in the accept role, not the standard Save: the box
        # resets a standard button's caption on a language change, after our
        # `retranslate` has run, and «Сохранить с пометкой» would turn into a
        # plain Save.
        self.save_button = buttons.addButton("", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._filesystem = self._record.filesystem
        self._load(self._record)
        self.retranslate()

    def retranslate(self) -> None:
        """Set every static text in the current language and rebuild the
        computed ones; what was typed into the fields stays as it is."""
        self.setWindowTitle(tr("record.title"))
        self._fields_group.setTitle(tr("record.group.fields"))
        self._derived_group.setTitle(tr("record.group.derived"))
        for label, key in self._row_labels:
            label.setText(tr(key))
        for edit in self._number_edits:
            edit.setPlaceholderText(tr("record.unset"))
        for button, key in (
            (self.payload_button, "record.payload"),
            (self.measure_button, "record.measure"),
            (self.left_button, "record.left"),
        ):
            button.setText(tr(key))
        for widget, key in (
            (self.id_edit, "record.id.tip"),
            (self.payload_button, "record.payload.tip"),
            (self.measure_button, "record.measure.tip"),
            (self.left_button, "record.left.tip"),
            (self.container_edit, "record.container.tip"),
            (self.cluster_combo, "record.cluster.tip"),
            (self.mounted_edit, "record.capacity.tip"),
            (self.free_edit, "record.empty_free.tip"),
            (self.file_edit, "record.data.tip"),
            (self.count_edit, "record.files.tip"),
            (self.alloc_edit, "record.alloc.tip"),
            (self.left_edit, "record.left_space.tip"),
            (self.predicted_edit, "record.predicted.tip"),
            (self.predicted_safety_edit, "record.predicted_safety.tip"),
            (self.note_edit, "record.note.tip"),
            (self.minimum_label, "record.minimum.tip"),
            (self.miss_label, "record.miss.tip"),
        ):
            widget.setToolTip(tr(key))
        notice = self._notice
        self._refresh()
        if notice is not None:
            self._show_notice(notice)
        # The minimum width comes from the widest row of buttons, not from a
        # round number. QPushButton agrees to become four times narrower than
        # its caption, and at 560 pixels the Take from Calculation button
        # («Взять с „Расчёта“») showed «Взять с…»: it clips silently, while
        # the window looks intact. Measured again after every change of
        # language: the captions change their width.
        self.setMinimumWidth(max(560, self._actions_width()))

    def event(self, event) -> bool:
        if repeated_change(self, event):
            return True
        return super().event(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _row(self, form: QFormLayout, key: str, field) -> None:
        """A form row whose caption `retranslate` sets by the key."""
        label = QLabel()
        form.addRow(label, field)
        self._row_labels.append((label, key))

    def _actions_width(self) -> int:
        """How much the row of buttons needs, with the window margins."""
        margins = self.layout().contentsMargins()
        return (
            self._actions.sizeHint().width() + margins.left() + margins.right() + 24
        )

    # --- building ----------------------------------------------------------

    def _build_fields(self) -> QGroupBox:
        group = self._fields_group = QGroupBox()
        form = QFormLayout(group)

        self.id_edit = QLineEdit()
        self._row(form, "record.id", self.id_edit)

        # Three actions in a row, right under the name: take what was
        # calculated, measure the empty volume, measure the left space after
        # copying.
        actions = QHBoxLayout()
        self.payload_button = QPushButton()
        self.payload_button.clicked.connect(self._take_payload)
        actions.addWidget(self.payload_button)

        self.measure_button = QPushButton()
        self.measure_button.clicked.connect(self._measure_empty)
        actions.addWidget(self.measure_button)

        self.left_button = QPushButton()
        self.left_button.clicked.connect(self._measure_left)
        actions.addWidget(self.left_button)
        actions.addStretch(1)
        form.addRow("", actions)
        self._actions = actions

        self.container_edit = QLineEdit()
        self._row(form, "record.container", self.container_edit)

        self.cluster_combo = QComboBox()
        self.cluster_combo.setEditable(True)
        for value in CLUSTER_CHOICES:
            self.cluster_combo.addItem(str(value), value)
        self._row(form, "record.cluster", self.cluster_combo)

        self.mounted_edit = self._number_field(form, "record.capacity")
        self.free_edit = self._number_field(form, "record.empty_free")
        self.file_edit = self._number_field(form, "record.data")
        self.count_edit = self._number_field(form, "record.files")
        self.alloc_edit = self._number_field(form, "record.alloc")
        self.left_edit = self._number_field(form, "record.left_space")
        self.predicted_edit = self._number_field(form, "record.predicted")
        self.predicted_safety_edit = self._number_field(
            form, "record.predicted_safety"
        )

        # Length limits: without them a field takes a number for which no
        # storage device exists, and the error surfaces only in the
        # plausibility check.
        self.container_edit.setMaxLength(MAX_MIB_CHARS)
        self.cluster_combo.lineEdit().setMaxLength(MAX_CLUSTER_CHARS)
        for edit in (self.mounted_edit, self.free_edit, self.file_edit,
                     self.alloc_edit, self.left_edit):
            edit.setMaxLength(MAX_BYTES_CHARS)
        self.count_edit.setMaxLength(MAX_COUNT_CHARS)
        for edit in (self.predicted_edit, self.predicted_safety_edit):
            edit.setMaxLength(MAX_MIB_CHARS)

        # All numeric fields accept only digits and digit-group separators. A
        # letter in a bytes field is a slip of the finger, not "a value that
        # failed to parse": parse_bytes used to return None silently, the
        # field kept the garbage, and the computed values turned into dashes.
        digits_only(self.cluster_combo)
        for edit in (
            self.container_edit,
            self.mounted_edit,
            self.free_edit,
            self.file_edit,
            self.count_edit,
            self.alloc_edit,
            self.left_edit,
            self.predicted_edit,
            self.predicted_safety_edit,
        ):
            digits_only(edit)
        plain_text(self.id_edit, MAX_ID_CHARS)

        if self._calibration:
            self._hide_payload_rows(form)

        self.note_edit = QLineEdit()
        plain_text(self.note_edit, MAX_NOTE_CHARS)
        self._row(form, "record.note", self.note_edit)

        return group

    def _hide_payload_rows(self, form: QFormLayout) -> None:
        """Keep only what an empty-volume measurement needs."""
        for widget in (
            self.payload_button,
            self.left_button,
            self.file_edit,
            self.count_edit,
            self.alloc_edit,
            self.left_edit,
            self.predicted_edit,
            self.predicted_safety_edit,
        ):
            widget.setVisible(False)
            label = form.labelForField(widget)
            if label is not None:
                label.setVisible(False)

    def _number_field(self, form: QFormLayout, label: str) -> QLineEdit:
        edit = QLineEdit()
        edit.textEdited.connect(self._refresh)
        self._row(form, label, edit)
        self._number_edits.append(edit)
        return edit

    def _build_derived(self) -> QGroupBox:
        group = self._derived_group = QGroupBox()
        form = QFormLayout(group)
        self.header_label = QLabel()
        self.ntfs_label = QLabel()
        self.consumed_label = QLabel()
        self.slack_label = QLabel()
        self.minimum_label = QLabel()
        self.miss_label = QLabel()
        self.miss_label.setWordWrap(True)
        self._row(form, "record.headers", self.header_label)
        self._row(form, "record.metadata", self.ntfs_label)
        self._row(form, "record.consumed", self.consumed_label)
        self._row(form, "record.slack", self.slack_label)
        self._row(form, "record.minimum", self.minimum_label)
        self._row(form, "record.miss", self.miss_label)
        return group

    def _build_issues(self) -> QLabel:
        self.issues_label = QLabel()
        self.issues_label.setWordWrap(True)
        self.issues_label.setTextFormat(Qt.RichText)
        return self.issues_label

    # --- data --------------------------------------------------------------

    def _load(self, record: Record) -> None:
        self.id_edit.setText(record.id)
        self.container_edit.setText(str(record.container_mib))
        self.cluster_combo.setCurrentText(str(record.cluster_bytes))
        self.mounted_edit.setText(fmt_bytes(record.mounted_bytes) if record.mounted_bytes else "")
        self.free_edit.setText(
            fmt_bytes(record.empty_free_bytes) if record.empty_free_bytes else ""
        )
        self.file_edit.setText(fmt_bytes(record.file_bytes) if record.file_bytes else "")
        self.count_edit.setText(str(record.file_count) if record.file_count else "")
        self.alloc_edit.setText(
            fmt_bytes(record.file_alloc_bytes) if record.file_alloc_bytes else ""
        )
        self.left_edit.setText(fmt_bytes(record.left_bytes) if record.left_bytes else "")
        self.predicted_edit.setText(
            fmt_bytes(record.predicted_mib) if record.predicted_mib else ""
        )
        self.predicted_safety_edit.setText(
            fmt_bytes(record.predicted_safety_mib)
            if record.predicted_safety_mib is not None
            else ""
        )
        self.note_edit.setText(record.note)

        for widget in (self.container_edit, self.id_edit):
            widget.textEdited.connect(self._refresh)
        self.cluster_combo.currentTextChanged.connect(self._refresh)

    def build_record(self) -> Record:
        cluster = parse_bytes(self.cluster_combo.currentText()) or DEFAULT_CLUSTER_BYTES
        return replace(
            self._record,
            id=self.id_edit.text().strip(),
            container_mib=parse_bytes(self.container_edit.text()) or 0,
            cluster_bytes=cluster,
            mounted_bytes=parse_bytes(self.mounted_edit.text()),
            empty_free_bytes=parse_bytes(self.free_edit.text()),
            file_bytes=parse_bytes(self.file_edit.text()),
            file_count=parse_bytes(self.count_edit.text()),
            file_alloc_bytes=parse_bytes(self.alloc_edit.text()),
            left_bytes=parse_bytes(self.left_edit.text()),
            filesystem=self._filesystem,
            note=self.note_edit.text().strip(),
            predicted_mib=parse_bytes(self.predicted_edit.text()),
            predicted_safety_mib=parse_bytes(self.predicted_safety_edit.text()),
        )

    def _measure_empty(self) -> None:
        """Volume capacity and empty free space: a point for the NTFS model."""
        result = self._ask_volume("record.prompt.empty")
        if result is None:
            return
        drive, total, free = result
        self.mounted_edit.setText(fmt_bytes(total))
        self.free_edit.setText(fmt_bytes(free))
        self._apply_volume_facts()
        self._refresh()

    def _measure_left(self) -> None:
        """Left space after copying, plus a check that the right data is there.

        A measurement without the check is meaningless: the left space is tied
        to the data size and the file count taken from the Calculation tab.
        Copy the wrong thing, and the copy slack comes out as garbage and
        silently spoils the calibration, with nothing to catch it afterwards.
        """
        result = self._ask_volume("record.prompt.left")
        if result is None:
            return
        drive, _total, free = result
        self._apply_volume_facts()

        cluster = parse_bytes(self.cluster_combo.currentText()) or DEFAULT_CLUSTER_BYTES
        scan = scan_volume(drive, cluster)
        if scan.empty:
            self._show_notice(
                lambda _issues: f"<i>{tr('record.left.no_files', drive=drive)}</i>"
            )
            return

        self.left_edit.setText(fmt_bytes(free))
        self._refresh()
        self._show_notice(
            lambda issues: self._verdict(drive, scan) + "<br>" + issues
        )

    def _show_notice(self, notice: Callable[[str], str]) -> None:
        """Put a button's message in place of the issues text."""
        self._notice = notice
        self.issues_label.setText(notice(self.issues_label.text()))

    def _ask_volume(self, prompt: str) -> tuple[str, int, int] | None:
        """Ask for a mounted volume; `prompt` is the key of the wording."""
        dialog = MeasureDialog(self, prompt_key=prompt)
        # Modal to its own window, not to the whole program: several record
        # windows can now be open, and choosing a drive letter in one of them
        # must not lock the others together with the Calculation tab.
        dialog.setWindowModality(Qt.WindowModal)
        accepted = dialog.exec() == QDialog.Accepted
        # Deleted once read: a hidden child would still hear every language
        # switch and read a volume that may be long unmounted.
        dialog.deleteLater()
        if not accepted:
            return None
        values = dialog.result_values()
        drive = dialog.selected_drive()
        if values is None or not drive:
            return None
        self._volume_facts = dialog.volume_facts()
        return drive, values[0], values[1]

    def _apply_volume_facts(self) -> None:
        """Fill in the filesystem and cluster size read from the volume.

        There is no reason to ask the user for them: the volume exists, and its
        properties are read exactly. A typo in the cluster size would distort
        the copy slack, and nothing would catch it.
        """
        filesystem, cluster = getattr(self, "_volume_facts", ("", None))
        if filesystem:
            self._filesystem = filesystem
        if cluster:
            self.cluster_combo.setCurrentText(str(cluster))

    def _verdict(self, drive: str, scan) -> str:
        """A check of the totals: the file count and the cluster-rounded size.

        Deliberately not compared file by file: copying a folder's contents
        instead of the folder itself, and renaming along the way, are normal,
        and path mismatches would raise a false alarm on every other
        measurement.
        """
        expected_count = parse_bytes(self.count_edit.text())
        expected_alloc = parse_bytes(self.alloc_edit.text())
        lines: list[str] = []

        if scan.service_dirs:
            lines.append(
                tr("record.verdict.service_dirs", dirs=", ".join(scan.service_dirs))
            )

        if expected_count is None and expected_alloc is None:
            lines.append(
                tr(
                    "record.verdict.unchecked",
                    drive=drive,
                    count=scan.payload.file_count,
                    alloc=fmt_bytes(scan.payload.alloc_bytes),
                )
            )
            return "<br>".join(lines)

        mismatch = []
        if expected_count is not None and expected_count != scan.payload.file_count:
            mismatch.append(
                tr(
                    "record.verdict.count_mismatch",
                    found=scan.payload.file_count,
                    expected=expected_count,
                )
            )
        if expected_alloc is not None and expected_alloc != scan.payload.alloc_bytes:
            mismatch.append(
                tr(
                    "record.verdict.alloc_mismatch",
                    found=fmt_bytes(scan.payload.alloc_bytes),
                    expected=fmt_bytes(expected_alloc),
                )
            )

        if mismatch:
            lines.append(tr("record.verdict.mismatch", details="; ".join(mismatch)))
        else:
            lines.append(
                tr_n(
                    "record.verdict.match",
                    scan.payload.file_count,
                    drive=drive,
                    alloc=fmt_bytes(scan.payload.alloc_bytes),
                )
            )
        if scan.errors:
            lines.append(tr_n("record.verdict.unread", len(scan.errors)))
        return "<br>".join(lines)

    def _take_payload(self) -> None:
        """Bring the data over from the Calculation tab.

        Both Container init and the cluster size come along. Container init,
        because that is exactly what gets created in VeraCrypt from this
        calculation, and retyping it by hand means inviting a typo. The
        cluster size, because computing the size with one cluster and the
        slack with another makes no sense.
        """
        payload = self._payload_provider() if self._payload_provider else None
        if payload is None:
            self._show_notice(lambda _issues: f"<i>{tr('record.payload.none')}</i>")
            return
        container = self._container_provider() if self._container_provider else None
        if container:
            self.container_edit.setText(str(container))
            # The prediction is recorded in the same move as the size itself:
            # otherwise it would have to be typed in by hand after the fact,
            # and by then the model is already different, and there would be
            # nothing to check.
            self.predicted_edit.setText(str(container))
        safety = self._safety_provider() if self._safety_provider else None
        if safety is not None:
            self.predicted_safety_edit.setText(str(safety))
        self.cluster_combo.setCurrentText(str(payload.cluster_bytes))
        self.file_edit.setText(fmt_bytes(payload.logical_bytes))
        self.count_edit.setText(str(payload.file_count))
        self.alloc_edit.setText(fmt_bytes(payload.alloc_bytes))
        self._refresh()

    def _refresh(self) -> None:
        record = self.build_record()
        self.payload_button.setEnabled(self._payload_provider is not None)
        # The headers are a constant, and the row is shown only once there
        # is a capacity to compare them with: whether it matches is said by
        # the tail check among the issues below.
        self.header_label.setText(
            fmt_both(VC_HEADERS_BYTES if record.mounted_bytes is not None else None)
        )
        self.ntfs_label.setText(fmt_both(record.metadata_bytes))
        self.consumed_label.setText(fmt_both(record.consumed_bytes))
        self.slack_label.setText(fmt_both(record.copy_slack_measured))
        self._refresh_forecast(record)
        # A collected record goes by a name built from its data, and so does a
        # new calibration point: the field stays empty and shows that name,
        # which follows the size and the language. Typed in, a name is kept.
        # Only such a record: a blank ordinary one is an empty volume too, and
        # would go nameless into the records as "Calibration 1 GiB".
        generated = (
            generated_name(record)
            if self._calibration or record.fileset or record.veracrypt
            else ""
        )
        self.id_edit.setPlaceholderText(generated or tr("record.id.placeholder"))
        self.note_edit.setPlaceholderText(collection_note(record.veracrypt))
        named = bool(record.id or generated)

        issues = validate(record)
        if not named:
            self.issues_label.setText(f"<i>{tr('record.no_name')}</i>")
        elif issues:
            body = "<br>".join(f"⚠ {issue.message}" for issue in issues)
            self.issues_label.setText(body)
        else:
            self.issues_label.setText("")
        self._notice = None

        self.save_button.setEnabled(named)
        self.save_button.setText(
            tr("record.save.flagged") if issues else tr("record.save")
        )

    def _refresh_forecast(self, record: Record) -> None:
        """Show whether the prediction held — in words, not with a bare number.

        The sign of the miss decides everything, and "−3" among the numbers is
        not read at once. So a verdict stands next to it: would it have fit or
        not.
        """
        minimum = record.minimum_mib
        self.minimum_label.setText(
            f"{fmt_bytes(minimum)} MiB" if minimum is not None else DASH
        )

        miss = record.miss_mib
        if miss is None:
            self.miss_label.setText(tr("record.miss.none"))
            return
        key = (
            "record.miss.short"
            if miss < 0
            else ("record.miss.exact" if miss == 0 else "record.miss.over")
        )
        self.miss_label.setText(
            tr(key, miss=f"{miss:+d}", model=f"{record.model_miss_mib:+d}")
        )

    def _on_save(self) -> None:
        """A flawed record is saved but flagged, and calibrates no model."""
        record = self.build_record()
        self._record = replace(record, flagged=bool(validate(record)))
        self.accept()

    def result_record(self) -> Record:
        return self._record
