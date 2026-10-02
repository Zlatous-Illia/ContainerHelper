"""Choosing a mounted volume and taking a measurement."""

from __future__ import annotations

from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

from ..formatting import fmt_both
from ..i18n import tr
from ..records import DEFAULT_PROFILE, VolumeProfile, volume_profile
from ..sizes import cluster_size, mounted_drives, volume_filesystem, volume_usage
from .language import repeated_change


#: What the dialog does when opened without specifics, as a key. The caller
#: usually knows more — which measurement exactly it is taking — and passes
#: its own key.
DEFAULT_PROMPT = "measure.prompt.default"


def _profile_text(profile: VolumeProfile) -> str:
    """«exFAT, 32 KiB»: the profile as the warning names it."""
    cluster = profile.cluster_bytes
    if not cluster:
        size = tr("measure.profile.cluster_unknown")
    elif cluster % 1024:
        size = f"{cluster} B"
    else:
        size = f"{cluster // 1024} KiB"
    return f"{profile.filesystem}, {size}"


class MeasureDialog(QDialog):
    """Returns the pair (capacity, free) for the chosen drive letter."""

    def __init__(
        self,
        parent=None,
        prompt: str | None = None,
        prompt_key: str = DEFAULT_PROMPT,
        prompt_params: dict | None = None,
    ) -> None:
        """`prompt_key` with `prompt_params` is the caller's wording, and it
        follows a language switch. `prompt` is ready text: shown as it is,
        never retranslated, and it wins over the key."""
        super().__init__(parent)
        self._prompt_text = prompt
        self._prompt_key = prompt_key
        self._prompt_params = prompt_params or {}
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        self.prompt = QLabel()
        self.prompt.setWordWrap(True)
        layout.addWidget(self.prompt)

        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(self._refresh)
        layout.addWidget(self.combo)

        self.details = QLabel()
        self.details.setWordWrap(True)
        layout.addWidget(self.details)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        layout.addWidget(buttons)

        self.retranslate()
        self._populate()

    def retranslate(self) -> None:
        self.setWindowTitle(tr("measure.title"))
        if self._prompt_text is not None:
            self.prompt.setText(self._prompt_text)
        else:
            self.prompt.setText(tr(self._prompt_key, **self._prompt_params))
        self.combo.setToolTip(tr("measure.drive.tip"))
        self.details.setToolTip(tr("measure.details.tip"))
        # The details are read from the volume again: that keeps the chosen
        # letter, while `_populate` would start the list over.
        if self.combo.count():
            self._refresh()
        else:
            self.details.setText(tr("measure.details.none"))

    def event(self, event) -> bool:
        if repeated_change(self, event):
            return True
        return super().event(event)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate()
        super().changeEvent(event)

    def _populate(self) -> None:
        drives = mounted_drives()
        self.combo.clear()
        for drive in drives:
            self.combo.addItem(drive, drive)
        if not drives:
            self.details.setText(tr("measure.details.none"))
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
            self.details.setText(
                tr("measure.details.unavailable", drive=drive, error=exc)
            )
            self.ok_button.setEnabled(False)
            return

        self.ok_button.setEnabled(True)
        raw_filesystem = volume_filesystem(drive)
        filesystem = raw_filesystem or tr("measure.details.filesystem_unknown")
        cluster = cluster_size(drive)
        warning = ""
        profile = volume_profile(raw_filesystem, cluster or 0)
        if profile != DEFAULT_PROFILE:
            warning = "\n" + tr(
                "measure.details.other_profile",
                profile=_profile_text(profile),
                default=_profile_text(DEFAULT_PROFILE),
            )
        if cluster:
            cluster_text = tr("measure.details.cluster", size=cluster)
        else:
            cluster_text = tr("measure.details.cluster_unknown")
        self.details.setText(
            tr(
                "measure.details",
                capacity=fmt_both(total),
                free=fmt_both(free),
                filesystem=filesystem,
                cluster=cluster_text,
            )
            + warning
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
