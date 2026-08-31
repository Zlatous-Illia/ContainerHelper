"""Выбор смонтированного тома и снятие замера."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

from ..formatting import fmt_both
from ..sizes import SUPPORTED_FS, cluster_size, mounted_drives, volume_filesystem, volume_usage


#: Что диалог делает, когда его открыли без уточнения. Вызывающий обычно
#: знает больше — какой именно замер он снимает — и передаёт свой текст.
DEFAULT_PROMPT = (
    "Смонтируйте контейнер и выберите его букву.\n"
    "Пустой том даёт ёмкость и метаданные NTFS, после\n"
    "копирования — остаток свободного места."
)


class MeasureDialog(QDialog):
    """Возвращает пару (ёмкость, свободно) для выбранной буквы диска."""

    def __init__(self, parent=None, prompt: str = DEFAULT_PROMPT) -> None:
        super().__init__(parent)
        self.setWindowTitle("Измерить том")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        self.prompt = QLabel(prompt)
        self.prompt.setWordWrap(True)
        layout.addWidget(self.prompt)

        self.combo = QComboBox()
        self.combo.setToolTip(
            "Буква смонтированного тома. Ёмкость, свободное место, файловую "
            "систему и размер кластера прочитаем с него самого.\n"
            "В списке все тома системы, включая обычные диски, — выбирайте "
            "контейнер."
        )
        self.combo.currentIndexChanged.connect(self._refresh)
        layout.addWidget(self.combo)

        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setToolTip(
            "Ёмкость — размер тома без заголовка VeraCrypt.\n"
            "Свободно — сколько на нём осталось. На пустом томе разница между "
            "ними и есть метаданные NTFS.\n"
            "Кластер и файловая система тоже попадут в запись."
        )
        layout.addWidget(self.details)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        layout.addWidget(buttons)

        self._populate()

    def _populate(self) -> None:
        drives = mounted_drives()
        self.combo.clear()
        for drive in drives:
            self.combo.addItem(drive, drive)
        if not drives:
            self.details.setText("Смонтированных томов не найдено.")
            self.ok_button.setEnabled(False)
        else:
            self._refresh()

    def _refresh(self) -> None:
        drive = self.combo.currentData()
        if not drive:
            return
        try:
            total, free = volume_usage(drive)
        except OSError as exc:
            self.details.setText(f"Том {drive} недоступен: {exc}")
            self.ok_button.setEnabled(False)
            return

        self.ok_button.setEnabled(True)
        filesystem = volume_filesystem(drive) or "неизвестна"
        cluster = cluster_size(drive)
        warning = ""
        if filesystem.upper() not in (SUPPORTED_FS, "НЕИЗВЕСТНА"):
            warning = (
                f"\n\u26a0 Модель метаданных снята на {SUPPORTED_FS}. У {filesystem}"
                f" накладные расходы устроены иначе, и в калибровку такой замер"
                f" не пойдёт."
            )
        self.details.setText(
            f"Ёмкость:   {fmt_both(total)}\n"
            f"Свободно:  {fmt_both(free)}\n"
            f"Система:   {filesystem}, кластер "
            f"{cluster if cluster else 'не определён'} B{warning}"
        )

    def volume_facts(self) -> tuple[str, int | None]:
        """Файловая система и размер кластера выбранного тома.

        Оба читаются, а не спрашиваются: том уже существует, и гадать про
        него незачем — на этом и держится проверка, что замер сделан на NTFS.
        """
        drive = self.combo.currentData()
        if not drive:
            return "", None
        return volume_filesystem(drive), cluster_size(drive)

    def selected_drive(self) -> str | None:
        """Буква выбранного тома — нужна, чтобы обойти его содержимое."""
        return self.combo.currentData()

    def result_values(self) -> tuple[int, int] | None:
        drive = self.combo.currentData()
        if not drive:
            return None
        try:
            return volume_usage(drive)
        except OSError:
            return None
