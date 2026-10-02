"""File sets: the arithmetic of a set and its generation on the volume."""

import tempfile
import unittest
from pathlib import Path

from containerhelper.fileset import (
    FILE_NAME,
    FILE_SETS,
    GIB,
    KIB,
    SMALL_FILE,
    WRITE_CHUNK,
    FileSet,
    Group,
    fileset_by_key,
    generate,
)
from containerhelper.model import MIB, CopySlackModel

TINY = FileSet("tiny", "10 файлов по 1 KiB", (Group(10, KIB),))
MIXED = FileSet(
    "mixed-tiny",
    "5 × 1 KiB + 1 × 100 KiB",
    (Group(5, KIB), Group(1, 100 * KIB)),
)


class ArithmeticTests(unittest.TestCase):
    def test_alloc_rounds_every_file_separately(self):
        """Otherwise 1.5 megabytes over 500 files would end up in slack."""
        five_hundred = FileSet("x", "x", (Group(500, KIB),))
        self.assertEqual(five_hundred.logical_bytes, 500 * KIB)
        self.assertEqual(five_hundred.alloc_bytes(4096), 500 * 4096)

    def test_cluster_size_changes_the_alloc(self):
        self.assertEqual(TINY.alloc_bytes(4096), 10 * 4096)
        self.assertEqual(TINY.alloc_bytes(65536), 10 * 65536)

    def test_groups_add_up(self):
        self.assertEqual(MIXED.file_count, 6)
        self.assertEqual(MIXED.logical_bytes, 5 * KIB + 100 * KIB)
        self.assertEqual(MIXED.alloc_bytes(4096), 5 * 4096 + 25 * 4096)

    def test_payload_does_not_materialise_every_size(self):
        """Ten thousand numbers for a sum that one multiplication gives."""
        payload = FILE_SETS[-1].payload(4096)
        self.assertEqual(payload.file_count, FILE_SETS[-1].file_count)
        self.assertEqual(payload.alloc_bytes, FILE_SETS[-1].alloc_bytes(4096))


class CatalogueTests(unittest.TestCase):
    """What the file sets contain decides what can be calibrated at all."""

    def test_enough_different_file_counts_to_fit_a_slope(self):
        """With a single file count the slope is not computed at all.

        CopySlackModel.calibrate uses least squares only with two or more
        distinct n, and the dependence comes in steps — from two points the
        slope would be random.
        """
        counts = {item.file_count for item in FILE_SETS}
        self.assertGreaterEqual(len(counts), 4)

    def test_two_sets_share_a_file_count_but_not_a_size(self):
        """Tests the assumption "slack depends only on the file count"."""
        singles = [item for item in FILE_SETS if item.file_count == 1]
        self.assertEqual(len(singles), 2)
        sizes = {item.logical_bytes for item in singles}
        self.assertEqual(len(sizes), 2)
        self.assertEqual(max(sizes), 4 * GIB)

    def test_the_described_mixed_set_is_there(self):
        mixed = fileset_by_key("mixed")
        self.assertIsNotNone(mixed)
        self.assertEqual(
            [(group.count, group.size_bytes) for group in mixed.groups],
            [(500, KIB), (50, 10 * MIB), (1, GIB)],
        )

    def test_the_smallest_file_still_gets_a_cluster(self):
        """Files under ~700 B sit in the MFT record; slack goes negative."""
        self.assertGreaterEqual(SMALL_FILE, 1024)

    def test_unknown_key_is_not_a_set(self):
        self.assertIsNone(fileset_by_key("нет такого"))

    def test_calibrating_on_these_counts_separates_the_per_file_part(self):
        """The very thing the file sets exist for."""
        samples = [
            (item.file_count, 200_000 + 1000 * item.file_count)
            for item in FILE_SETS
        ]
        model = CopySlackModel.calibrate(samples)
        self.assertTrue(model.per_file_calibrated)
        self.assertEqual(model.per_file, 1000)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name) / "payload"

    def tearDown(self):
        self._dir.cleanup()

    def files(self):
        return sorted(path.name for path in self.root.glob("*"))

    def test_every_file_of_the_set_appears(self):
        self.assertEqual(generate(self.root, TINY), 10)
        self.assertEqual(len(self.files()), 10)

    def test_sizes_are_exactly_as_asked(self):
        generate(self.root, MIXED)
        sizes = sorted(path.stat().st_size for path in self.root.glob("*"))
        self.assertEqual(sizes, [KIB] * 5 + [100 * KIB])

    def test_names_are_long_enough_to_grow_the_index_honestly(self):
        """Short names grow the index less than real ones: an underestimate."""
        generate(self.root, TINY)
        self.assertTrue(all(len(name) >= 30 for name in self.files()))
        self.assertEqual(len(set(self.files())), 10)

    def test_a_file_larger_than_the_buffer_is_written_whole(self):
        big = FileSet("big", "big", (Group(1, WRITE_CHUNK + 12345),))
        generate(self.root, big)
        written = next(self.root.glob("*")).stat().st_size
        self.assertEqual(written, WRITE_CHUNK + 12345)

    def test_progress_counts_files_and_bytes(self):
        seen = []
        generate(self.root, TINY, on_progress=lambda files, size: seen.append((files, size)))
        self.assertEqual(seen[0], (1, KIB))
        self.assertEqual(seen[-1], (10, 10 * KIB))
        self.assertEqual([files for files, _ in seen], list(range(1, 11)))

    def test_the_check_runs_on_every_file(self):
        calls = []
        generate(self.root, TINY, check=calls.append)
        self.assertEqual(len(calls), 10)

    def test_the_check_stops_the_run_by_raising(self):
        """Return values are ignored: a silent refusal looks like success."""

        class Enough(Exception):
            pass

        def check(_done):
            if len(self.files()) >= 3:
                raise Enough()

        with self.assertRaises(Enough):
            generate(self.root, TINY, check=check)
        self.assertLess(len(self.files()), 10)

    def test_the_folder_is_made_if_missing(self):
        self.assertFalse(self.root.exists())
        generate(self.root, TINY)
        self.assertTrue(self.root.is_dir())

    def test_content_is_not_all_zeroes(self):
        """Nothing to fold away: no need to rely on compression being off."""
        generate(self.root, TINY)
        data = next(self.root.glob("*")).read_bytes()
        self.assertNotEqual(data, bytes(len(data)))

    def test_the_name_pattern_takes_an_index(self):
        self.assertIn("000007", FILE_NAME.format(index=7))


if __name__ == "__main__":
    unittest.main()
