"""Tests of the display units.

A unit changes only what is shown. Input and storage stay in bytes, so
parse_bytes takes no part here and there is no reverse conversion.
"""

import unittest

from containerhelper.formatting import (
    DASH,
    UNIT_AUTO,
    UNIT_BYTE,
    UNIT_GIB,
    UNIT_KIB,
    UNIT_MIB,
    UNIT_TIB,
    UNITS,
    fmt_in_unit,
    fmt_table_cell,
    fmt_with_unit,
    resolve_unit,
    unit_by_key,
    unit_suffix,
)

GIB = 1024**3


class UnitTests(unittest.TestCase):
    def test_bytes_keep_group_separators_and_stay_integer(self):
        self.assertEqual(fmt_in_unit(107_373_916_160, UNIT_BYTE), "107 373 916 160")

    def test_scaled_units_divide_by_their_factor(self):
        self.assertEqual(fmt_in_unit(GIB, UNIT_GIB), "1.000")
        self.assertEqual(fmt_in_unit(GIB, UNIT_MIB), "1 024.00")
        self.assertEqual(fmt_in_unit(2048, UNIT_KIB), "2.00")

    def test_none_shows_a_dash_in_every_unit(self):
        for unit in UNITS:
            with self.subTest(unit.key):
                self.assertEqual(fmt_in_unit(None, unit), DASH)

    def test_negative_values_keep_their_sign(self):
        self.assertEqual(fmt_in_unit(-4 * 1024**2, UNIT_MIB), "-4.00")

    def test_auto_picks_the_largest_unit_that_fits(self):
        self.assertEqual(resolve_unit(512, UNIT_AUTO), UNIT_BYTE)
        self.assertEqual(resolve_unit(4096, UNIT_AUTO), UNIT_KIB)
        self.assertEqual(resolve_unit(5 * 1024**2, UNIT_AUTO), UNIT_MIB)
        self.assertEqual(resolve_unit(5 * GIB, UNIT_AUTO), UNIT_GIB)
        self.assertEqual(resolve_unit(5 * 1024**4, UNIT_AUTO), UNIT_TIB)

    def test_auto_names_the_unit_in_the_cell(self):
        """Otherwise the column would mix MiB and GiB with no label."""
        self.assertEqual(fmt_table_cell(GIB, UNIT_AUTO), "1.000 GiB")
        self.assertEqual(fmt_table_cell(5 * 1024**2, UNIT_AUTO), "5.00 MiB")

    def test_fixed_unit_leaves_the_name_to_the_header(self):
        self.assertEqual(fmt_table_cell(GIB, UNIT_GIB), "1.000")
        self.assertEqual(unit_suffix(UNIT_GIB), ", GiB")

    def test_auto_has_no_header_suffix(self):
        self.assertEqual(unit_suffix(UNIT_AUTO), "")

    def test_unknown_key_falls_back_to_bytes(self):
        self.assertEqual(unit_by_key("parsec"), UNIT_BYTE)
        self.assertEqual(unit_by_key("GiB"), UNIT_GIB)

    def test_fmt_with_unit_spells_the_unit_out(self):
        self.assertEqual(fmt_with_unit(266_240, UNIT_AUTO), "260.00 KiB")
