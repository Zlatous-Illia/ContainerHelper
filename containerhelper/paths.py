"""Где лежат данные и настройки.

Программа рассчитана на портативную папку: рядом с исполняемым файлом
каталог `data`, в нём записи, резервные копии и настройки. Реестр не
трогается вовсе — иначе утилита перестаёт быть портативной, а на другой
машине молча забывает всё.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: Имя подпапки рядом с исполняемым файлом. Корень папки остаётся чистым:
#: .bak и .tmp не мешаются под ногами.
DATA_DIR_NAME = "data"

RECORDS_NAME = "Records.json"

#: Замеры пустых томов лежат своим файлом. Они описывают не работу с данными,
#: а машину: сборку Windows и версию VeraCrypt. Записи о копировании переносят
#: с собой, замеры на чужой машине бессмысленны, и держать их в одном файле
#: значило бы возить чужую калибровку под видом своей.
CALIBRATION_NAME = "Calibration.json"

SETTINGS_NAME = "settings.ini"

#: Ключ аргумента командной строки. Прописывается в ярлык и потому не
#: требует никакого сохранённого состояния — этим он и хорош там, где папка
#: программы не пишется.
DATA_ARGUMENT = "--data"

#: Единственный след вне портативной папки. Появляется только после того, как
#: пользователю сказали, что папка не пишется, и он выбрал место сам. Ключ —
#: путь к папке программы, чтобы две копии не затирали выбор друг друга.
LOCATION_ORGANISATION = "ContainerHelper"
LOCATION_NAME = "location.ini"


class DataDirError(Exception):
    """Каталог данных недоступен и выбрать его автоматически не вышло."""


def program_dir() -> Path:
    """Папка программы: рядом с exe в сборке, корень проекта в исходниках."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    return program_dir() / DATA_DIR_NAME


def data_dir_from_arguments(argv: list[str] | None = None) -> Path | None:
    """Разобрать `--data <путь>` или `--data=<путь>`."""
    argv = list(sys.argv[1:] if argv is None else argv)
    for index, item in enumerate(argv):
        if item == DATA_ARGUMENT and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser()
        if item.startswith(DATA_ARGUMENT + "="):
            return Path(item.split("=", 1)[1]).expanduser()
    return None


def is_writable(directory: Path) -> bool:
    """Проверка записью, а не правами: права на сетевых дисках врут."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _location_store() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / LOCATION_ORGANISATION / LOCATION_NAME


def remembered_data_dir() -> Path | None:
    """Папка, выбранная руками для этой копии программы."""
    from PySide6.QtCore import QSettings

    store = _location_store()
    if not store.exists():
        return None
    settings = QSettings(str(store), QSettings.IniFormat)
    value = settings.value(_location_key(), "", type=str)
    return Path(value) if value else None


def remember_data_dir(directory: Path) -> None:
    from PySide6.QtCore import QSettings

    store = _location_store()
    store.parent.mkdir(parents=True, exist_ok=True)
    settings = QSettings(str(store), QSettings.IniFormat)
    settings.setValue(_location_key(), str(directory))
    settings.sync()


def _location_key() -> str:
    """Ключ по папке программы: две копии не должны сталкиваться."""
    return str(program_dir()).replace("\\", "/").replace("/", "|")


def resolve_data_dir(argv: list[str] | None = None) -> tuple[Path | None, str]:
    """Найти каталог данных. Возвращает путь и то, откуда он взялся.

    Порядок: аргумент командной строки, папка рядом с программой,
    запомненный ранее выбор. Ничего не подошло — None, и спросить должен
    вызывающий: тихо уезжать в чужой каталог программа не должна.
    """
    chosen = data_dir_from_arguments(argv)
    if chosen is not None:
        if is_writable(chosen):
            return chosen, DATA_ARGUMENT
        raise DataDirError(f"Каталог из {DATA_ARGUMENT} недоступен на запись: {chosen}")

    beside = default_data_dir()
    if is_writable(beside):
        return beside, "рядом с программой"

    remembered = remembered_data_dir()
    if remembered is not None and is_writable(remembered):
        return remembered, "выбран ранее"

    return None, "папка программы недоступна на запись"


def records_path(data_dir: Path) -> Path:
    return data_dir / RECORDS_NAME


def calibration_path(data_dir: Path) -> Path:
    return data_dir / CALIBRATION_NAME


def settings_path(data_dir: Path) -> Path:
    return data_dir / SETTINGS_NAME
