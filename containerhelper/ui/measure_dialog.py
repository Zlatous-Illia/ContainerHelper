"""Choosing a mounted volume and taking a measurement."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

from ..formatting import fmt_both
from ..records import DEFAULT_PROFILE, VolumeProfile, volume_profile
from ..sizes import cluster_size, mounted_drives, volume_filesystem, volume_usage


#: What the dialog does when opened without specifics. The caller usually
#: knows more — which measurement exactly it is taking — and passes its own
#: text.
DEFAULT_PROMPT = (
    "Смонтируйте контейнер и выберите его букву.\n"
    "Пустой том даёт ёмкость и метаданные NTFS, после\n"
    "копирования — остаток свободного места."
)


def _profile_text(profile: VolumeProfile) -> str:
    """«exFAT, 32 KiB»: the profile as the warning names it."""
    cluster = profile.cluster_bytes
    if not cluster:
        size = "кластер не определён"
    elif cluster % 1024:
        size = f"{cluster} B"
    else:
        size = f"{cluster // 1024} KiB"
    return f"{profile.filesystem}, {size}"


class MeasureDialog(QDialog):
    """Returns the pair (capacity, free) for the chosen drive letter."""

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
            "Ёмкость — сколько показывает том: контейнер без заголовков "
            "VeraCrypt и без одного кластера, который NTFS оставляет себе.\n"
            "Свободно — сколько на нём осталось. На пустом томе размер тома "
            "минус свободное место и есть метаданные NTFS.\n"
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
        raw_filesystem = volume_filesystem(drive)
        filesystem = raw_filesystem or "неизвестна (считается NTFS)"
        cluster = cluster_size(drive)
        warning = ""
        profile = volume_profile(raw_filesystem, cluster or 0)
        if profile != DEFAULT_PROFILE:
            warning = (
                f"\n⚠ Замер пойдёт в профиль {_profile_text(profile)}. "
                f"Расчёт пока ведётся только для "
                f"{_profile_text(DEFAULT_PROFILE)}, и этот замер в нём не "
                f"участвует."
            )
        self.details.setText(
            f"Ёмкость:   {fmt_both(total)}\n"
            f"Свободно:  {fmt_both(free)}\n"
            f"Система:   {filesystem}, кластер "
            f"{cluster if cluster else 'не определён'} B{warning}"
        )

    def volume_facts(self) -> tuple[str, int | None]:
        """Filesystem and cluster size of the chosen volume.

        Both are read, not asked for: the volume already exists, and there is
        no need to guess about it — this is what the check that the
        measurement was taken on NTFS rests on.
        """
        drive = self.combo.currentData()
        if not drive:
            return "", None
        return volume_filesystem(drive), cluster_size(drive)

    def selected_drive(self) -> str | None:
        """The chosen volume's letter — needed to scan its contents."""
        return self.combo.currentData()

    def result_values(self) -> tuple[int, int] | None:
        drive = self.combo.currentData()
        if not drive:
            return None
        try:
            return volume_usage(drive)
        except OSError:
            return None
