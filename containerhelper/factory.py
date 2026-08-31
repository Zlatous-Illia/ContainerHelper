"""Заводские точки калибровки, которые едут внутри программы.

Смысл в том, чтобы новая копия считала осмысленно с первого запуска: снять
двадцать шесть пустых контейнеров от 512 MiB до 1 TiB — это несколько часов
работы, и без динамических контейнеров ещё и терабайт свободного места.

Данные только на чтение. Заменить их нельзя, можно лишь перекрыть своим
замером на тот же размер тома — и это правильный порядок, потому что размер
метаданных решает не NTFS вообще, а конкретный код форматирования: сборка
Windows и версия VeraCrypt. Своё всегда вернее заводского.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache

from .model import MIB, VC_HEADER_BYTES

PACKAGE_DATA = "containerhelper.data"
FACTORY_FILE = "factory_points.json"

#: Заводские замеры запаса на копирование. Отдельным файлом, а не ключом в
#: том же: точки NTFS снимаются пустыми контейнерами за минуты, а замеры
#: запаса пишут на том гигабайты, и обновляются они порознь.
FACTORY_SLACK_FILE = "factory_slack.json"

#: Надбавка к страховке, пока оба конца отрезка — заводские точки. Чужая
#: сборка Windows могла выбрать другой размер $LogFile, а занижение здесь и
#: есть единственная опасная сторона. Свой замер рядом надбавку снимает.
FACTORY_MARGIN_BYTES = 4 * MIB


@dataclass(frozen=True)
class FactoryPoint:
    container_mib: int
    cluster_bytes: int
    mounted_bytes: int
    empty_free_bytes: int

    @property
    def ntfs_bytes(self) -> int:
        return self.mounted_bytes - self.empty_free_bytes


@dataclass(frozen=True)
class FactorySample:
    """Заводской замер запаса на копирование: набор файлов на пустом томе.

    Хранятся только измеренные величины, как и везде: сам запас выводится из
    них. Ровно дублирование вычислимых полей и испортило исходные рукописные
    записи.
    """

    fileset: str
    title: str
    container_mib: int
    cluster_bytes: int
    mounted_bytes: int
    empty_free_bytes: int
    file_bytes: int
    file_count: int
    file_alloc_bytes: int
    left_bytes: int

    @property
    def copy_slack_bytes(self) -> int:
        return (self.empty_free_bytes - self.left_bytes) - self.file_alloc_bytes


@dataclass(frozen=True)
class FactoryData:
    source: str
    note: str
    points: tuple[FactoryPoint, ...]
    #: Заводские замеры запаса. Пусто, пока их не сняли ни разу: пустой
    #: список честнее выдуманных чисел.
    samples: tuple[FactorySample, ...] = ()

    def by_volume(self) -> dict[int, FactoryPoint]:
        return {point.mounted_bytes: point for point in self.points}


def _empty() -> FactoryData:
    return FactoryData(source="", note="", points=(), samples=())


def _read_resource(name: str) -> dict:
    """Прочитать ресурс пакета. Отсутствие файла — не ошибка, а пустота.

    Через importlib.resources, а не по пути на диске: в сборке одним файлом
    ресурс распаковывается во временный каталог, и обычный путь туда не ведёт.
    """
    try:
        from importlib.resources import files

        raw = (files(PACKAGE_DATA) / name).read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_samples(data: dict) -> tuple[FactorySample, ...]:
    """Заводские замеры запаса. Битый файл оставляет их пустыми, а не рушит.

    Пустой список — рабочее состояние, а не поломка: замеры запаса появляются
    только после настоящего прогона на живой машине.
    """
    try:
        return tuple(
            FactorySample(
                fileset=str(item.get("fileset", "")),
                title=str(item.get("title", "")),
                container_mib=int(item["container_mib"]),
                cluster_bytes=int(item.get("cluster_bytes", 4096)),
                mounted_bytes=int(item["mounted_bytes"]),
                empty_free_bytes=int(item["empty_free_bytes"]),
                file_bytes=int(item["file_bytes"]),
                file_count=int(item["file_count"]),
                file_alloc_bytes=int(item["file_alloc_bytes"]),
                left_bytes=int(item["left_bytes"]),
            )
            for item in data.get("samples", ())
        )
    except (ValueError, KeyError, TypeError):
        return ()


@lru_cache(maxsize=1)
def factory_data() -> FactoryData:
    """Прочитать заводские точки и замеры запаса из ресурсов пакета.

    Отсутствие любого из файлов — не ошибка: программа просто остаётся без
    соответствующих заводских данных.
    """
    data = _read_resource(FACTORY_FILE)
    try:
        points = tuple(
            FactoryPoint(
                container_mib=int(item["container_mib"]),
                cluster_bytes=int(item.get("cluster_bytes", 4096)),
                mounted_bytes=int(item["mounted_bytes"]),
                empty_free_bytes=int(item["empty_free_bytes"]),
            )
            for item in data.get("points", ())
        )
    except (ValueError, KeyError, TypeError):
        return _empty()

    return FactoryData(
        source=str(data.get("source", "")),
        note=str(data.get("note", "")),
        points=points,
        samples=_read_samples(_read_resource(FACTORY_SLACK_FILE)),
    )


def factory_volume(container_mib: int) -> int:
    """Размер тома, который даёт контейнер такого размера."""
    return container_mib * MIB - VC_HEADER_BYTES
