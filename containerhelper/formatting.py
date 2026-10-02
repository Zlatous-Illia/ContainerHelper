"""Number formatting for the interface.

On screen, digit groups are separated by spaces; in JSON, never: there
numbers are written solid. That is why formatting lives separately and takes
no part in storage.
"""

from __future__ import annotations

from dataclasses import dataclass

from .i18n import tr
from .model import MIB

#: Digit group separator on screen. A plain space, not a non-breaking one: the
#: value must stay fit for copying and parsing back.
GROUP_SEPARATOR = " "

#: Characters that parse_bytes throws out of the input. Non-breaking and thin
#: spaces get here when pasting from Explorer and from spreadsheets.
#:
#: Not private: the size field validator lives by the same set. Were they to
#: diverge, the field would accept what parsing does not understand, or, the
#: other way round, reject what was pasted from Explorer, and this could only
#: be noticed by hand.
IGNORED_IN_INPUT = (" ", " ", " ", " ", "_")

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
    """Bytes in MiB with a fractional part."""
    if value_bytes is None:
        return DASH
    return _group(value_bytes / MIB, digits)


def fmt_both(value_bytes: int | None) -> str:
    """'1 048 576 B · 1.00 MiB' — bytes and MiB side by side."""
    if value_bytes is None:
        return DASH
    return f"{fmt_bytes(value_bytes)} B · {fmt_mib(value_bytes)} MiB"


def size_label(container_mib: int) -> str:
    """1024 → "1 GiB", 512 → "512 MiB" — the way a size is said out loud.

    Needed where the size goes into a record name and into the labels of
    collection steps: "Calibration 1048576 MiB" cannot be read by eye at all.
    """
    if container_mib % 1024 == 0:
        return f"{container_mib // 1024} GiB"
    return f"{container_mib} MiB"


def parse_bytes(text: str) -> int | None:
    """Parse manual input without being picky about digit group separators."""
    cleaned = text
    for junk in IGNORED_IN_INPUT:
        cleaned = cleaned.replace(junk, "")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    try:
        value = int(cleaned)
    except ValueError:
        return None
    return value if value >= 0 else None


# --- display units ---------------------------------------------------------
#
# Storage and input always stay in bytes: the unit affects only how a number
# is shown in the tables. Otherwise rounding to GiB on input would lose the
# measurement precision that the whole thing was started for.


@dataclass(frozen=True)
class Unit:
    """Display unit. factor = 0 means auto-selection by magnitude."""

    key: str
    symbol: str
    factor: int
    digits: int

    @property
    def label(self) -> str:
        """The symbol; auto-selection has none and is named in words."""
        return self.symbol or tr("unit.auto")


UNIT_BYTE = Unit("B", "B", 1, 0)
UNIT_KIB = Unit("KiB", "KiB", 1024, 2)
UNIT_MIB = Unit("MiB", "MiB", 1024**2, 2)
UNIT_GIB = Unit("GiB", "GiB", 1024**3, 3)
UNIT_TIB = Unit("TiB", "TiB", 1024**4, 3)
UNIT_AUTO = Unit("auto", "", 0, 2)

#: Order in the drop-down list. Bytes first: that is the default.
UNITS = (UNIT_BYTE, UNIT_KIB, UNIT_MIB, UNIT_GIB, UNIT_TIB, UNIT_AUTO)

#: Multiple units from larger to smaller — for auto-selection.
_SCALED = (UNIT_TIB, UNIT_GIB, UNIT_MIB, UNIT_KIB)

DEFAULT_UNIT = UNIT_BYTE


def unit_by_key(key: str) -> Unit:
    for unit in UNITS:
        if unit.key == key:
            return unit
    return DEFAULT_UNIT


def resolve_unit(value_bytes: int, unit: Unit) -> Unit:
    """Expand "Auto" into a concrete unit for this value."""
    if unit.factor:
        return unit
    magnitude = abs(value_bytes)
    for candidate in _SCALED:
        if magnitude >= candidate.factor:
            return candidate
    return UNIT_BYTE


def fmt_in_unit(value_bytes: int | None, unit: Unit = DEFAULT_UNIT) -> str:
    """A byte count in the chosen unit, without the unit name."""
    if value_bytes is None:
        return DASH
    resolved = resolve_unit(value_bytes, unit)
    if resolved.factor <= 1:
        return fmt_bytes(value_bytes)
    sign = "-" if value_bytes < 0 else ""
    return sign + _group(abs(value_bytes) / resolved.factor, resolved.digits)


def unit_suffix(unit: Unit = DEFAULT_UNIT) -> str:
    """Column header suffix: ", MiB", or empty for auto-selection."""
    return "" if not unit.factor else f", {unit.label}"


def fmt_with_unit(value_bytes: int | None, unit: Unit = DEFAULT_UNIT) -> str:
    """The same, but with the unit name alongside — for standalone labels."""
    if value_bytes is None:
        return DASH
    resolved = resolve_unit(value_bytes, unit)
    return f"{fmt_in_unit(value_bytes, resolved)} {resolved.label}"


def fmt_table_cell(value_bytes: int | None, unit: Unit = DEFAULT_UNIT) -> str:
    """Value for a table cell.

    In a fixed unit a bare number is returned: the unit sits in the column
    header. In "Auto" mode each row has its own unit, and without a label the
    column would mix MiB with GiB.
    """
    return fmt_in_unit(value_bytes, unit) if unit.factor else fmt_with_unit(value_bytes, unit)
