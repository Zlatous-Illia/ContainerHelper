"""Tests of the record schema, validation and the store."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from containerhelper.model import MIB, VC_HEADERS_BYTES
from containerhelper.records import (
    SCHEMA_VERSION,
    SCOPE_METADATA,
    SCOPE_SLACK,
    Record,
    Store,
    StoreError,
    VolumeProfile,
    is_usable,
    metadata_points,
    slack_samples,
    validate,
)
from tests import reference


def codes(record):
    return {issue.code for issue in validate(record)}


class ValidationTests(unittest.TestCase):
    def test_consistent_records_pass(self):
        for record in (reference.CACHE_1, reference.CACHE_4):
            with self.subTest(record.id):
                self.assertEqual(validate(record), [])

    def test_impossible_left_space_is_caught(self):
        """Cache 2: less is used than the file itself. A manual entry error."""
        self.assertIn("consumed_lt_file", codes(reference.CACHE_2))

    def test_impossible_left_space_only_invalidates_slack(self):
        """The point for the NTFS model in such a record is still good."""
        self.assertTrue(is_usable(reference.CACHE_2, SCOPE_METADATA))
        self.assertFalse(is_usable(reference.CACHE_2, SCOPE_SLACK))

    def test_truncated_ntfs_value_is_caught(self):
        """36 573 instead of 36 573 184 — digits lost when copied by hand."""
        broken = replace(
            reference.CACHE_2,
            empty_free_bytes=reference.CACHE_2.mounted_bytes - 36_573,
        )
        self.assertIn("ntfs_range", codes(broken))

    def test_large_volume_ntfs_is_not_falsely_flagged(self):
        """At 100 GiB, metadata of nearly a hundred megabytes is normal."""
        volume = 100 * 1024**3
        record = Record(
            id="big",
            container_mib=(volume + reference.HEADERS_AND_TAIL) // (1024 * 1024),
            mounted_bytes=volume,
            empty_free_bytes=volume - 100 * 1024 * 1024,
        )
        self.assertNotIn("ntfs_range", codes(record))

    def test_wrong_container_size_is_caught(self):
        """The NTFS tail is one cluster; a deviation gives away a typo."""
        broken = replace(reference.CACHE_1, container_mib=11131)
        self.assertIn("tail_unusual", codes(broken))

    def test_volume_larger_than_container_is_caught(self):
        broken = replace(reference.CACHE_1, container_mib=1)
        self.assertIn("tail_negative", codes(broken))


class TailTests(unittest.TestCase):
    """The filesystem tail: the volume size minus the measured capacity.

    It used to hide inside a single 266 240 B header constant, which also kept
    every volume other than NTFS with a 4 KiB cluster out of the calibration —
    by accident, through a header that "came out wrong". The checks below say
    it on purpose.
    """

    def test_the_parts_add_up_to_the_container(self):
        for record in reference.ALL:
            with self.subTest(record.id):
                self.assertEqual(
                    VC_HEADERS_BYTES + record.metadata_bytes + record.empty_free_bytes,
                    record.container_bytes,
                )

    def test_the_metadata_does_not_follow_the_capacity(self):
        """FAT leaves its tables out of the capacity; the volume size stays."""
        short = replace(
            reference.CACHE_1, mounted_bytes=reference.CACHE_1.mounted_bytes - MIB
        )
        self.assertEqual(short.metadata_bytes, reference.CACHE_1.metadata_bytes)

    def test_another_ntfs_cluster_stays_out_of_the_default_profile(self):
        """The model is taken on 4 KiB; a 64 KiB point is another curve's node.

        It used to be flagged by a "cluster" check of its own. The profile
        keeps it apart now, and the record stays valid for its own profile.
        """
        big = replace(
            reference.CACHE_1,
            cluster_bytes=65536,
            mounted_bytes=reference.CACHE_1.volume_bytes - 65536,
        )
        self.assertEqual(codes(big), set())
        self.assertEqual(metadata_points([big]), [])
        self.assertEqual(len(metadata_points([big], VolumeProfile("NTFS", 65536))), 1)

    def test_an_unmeasured_ntfs_cluster_has_its_tail_unchecked(self):
        """One cluster at 64 KiB is expected, not measured: no false alarm."""
        big = replace(reference.CACHE_1, cluster_bytes=65536)
        self.assertNotIn("tail_unusual", codes(big))

    def test_an_unread_cluster_counts_as_the_default(self):
        point = Record(
            id="Точка",
            container_mib=1024,
            cluster_bytes=0,
            mounted_bytes=1024 * MIB - reference.HEADERS_AND_TAIL,
            empty_free_bytes=1000 * MIB,
        )
        self.assertEqual(codes(point), set())

    def test_the_tail_is_not_checked_off_ntfs(self):
        """What exFAT keeps back is not measured yet."""
        fat = replace(
            reference.CACHE_1,
            filesystem="exFAT",
            mounted_bytes=reference.CACHE_1.mounted_bytes - MIB,
        )
        self.assertEqual(codes(fat), set())

    def test_the_metadata_range_is_not_checked_off_ntfs(self):
        """A small exFAT volume may spend less than a mebibyte on metadata."""
        fat = replace(
            reference.CACHE_1,
            filesystem="exFAT",
            empty_free_bytes=reference.CACHE_1.volume_bytes - 512 * 1024,
        )
        self.assertEqual(codes(fat), set())
        self.assertIn("ntfs_range", codes(replace(fat, filesystem="NTFS")))

    def test_free_space_above_capacity_is_caught(self):
        broken = replace(
            reference.CACHE_1, empty_free_bytes=reference.CACHE_1.mounted_bytes + 1
        )
        self.assertIn("free_ge_mounted", codes(broken))

    def test_file_count_bounds(self):
        self.assertIn("file_count", codes(replace(reference.CACHE_1, file_count=0)))
        self.assertIn(
            "file_count_gt_bytes",
            codes(replace(reference.CACHE_1, file_bytes=10, file_count=11)),
        )

    def test_flagged_record_is_excluded_everywhere(self):
        flagged = replace(reference.CACHE_1, flagged=True)
        self.assertFalse(is_usable(flagged, SCOPE_METADATA))
        self.assertFalse(is_usable(flagged, SCOPE_SLACK))


class CalibrationInputTests(unittest.TestCase):
    def test_all_three_records_give_ntfs_points(self):
        points = metadata_points(reference.ALL)
        self.assertEqual(len(points), 3)
        self.assertEqual(
            sorted(overhead for _, overhead in points),
            sorted(reference.EXPECTED_NTFS.values()),
        )

    def test_only_consistent_records_give_slack_samples(self):
        samples = slack_samples(reference.ALL)
        self.assertEqual(len(samples), 2)
        self.assertEqual(
            sorted(value for _, value in samples),
            sorted(reference.EXPECTED_SLACK.values()),
        )

    def test_record_without_copy_data_still_calibrates_ntfs(self):
        """An empty container with no copying is a full-fledged NTFS point."""
        empty = Record(
            id="probe",
            container_mib=reference.CACHE_1.container_mib,
            mounted_bytes=reference.CACHE_1.mounted_bytes,
            empty_free_bytes=reference.CACHE_1.empty_free_bytes,
        )
        self.assertEqual(len(metadata_points([empty])), 1)
        self.assertEqual(len(slack_samples([empty])), 0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_round_trip(self):
        store = Store(path=self.path, records=list(reference.ALL))
        store.save()
        loaded = Store.load(self.path)
        self.assertEqual(len(loaded.records), 3)
        for original, restored in zip(reference.ALL, loaded.records):
            self.assertEqual(original.id, restored.id)
            self.assertEqual(original.mounted_bytes, restored.mounted_bytes)
            self.assertEqual(original.left_bytes, restored.left_bytes)

    def test_derived_fields_are_never_persisted(self):
        Store(path=self.path, records=[reference.CACHE_1]).save()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        stored = raw["records"][0]
        for forbidden in (
            "metadata_bytes",
            "vc_header",
            "tail_bytes",
            "volume_bytes",
            "consumed_bytes",
            "container_bytes",
        ):
            self.assertNotIn(forbidden, stored)

    def test_missing_optional_fields_are_omitted(self):
        record = Record(id="probe", container_mib=1024)
        Store(path=self.path, records=[record]).save()
        stored = json.loads(self.path.read_text(encoding="utf-8"))["records"][0]
        self.assertNotIn("left_bytes", stored)
        self.assertIsNone(Store.load(self.path).records[0].left_bytes)

    def test_backup_is_written_before_overwrite(self):
        store = Store(path=self.path, records=[reference.CACHE_1])
        store.save()
        store.records.append(reference.CACHE_4)
        store.save()
        self.assertTrue(store.backup_path.exists())
        backup = json.loads(store.backup_path.read_text(encoding="utf-8"))
        self.assertEqual(len(backup["records"]), 1)

    def test_missing_file_loads_empty(self):
        self.assertEqual(Store.load(self.path).records, [])

    def test_corrupt_file_raises(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(StoreError):
            Store.load(self.path)

    def test_unknown_schema_raises(self):
        self.path.write_text('{"schema": 99, "records": []}', encoding="utf-8")
        with self.assertRaises(StoreError):
            Store.load(self.path)

    def test_numbers_are_written_without_separators(self):
        Store(path=self.path, records=[reference.CACHE_1]).save()
        self.assertIn("11670384640", self.path.read_text(encoding="utf-8"))


class MeasurementRoutingTests(unittest.TestCase):
    def test_first_measurement_fills_empty_volume_fields(self):
        store = Store(path=Path("unused.json"))
        record = Record(id="probe", container_mib=11130)
        updated = store.measure_into(record, 11_670_384_640, 11_630_067_712)
        self.assertEqual(updated.mounted_bytes, 11_670_384_640)
        self.assertEqual(updated.empty_free_bytes, 11_630_067_712)
        self.assertIsNone(updated.left_bytes)

    def test_second_measurement_fills_left_space(self):
        store = Store(path=Path("unused.json"))
        updated = store.measure_into(reference.CACHE_1, 11_670_384_640, 12_345_678)
        self.assertEqual(updated.left_bytes, 12_345_678)
        self.assertEqual(updated.empty_free_bytes, reference.CACHE_1.empty_free_bytes)


class PayloadAllocTests(unittest.TestCase):
    """Cluster-rounded payload: the measured value beats the derived one.

    For a folder of many files Σ ceil(size_i / c) × c is larger than
    ceil(Σ size_i / c) × c, and otherwise the whole difference would end up
    in copy slack.
    """

    def test_derived_from_file_bytes_when_not_measured(self):
        record = Record(id="one", container_mib=1024, file_bytes=10_000)
        self.assertEqual(record.payload_alloc, 12_288)

    def test_measured_value_wins_over_the_derived_one(self):
        record = Record(
            id="many",
            container_mib=1024,
            file_bytes=624_750,
            file_count=500,
            file_alloc_bytes=2_048_000,
        )
        self.assertEqual(record.payload_alloc, 2_048_000)

    def test_copy_slack_uses_the_measured_alloc(self):
        record = Record(
            id="many",
            container_mib=1024,
            empty_free_bytes=10_000_000,
            left_bytes=7_000_000,
            file_bytes=624_750,
            file_count=500,
            file_alloc_bytes=2_048_000,
        )
        self.assertEqual(record.consumed_bytes, 3_000_000)
        self.assertEqual(record.copy_slack_measured, 3_000_000 - 2_048_000)

    def test_alloc_below_logical_size_is_rejected(self):
        record = Record(
            id="bad", container_mib=1024, file_bytes=10_000, file_alloc_bytes=8_192
        )
        self.assertIn("alloc_lt_logical", codes(record))

    def test_alloc_off_the_cluster_grid_is_rejected(self):
        record = Record(
            id="bad", container_mib=1024, file_bytes=10_000, file_alloc_bytes=12_290
        )
        self.assertIn("alloc_not_aligned", codes(record))

    def test_alloc_errors_do_not_disqualify_the_ntfs_point(self):
        record = replace(
            reference.CACHE_1, file_alloc_bytes=12_290, file_bytes=10_000
        )
        self.assertFalse(is_usable(record, SCOPE_SLACK))
        self.assertTrue(is_usable(record, SCOPE_METADATA))


class SchemaCompatibilityTests(unittest.TestCase):
    def test_schema_1_file_still_loads(self):
        """Files taken before file_alloc_bytes existed load as they were."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "records": [
                            {
                                "id": "Cache 1",
                                "container_mib": 11130,
                                "cluster_bytes": 4096,
                                "mounted_bytes": 11_670_384_640,
                                "empty_free_bytes": 11_630_067_712,
                                "file_bytes": 11_553_254_233,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = Store.load(path)
            self.assertEqual(len(store.records), 1)
            self.assertIsNone(store.records[0].file_alloc_bytes)
            # without a measured alloc the behaviour is as before: the sum
            # is rounded
            self.assertEqual(store.records[0].payload_alloc, 11_553_255_424)

    def test_saving_writes_the_current_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new.json"
            store = Store(path=path, records=[reference.CACHE_1])
            store.save()
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema"], SCHEMA_VERSION)

    def test_unknown_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "future.json"
            path.write_text(json.dumps({"schema": 99, "records": []}), encoding="utf-8")
            with self.assertRaises(StoreError):
                Store.load(path)

    def test_alloc_survives_a_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.json"
            record = Record(
                id="folder",
                container_mib=1024,
                file_bytes=624_750,
                file_count=500,
                file_alloc_bytes=2_048_000,
            )
            Store(path=path, records=[record]).save()
            self.assertEqual(Store.load(path).records[0].file_alloc_bytes, 2_048_000)

    def test_absent_alloc_is_not_written(self):
        """Schema rule: only what was measured goes into the file."""
        payload = Record(id="one", container_mib=1024, file_bytes=10_000).to_json()
        self.assertNotIn("file_alloc_bytes", payload)


if __name__ == "__main__":
    unittest.main()
