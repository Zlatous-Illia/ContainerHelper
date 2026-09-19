"""Tests of walking the input data and of number formatting."""

import tempfile
import unittest
from pathlib import Path

from containerhelper.formatting import fmt_both, fmt_bytes, fmt_mib, parse_bytes
from containerhelper.sizes import scan_path


class ScanTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def _write(self, relative: str, size: int) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * size)
        return path

    def test_single_file(self):
        path = self._write("one.bin", 5000)
        result = scan_path(path, 4096)
        self.assertTrue(result.ok)
        self.assertEqual(result.sizes, [5000])
        self.assertEqual(result.payload.logical_bytes, 5000)
        self.assertEqual(result.payload.alloc_bytes, 8192)
        self.assertEqual(result.payload.file_count, 1)

    def test_folder_counts_every_file_and_rounds_each(self):
        self._write("a.bin", 1)
        self._write("b.bin", 4097)
        self._write("nested/c.bin", 4096)

        result = scan_path(self.root, 4096)
        self.assertTrue(result.ok)
        self.assertEqual(result.payload.file_count, 3)
        self.assertEqual(result.payload.logical_bytes, 1 + 4097 + 4096)
        self.assertEqual(result.payload.alloc_bytes, 4096 + 8192 + 4096)

    def test_cluster_change_reuses_the_scan(self):
        self._write("a.bin", 1)
        self._write("b.bin", 4097)
        result = scan_path(self.root, 4096)

        recomputed = result.with_cluster(65536)
        self.assertEqual(recomputed.alloc_bytes, 65536 * 2)
        self.assertEqual(recomputed.file_count, 2)

    def test_empty_folder(self):
        result = scan_path(self.root, 4096)
        self.assertTrue(result.ok)
        self.assertEqual(result.payload.alloc_bytes, 0)
        self.assertEqual(result.payload.file_count, 0)

    def test_missing_path_reports_error_without_raising(self):
        result = scan_path(self.root / "absent", 4096)
        self.assertFalse(result.ok)
        self.assertEqual(result.payload.logical_bytes, 0)


class FormattingTests(unittest.TestCase):
    def test_thousands_separators(self):
        self.assertEqual(fmt_bytes(11_553_254_233), "11 553 254 233")
        self.assertEqual(fmt_bytes(0), "0")
        self.assertEqual(fmt_bytes(-4096), "-4 096")
        self.assertEqual(fmt_bytes(None), "—")

    def test_mib_conversion(self):
        self.assertEqual(fmt_mib(1024 * 1024), "1.00")
        self.assertEqual(fmt_both(1024 * 1024), "1 048 576 B · 1.00 MiB")

    def test_parse_accepts_separators(self):
        self.assertEqual(parse_bytes("11 553 254 233"), 11_553_254_233)
        self.assertEqual(parse_bytes("11553254233"), 11_553_254_233)
        self.assertEqual(parse_bytes("  4096  "), 4096)

    def test_parse_rejects_garbage(self):
        self.assertIsNone(parse_bytes(""))
        self.assertIsNone(parse_bytes("abc"))
        self.assertIsNone(parse_bytes("-1"))


if __name__ == "__main__":
    unittest.main()
