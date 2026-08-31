"""Наборы файлов для замера запаса на копирование.

Запас на копирование — это то, во что обходится само появление файлов на
томе сверх их кластерного размера: запись MFT на каждый файл, рост индекса
каталога, служебные структуры первой записи. Модель считает его как
`base + per_file × n`, и по-файловая часть до сих пор не подтверждена ничем:
разделить две части можно только по замерам с разным числом файлов, а такой
замер в хранилище был ровно один.

Отсюда наборы: несколько заведомо разных `n`, снятых подряд одной машиной.
Файлы генерируются **прямо на смонтированном томе**, а не копируются откуда-то
с диска. Копировать нечего — набора такого вида ни у кого не лежит, — а хосту
пришлось бы держать вторую копию рядом с контейнером и тратить вдвое больше
места. Для измеряемой величины это одно и то же: запас меряет появление файлов
на томе, а не происхождение байтов.

Без Qt: состав наборов и их арифметика — не отображение, и проверяться должны
без интерфейса.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .model import DEFAULT_CLUSTER_BYTES, MIB, Payload, round_up

KIB = 1024
GIB = 1024 * MIB

#: Самый мелкий файл набора. Килобайт, а не меньше: файл короче примерно
#: семисот байт NTFS держит прямо в записи MFT и кластера ему не выдаёт вовсе.
#: Тогда Σ ceil(size / cluster) завышает занятое, измеренный запас выходит
#: отрицательным, и запись отбраковывается проверкой «занято меньше файла».
SMALL_FILE = KIB

#: Имя файла в наборе. Длинное намеренно: запись в индексе каталога тем
#: больше, чем длиннее имя, и на коротких именах индекс вырос бы меньше, чем
#: у настоящих данных. Занижение запаса — единственная опасная сторона, и
#: ошибаться тут надо в другую.
FILE_NAME = "containerhelper-payload-{index:06d}.bin"

#: Папка на томе, в которую ложится набор. Не корень: данные почти всегда
#: кладут папкой, а индекс корня устроен не так, как индекс обычного каталога.
PAYLOAD_DIR = "containerhelper-payload"

#: Каким куском писать. Четыре мегабайта — компромисс между числом системных
#: вызовов и памятью под буфер.
WRITE_CHUNK = 4 * MIB


@dataclass(frozen=True)
class Group:
    """Одна размерная группа набора: столько-то файлов такого-то размера."""

    count: int
    size_bytes: int

    @property
    def logical_bytes(self) -> int:
        return self.count * self.size_bytes

    def alloc_bytes(self, cluster_bytes: int = DEFAULT_CLUSTER_BYTES) -> int:
        """Сумма кластерных размеров, а не кластерный размер суммы.

        Каждый файл округляется вверх по отдельности: на пятистах файлах по
        килобайту разница между этими двумя величинами — полтора мегабайта,
        и вся она уехала бы в измеряемый запас.
        """
        return self.count * round_up(self.size_bytes, cluster_bytes)


@dataclass(frozen=True)
class FileSet:
    """Набор файлов: из чего состоит и сколько места займёт."""

    key: str
    title: str
    groups: tuple[Group, ...]

    @property
    def file_count(self) -> int:
        return sum(group.count for group in self.groups)

    @property
    def logical_bytes(self) -> int:
        return sum(group.logical_bytes for group in self.groups)

    def alloc_bytes(self, cluster_bytes: int = DEFAULT_CLUSTER_BYTES) -> int:
        return sum(group.alloc_bytes(cluster_bytes) for group in self.groups)

    def payload(self, cluster_bytes: int = DEFAULT_CLUSTER_BYTES) -> Payload:
        """Payload, не разворачивая набор в список размеров.

        Десять тысяч чисел ради суммы, которая считается умножением, —
        напрасная работа и на обходе, и в памяти.
        """
        return Payload(
            logical_bytes=self.logical_bytes,
            alloc_bytes=self.alloc_bytes(cluster_bytes),
            file_count=self.file_count,
            cluster_bytes=cluster_bytes,
        )


#: Наборы, которыми калибруется запас. Числа файлов выбраны так, чтобы
#: различных `n` было много: наклон в CopySlackModel.calibrate считается
#: методом наименьших квадратов и берётся вообще только при двух и более
#: различных `n`, а зависимость от `n` не совсем прямая — MFT прирастает
#: кусками, и по двум точкам наклон был бы случайным.
#:
#: Объём при этом почти везде маленький: запас зависит от числа файлов, а не
#: от их размера. Ровно это предположение и проверяют два набора с `n = 1` —
#: 64 MiB и 4 GiB. Разойдись они, модель «запас зависит только от n» неверна,
#: и узнать это надо явно, а не подозревать.
FILE_SETS = (
    FileSet("one", "Один файл 64 MiB", (Group(1, 64 * MIB),)),
    FileSet("fifty", "50 файлов по 10 MiB", (Group(50, 10 * MIB),)),
    FileSet("small-500", "500 файлов по 1 KiB", (Group(500, SMALL_FILE),)),
    FileSet("small-5000", "5 000 файлов по 1 KiB", (Group(5000, SMALL_FILE),)),
    FileSet("small-10000", "10 000 файлов по 1 KiB", (Group(10000, SMALL_FILE),)),
    FileSet(
        "mixed",
        "500 × 1 KiB + 50 × 10 MiB + 1 × 1 GiB",
        (Group(500, SMALL_FILE), Group(50, 10 * MIB), Group(1, GIB)),
    ),
    FileSet("huge", "Один файл 4 GiB", (Group(1, 4 * GIB),)),
)


def fileset_by_key(key: str) -> FileSet | None:
    for item in FILE_SETS:
        if item.key == key:
            return item
    return None


def file_sizes(fileset: FileSet) -> Iterable[int]:
    """Размеры файлов набора по порядку — тем же, каким они создаются."""
    for group in fileset.groups:
        for _ in range(group.count):
            yield group.size_bytes


def generate(
    root: Path,
    fileset: FileSet,
    on_progress: Callable[[int, int], None] | None = None,
    check: Callable[[int], None] | None = None,
) -> int:
    """Создать набор в папке root. Возвращает число созданных файлов.

    `check` зовётся на границе каждого файла и после каждого полного куска
    большого файла; ему передано число уже записанных байт. Прервать запись
    он может только исключением — возвращаемое значение не смотрится, потому
    что молчаливый отказ здесь неотличим от успеха. Через него же работают и
    отмена, и слежение за свободным местом хоста: терабайтный диск может
    кончиться посреди записи от постороннего процесса, а запись в динамический
    контейнер на кончившемся диске рвёт том.

    `on_progress` зовётся на границе каждого файла и throttling — забота
    вызывающего: на десяти тысячах файлов сигнал через границу потока десять
    тысяч раз забьёт очередь событий.
    """
    root.mkdir(parents=True, exist_ok=True)
    # Не нули: том свежий и без сжатия, но зависеть от того, что нули на нём
    # ничем не свернутся, незачем. Буфер не больше самого большого файла —
    # набору из килобайтных файлов четыре мегабайта случайных байт ни к чему.
    largest = max((group.size_bytes for group in fileset.groups), default=0)
    buffer = os.urandom(max(min(largest, WRITE_CHUNK), 1))

    index = 0
    files_done = 0
    bytes_done = 0
    since_check = 0
    for group in fileset.groups:
        for _ in range(group.count):
            if check is not None:
                check(bytes_done)
                since_check = 0
            path = root / FILE_NAME.format(index=index)
            index += 1
            with open(path, "wb") as handle:
                left = group.size_bytes
                while left > 0:
                    chunk = min(left, len(buffer))
                    handle.write(buffer if chunk == len(buffer) else buffer[:chunk])
                    left -= chunk
                    bytes_done += chunk
                    since_check += chunk
                    if since_check >= WRITE_CHUNK and check is not None:
                        check(bytes_done)
                        since_check = 0
            files_done += 1
            if on_progress is not None:
                on_progress(files_done, bytes_done)
    return files_done
