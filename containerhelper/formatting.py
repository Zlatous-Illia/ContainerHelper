"""Форматирование чисел для интерфейса.

На экране разряды разделяются пробелами, в JSON — никогда: там числа пишутся
слитно. Поэтому форматирование живёт отдельно и не участвует в хранении.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import MIB

#: Разделитель разрядов на экране. Обычный пробел, а не неразрывный: значение
#: должно оставаться пригодным для копирования и обратного разбора.
GROUP_SEPARATOR = " "

#: Символы, которые parse_bytes выбрасывает из ввода. Неразрывный и узкий
#: пробелы попадают сюда при вставке из Проводника и из таблиц.
_IGNORED_IN_INPUT = (" ", " ", " ", " ", "_")

DASH = "—"


def _group(value: int, digits: int | None = None) -> str:
    spec = f",.{digits}f" if digits is not None else ","
    return format(value, spec).replace(",", GROUP_SEPARATOR)


def fmt_bytes(value: int | None) -> str:
    """12345678 -> '12 345 678'."""
    if value is None:
        return DASH
    sign = "-" if value < 0 else ""
    return sign + _group(abs(value))


def fmt_mib(value_bytes: int | None, digits: int = 2) -> str:
    """Байты в MiB с дробной частью."""
    if value_bytes is None:
        return DASH
    return _group(value_bytes / MIB, digits)


def fmt_both(value_bytes: int | None) -> str:
    """'1 048 576 B · 1.00 MiB' — байты и MiB рядом."""
    if value_bytes is None:
        return DASH
    return f"{fmt_bytes(value_bytes)} B · {fmt_mib(value_bytes)} MiB"


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение после числа: 1 точка, 2 точки, 5 точек.

    Нужно там, где число подставляется в текст. «23 точек» в отчёте читается
    как машинный перевод, а исправляется одной строкой.
    """
    if 11 <= abs(count) % 100 <= 14:
        return many
    tail = abs(count) % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def size_label(container_mib: int) -> str:
    """1024 → «1 GiB», 512 → «512 MiB» — как размер называют вслух.

    Нужно там, где размер попадает в имя записи и в подписи шагов сбора:
    «Калибровка 1048576 MiB» не читается глазом вовсе.
    """
    if container_mib % 1024 == 0:
        return f"{container_mib // 1024} GiB"
    return f"{container_mib} MiB"


def parse_bytes(text: str) -> int | None:
    """Разобрать ручной ввод, не придираясь к разделителям разрядов."""
    cleaned = text
    for junk in _IGNORED_IN_INPUT:
        cleaned = cleaned.replace(junk, "")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    try:
        value = int(cleaned)
    except ValueError:
        return None
    return value if value >= 0 else None


# --- единицы отображения ---------------------------------------------------
#
# Хранение и ввод остаются в байтах всегда: единица влияет только на то, как
# число показано в таблицах. Иначе округление до GiB при вводе потеряло бы
# точность замера, ради которой всё и затевалось.


@dataclass(frozen=True)
class Unit:
    """Единица отображения. factor = 0 означает автоподбор по величине."""

    key: str
    label: str
    factor: int
    digits: int


UNIT_BYTE = Unit("B", "B", 1, 0)
UNIT_KIB = Unit("KiB", "KiB", 1024, 2)
UNIT_MIB = Unit("MiB", "MiB", 1024**2, 2)
UNIT_GIB = Unit("GiB", "GiB", 1024**3, 3)
UNIT_TIB = Unit("TiB", "TiB", 1024**4, 3)
UNIT_AUTO = Unit("auto", "Авто", 0, 2)

#: Порядок в выпадающем списке. Байты первыми: это значение по умолчанию.
UNITS = (UNIT_BYTE, UNIT_KIB, UNIT_MIB, UNIT_GIB, UNIT_TIB, UNIT_AUTO)

#: Кратные единицы от большей к меньшей — для автоподбора.
_SCALED = (UNIT_TIB, UNIT_GIB, UNIT_MIB, UNIT_KIB)

DEFAULT_UNIT = UNIT_BYTE


def unit_by_key(key: str) -> Unit:
    for unit in UNITS:
        if unit.key == key:
            return unit
    return DEFAULT_UNIT


def resolve_unit(value_bytes: int, unit: Unit) -> Unit:
    """Развернуть «Авто» в конкретную единицу для этого значения."""
    if unit.factor:
        return unit
    magnitude = abs(value_bytes)
    for candidate in _SCALED:
        if magnitude >= candidate.factor:
            return candidate
    return UNIT_BYTE


def fmt_in_unit(value_bytes: int | None, unit: Unit = DEFAULT_UNIT) -> str:
    """Число байт в выбранной единице, без названия единицы."""
    if value_bytes is None:
        return DASH
    resolved = resolve_unit(value_bytes, unit)
    if resolved.factor <= 1:
        return fmt_bytes(value_bytes)
    sign = "-" if value_bytes < 0 else ""
    return sign + _group(abs(value_bytes) / resolved.factor, resolved.digits)


def unit_suffix(unit: Unit = DEFAULT_UNIT) -> str:
    """Подпись для заголовка столбца: «, MiB» или пусто для автоподбора."""
    return "" if not unit.factor else f", {unit.label}"


def fmt_with_unit(value_bytes: int | None, unit: Unit = DEFAULT_UNIT) -> str:
    """То же, но с названием единицы рядом — для одиночных подписей."""
    if value_bytes is None:
        return DASH
    resolved = resolve_unit(value_bytes, unit)
    return f"{fmt_in_unit(value_bytes, resolved)} {resolved.label}"


def fmt_table_cell(value_bytes: int | None, unit: Unit = DEFAULT_UNIT) -> str:
    """Значение для ячейки таблицы.

    В фиксированной единице возвращается голое число: единица стоит в
    заголовке столбца. В режиме «Авто» единица у каждой строки своя, и без
    подписи столбец смешал бы MiB с GiB.
    """
    return fmt_in_unit(value_bytes, unit) if unit.factor else fmt_with_unit(value_bytes, unit)
