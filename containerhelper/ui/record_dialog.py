"""Adding and editing a measurement record."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

PayloadProvider = Callable[[], "Payload | None"]

from PySide6.QtCore import Qt
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
from ..model import DEFAULT_CLUSTER_BYTES, Payload

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
from ..records import Record, validate
from .calc_tab import CLUSTER_CHOICES
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
        self.setWindowTitle("Запись измерений")
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

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_fields())
        layout.addWidget(self._build_derived())
        layout.addWidget(self._build_issues())

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        layout.addWidget(buttons)

        self._filesystem = self._record.filesystem
        self._load(self._record)
        self._refresh()
        # The minimum width comes from the widest row of buttons, not from a
        # round number. QPushButton agrees to become four times narrower than
        # its caption, and at 560 pixels the Take from Calculation button
        # («Взять с „Расчёта“») showed «Взять с…»: it clips silently, while
        # the window looks intact.
        self.setMinimumWidth(max(self.minimumWidth(), self._actions_width()))

    def _actions_width(self) -> int:
        """How much the row of buttons needs, with the window margins."""
        margins = self.layout().contentsMargins()
        return (
            self._actions.sizeHint().width() + margins.left() + margins.right() + 24
        )

    # --- building ----------------------------------------------------------

    def _build_fields(self) -> QGroupBox:
        group = QGroupBox("Измеренные величины")
        form = QFormLayout(group)

        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("например Cache 5")
        self.id_edit.setToolTip(
            "Как запись будет называться в таблицах. На расчёт не влияет, но "
            "без имени не сохранить — иначе её не найти в проверках."
        )
        form.addRow("Имя:", self.id_edit)

        # Three actions in a row, right under the name: take what was
        # calculated, measure the empty volume, measure the left space after
        # copying.
        actions = QHBoxLayout()
        self.payload_button = QPushButton("Взять с «Расчёта»")
        self.payload_button.setToolTip(
            "Перенести Container init, размер кластера, размер данных, число "
            "файлов и объём по кластерам — ровно те, по которым считали."
        )
        self.payload_button.clicked.connect(self._take_payload)
        actions.addWidget(self.payload_button)

        self.measure_button = QPushButton("Измерить том")
        self.measure_button.setToolTip(
            "Прочитать ёмкость и свободное место пустого смонтированного тома."
        )
        self.measure_button.clicked.connect(self._measure_empty)
        actions.addWidget(self.measure_button)

        self.left_button = QPushButton("Замерить остаток")
        self.left_button.setToolTip(
            "Снять свободное место после копирования и заодно проверить, что "
            "на томе лежит именно то, по чему считали контейнер."
        )
        self.left_button.clicked.connect(self._measure_left)
        actions.addWidget(self.left_button)
        actions.addStretch(1)
        form.addRow("", actions)
        self._actions = actions

        self.container_edit = QLineEdit()
        self.container_edit.setToolTip(
            "Container init — то, что задали в VeraCrypt при создании. Не "
            "размер файла на диске: файл больше тома на заголовок."
        )
        form.addRow("Container init, MiB:", self.container_edit)

        self.cluster_combo = QComboBox()
        self.cluster_combo.setEditable(True)
        for value in CLUSTER_CHOICES:
            self.cluster_combo.addItem(str(value), value)
        self.cluster_combo.setToolTip(
            "Шаг, которым файловая система выдаёт место. При замере читается "
            "с тома сам; руками нужен только для записи задним числом.\n"
            "Опечатка здесь тихо испортит запас на копирование."
        )
        form.addRow("Размер кластера, B:", self.cluster_combo)

        self.mounted_edit = self._number_field(form, "Ёмкость тома, B:")
        self.mounted_edit.setToolTip(
            "Сколько показывает смонтированный том. От него зависят "
            "метаданные NTFS, по нему же замер привязан к размеру.\n"
            "Снимается кнопкой «Измерить том»."
        )
        self.free_edit = self._number_field(form, "Свободно на пустом, B:")
        self.free_edit.setToolTip(
            "Свободное место сразу после форматирования, до копирования. "
            "Ёмкость минус это число и есть метаданные NTFS."
        )
        self.file_edit = self._number_field(form, "Размер данных, B:")
        self.file_edit.setToolTip(
            "Размер того, что скопировали, — столько же показывает "
            "Проводник.\n"
            "Пусто — значит, том был пустой; запас на копирование такая "
            "запись не калибрует."
        )
        self.count_edit = self._number_field(form, "Файлов:")
        self.count_edit.setToolTip(
            "Сколько файлов скопировали. По этому числу калибруется "
            "по-файловая часть запаса — но только если записей несколько и "
            "число файлов в них разное."
        )
        self.alloc_edit = self._number_field(form, "Данные по кластерам, B:")
        self.alloc_edit.setToolTip(
            "Сколько данные заняли с округлением каждого файла до кластера. "
            "Для одного файла можно не заполнять — посчитается само. Для "
            "папки так не выйдет, и без этого числа запас на копирование "
            "выйдет завышенным."
        )
        self.left_edit = self._number_field(form, "Остаток после копии, B:")
        self.left_edit.setToolTip(
            "Свободное место после копирования, оно же Left space в "
            "VeraCrypt. Разница со «свободно на пустом» — это и есть то, "
            "сколько данные заняли на самом деле.\n"
            "Снимается кнопкой «Замерить остаток»."
        )

        self.predicted_edit = self._number_field(form, "Обещано расчётом, MiB:")
        self.predicted_edit.setToolTip(
            "Container init, который посоветовала программа на эти данные. "
            "Заполняется кнопкой «Взять с „Расчёта“».\n"
            "Хранится, а не считается заново: обе модели меняются от каждого "
            "нового замера, и пересчёт задним числом ответил бы «что я скажу "
            "сегодня», а не «что я сказал тогда».\n"
            "Пусто — проверять прогноз не с чем."
        )
        self.predicted_safety_edit = self._number_field(
            form, "Из них страховка, MiB:"
        )
        self.predicted_safety_edit.setToolTip(
            "Сколько в обещанном было страховочного запаса. Без этого числа "
            "промах неоднозначен: +5 MiB одинаково выглядят и когда модель "
            "точна при страховке 5 MiB, и когда модель занизила на 3, а 8 MiB "
            "страховки это скрыли."
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
        self.note_edit.setToolTip(
            "Свободный текст для себя: чем заполняли, на какой машине, что "
            "показалось странным. В расчёте не участвует."
        )
        form.addRow("Заметка:", self.note_edit)

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
        edit.setPlaceholderText("не задано")
        edit.textEdited.connect(self._refresh)
        form.addRow(label, edit)
        return edit

    def _build_derived(self) -> QGroupBox:
        group = QGroupBox("Вычисляется автоматически, в файл не пишется")
        form = QFormLayout(group)
        self.header_label = QLabel()
        self.ntfs_label = QLabel()
        self.consumed_label = QLabel()
        self.slack_label = QLabel()
        self.minimum_label = QLabel()
        self.miss_label = QLabel()
        self.miss_label.setWordWrap(True)
        form.addRow("Заголовок VeraCrypt:", self.header_label)
        form.addRow("Метаданные NTFS:", self.ntfs_label)
        form.addRow("Занято при копировании:", self.consumed_label)
        form.addRow("Запас на копирование:", self.slack_label)

        self.minimum_label.setToolTip(
            "Наименьший контейнер, в который эти данные всё-таки влезли бы: "
            "занятое на томе плюс заголовок VeraCrypt, округлённое вверх до "
            "MiB.\n"
            "Оценка чуть завышена — контейнер поменьше дал бы и метаданных "
            "поменьше, — то есть промах выходит меньше настоящего. Ошибка в "
            "сторону тревоги, а не благодушия."
        )
        self.miss_label.setToolTip(
            "Обещано минус минимально достаточно. Минус означает, что данные "
            "не влезли бы, — ради этого случая проверка и делается."
        )
        form.addRow("Минимально достаточный контейнер:", self.minimum_label)
        form.addRow("Промах:", self.miss_label)
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
        result = self._ask_volume(
            "Смонтируйте пустой отформатированный контейнер и выберите его "
            "букву. Считываются ёмкость тома и свободное место на нём."
        )
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
        result = self._ask_volume(
            "Смонтируйте контейнер с уже скопированными данными и выберите "
            "его букву. Считывается остаток свободного места."
        )
        if result is None:
            return
        drive, _total, free = result
        self._apply_volume_facts()

        cluster = parse_bytes(self.cluster_combo.currentText()) or DEFAULT_CLUSTER_BYTES
        scan = scan_volume(drive, cluster)
        if scan.empty:
            self.issues_label.setText(
                f"<i>На томе {drive} нет файлов. Замер остатка снимают после "
                f"копирования — для пустого тома есть «Измерить том».</i>"
            )
            return

        self.left_edit.setText(fmt_bytes(free))
        self._refresh()
        self.issues_label.setText(
            self._verdict(drive, scan) + "<br>" + self.issues_label.text()
        )

    def _ask_volume(self, prompt: str) -> tuple[str, int, int] | None:
        dialog = MeasureDialog(self, prompt=prompt)
        # Modal to its own window, not to the whole program: several record
        # windows can now be open, and choosing a drive letter in one of them
        # must not lock the others together with the Calculation tab.
        dialog.setWindowModality(Qt.WindowModal)
        if dialog.exec() != QDialog.Accepted:
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
                "Служебные каталоги NTFS на томе: "
                + ", ".join(scan.service_dirs)
                + ". Место они занимают, в число файлов не входят."
            )

        if expected_count is None and expected_alloc is None:
            lines.append(
                f"На томе {drive}: файлов {scan.payload.file_count}, по кластерам "
                f"{fmt_bytes(scan.payload.alloc_bytes)} B. Сверять не с чем — "
                f"заполните размер данных и число файлов."
            )
            return "<br>".join(lines)

        mismatch = []
        if expected_count is not None and expected_count != scan.payload.file_count:
            mismatch.append(
                f"файлов на томе {scan.payload.file_count}, ожидалось {expected_count}"
            )
        if expected_alloc is not None and expected_alloc != scan.payload.alloc_bytes:
            mismatch.append(
                f"по кластерам на томе {fmt_bytes(scan.payload.alloc_bytes)} B, "
                f"ожидалось {fmt_bytes(expected_alloc)} B"
            )

        if mismatch:
            lines.append(
                "⚠ Содержимое тома не совпало с расчётом: "
                + "; ".join(mismatch)
                + ". Запас на копирование из такой записи брать нельзя."
            )
        else:
            lines.append(
                f"Содержимое тома {drive} совпало с расчётом: "
                f"{scan.payload.file_count} файлов, "
                f"{fmt_bytes(scan.payload.alloc_bytes)} B по кластерам."
            )
        if scan.errors:
            lines.append(f"Часть тома не прочитана ({len(scan.errors)} путей).")
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
            self.issues_label.setText(
                "<i>На вкладке «Расчёт» нет данных: выберите файл или папку "
                "либо введите размер.</i>"
            )
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
        self.header_label.setText(fmt_both(record.vc_header))
        self.ntfs_label.setText(fmt_both(record.metadata_bytes))
        self.consumed_label.setText(fmt_both(record.consumed_bytes))
        self.slack_label.setText(fmt_both(record.copy_slack_measured))
        self._refresh_forecast(record)

        issues = validate(record)
        if not record.id:
            self.issues_label.setText("<i>Укажите имя записи.</i>")
        elif issues:
            body = "<br>".join(f"⚠ {issue.message}" for issue in issues)
            self.issues_label.setText(body)
        else:
            self.issues_label.setText("")

        self.save_button.setEnabled(bool(record.id))
        self.save_button.setText("Сохранить с пометкой" if issues else "Сохранить")

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
            self.miss_label.setText(
                "—  (нужны «Обещано расчётом» и остаток после копии)"
            )
            return
        model_miss = record.model_miss_mib
        verdict = (
            "данные не влезли бы"
            if miss < 0
            else ("впритык" if miss == 0 else "перезаклад")
        )
        self.miss_label.setText(
            f"{miss:+d} MiB — {verdict}; из них промах моделей "
            f"{model_miss:+d} MiB, остальное страховка."
        )

    def _on_save(self) -> None:
        """A flawed record is saved but flagged, and calibrates no model."""
        record = self.build_record()
        self._record = replace(record, flagged=bool(validate(record)))
        self.accept()

    def result_record(self) -> Record:
        return self._record
