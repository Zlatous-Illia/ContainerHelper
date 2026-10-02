"""Checking the prediction after the fact: what was promised, what came out.

The value the program exists for. Checked without Qt: the minimum is derived
from the record's measurements, and the prediction is stored in the record.
"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from containerhelper.factory import FactorySample, factory_data
from containerhelper.model import MIB, VC_HEADERS_BYTES
from containerhelper.records import (
    Record,
    Store,
    factory_slack_record,
    forecast,
    slack_key,
    slack_samples,
)

from tests import reference
from tests.reference import HEADERS_AND_TAIL


def copied(**changes) -> Record:
    """A copy record: volume, data and left space — all the check needs."""
    base = Record(
        id="Проба",
        container_mib=1024,
        mounted_bytes=1024 * MIB - HEADERS_AND_TAIL,
        empty_free_bytes=1024 * MIB - HEADERS_AND_TAIL - 18 * MIB,
        file_bytes=900 * MIB,
        file_count=10,
        file_alloc_bytes=900 * MIB,
        left_bytes=100 * MIB,
    )
    return replace(base, **changes)


class MinimumTests(unittest.TestCase):
    """The smallest container the data would still have fitted into."""

    def test_it_matches_the_minimum_worked_out_by_hand(self):
        """The numbers in tests/reference.py predate this property.

        SPEC also names them the "true minimum" from the measured Left space,
        and matching them is the only tie between the new value and real
        containers.
        """
        for record in reference.ALL:
            expected = reference.TRUE_MINIMUM_MIB.get(record.id)
            if expected is None:
                continue
            with self.subTest(record.id):
                self.assertEqual(record.minimum_mib, expected)

    def test_it_is_the_occupied_space_plus_the_header(self):
        record = copied()
        occupied = record.volume_bytes - record.left_bytes
        self.assertEqual(
            record.minimum_mib, -(-(occupied + VC_HEADERS_BYTES) // MIB)
        )

    def test_without_a_leftover_there_is_nothing_to_derive_it_from(self):
        self.assertIsNone(copied(left_bytes=None).minimum_mib)

    def test_a_calibration_point_has_no_minimum(self):
        point = Record(
            id="Калибровка",
            container_mib=1024,
            mounted_bytes=1024 * MIB - HEADERS_AND_TAIL,
            empty_free_bytes=1000 * MIB,
        )
        self.assertIsNone(point.minimum_mib)

    def test_the_estimate_errs_towards_alarm(self):
        """A smaller container would also have had less metadata.

        So the real minimum is a little lower, and the miss a little larger
        than shown. The metric errs towards alarm, and that is the right side.
        """
        record = copied()
        occupied = record.volume_bytes - record.left_bytes
        self.assertGreaterEqual(record.minimum_mib * MIB, occupied + VC_HEADERS_BYTES)


class MissTests(unittest.TestCase):
    def test_a_promise_larger_than_the_minimum_is_an_overshoot(self):
        record = copied(predicted_mib=1024, predicted_safety_mib=4)
        self.assertGreater(record.miss_mib, 0)
        self.assertEqual(record.miss_mib, 1024 - record.minimum_mib)

    def test_a_promise_below_the_minimum_is_the_dangerous_case(self):
        """Negative: the data would not have fitted — what all this is for."""
        record = copied(predicted_mib=10, predicted_safety_mib=0)
        self.assertLess(record.miss_mib, 0)

    def test_the_model_miss_takes_the_safety_out(self):
        record = copied(predicted_mib=1024, predicted_safety_mib=4)
        self.assertEqual(record.model_miss_mib, record.miss_mib - 4)

    def test_a_near_miss_hidden_by_safety_shows_up_in_the_model_miss(self):
        """Exactly why the safety margin is stored as a separate field."""
        record = copied()
        record = replace(
            record,
            predicted_mib=record.minimum_mib + 5,
            predicted_safety_mib=8,
        )
        self.assertGreater(record.miss_mib, 0)
        self.assertLess(record.model_miss_mib, 0)

    def test_without_a_promise_there_is_nothing_to_check(self):
        record = copied()
        self.assertIsNone(record.miss_mib)
        self.assertIsNone(record.model_miss_mib)
        self.assertFalse(record.forecast_checked)

    def test_a_promise_without_a_leftover_is_not_a_check_either(self):
        record = copied(left_bytes=None, predicted_mib=1024)
        self.assertIsNone(record.miss_mib)
        self.assertFalse(record.forecast_checked)

    def test_missing_safety_counts_as_none(self):
        record = copied(predicted_mib=1024)
        self.assertEqual(record.model_miss_mib, record.miss_mib)


class ForecastSummaryTests(unittest.TestCase):
    def test_nothing_to_check_is_said_plainly(self):
        report = forecast([copied()])
        self.assertEqual(report.checked, 0)
        self.assertFalse(report.any_short)

    def test_the_worst_case_is_the_smallest_miss(self):
        """Worst means smallest: negative is more dangerous than positive."""
        report = forecast(
            [
                copied(predicted_mib=1024, predicted_safety_mib=4),
                copied(predicted_mib=930, predicted_safety_mib=4),
            ]
        )
        self.assertEqual(report.checked, 2)
        self.assertEqual(report.worst_miss, 930 - copied().minimum_mib)

    def test_records_that_would_not_have_fitted_are_named(self):
        report = forecast(
            [
                copied(id="Хорошая", predicted_mib=1024),
                copied(id="Плохая", predicted_mib=10),
            ]
        )
        self.assertTrue(report.any_short)
        self.assertEqual(report.short, ("Плохая",))

    def test_a_clean_run_names_nobody(self):
        report = forecast([copied(predicted_mib=1024, predicted_safety_mib=4)])
        self.assertFalse(report.any_short)
        self.assertEqual(report.short, ())


class StorageTests(unittest.TestCase):
    """The prediction is stored: it cannot be computed after the fact."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_both_fields_survive_a_round_trip(self):
        record = copied(predicted_mib=1024, predicted_safety_mib=7)
        Store(path=self.path, records=[record]).save()
        loaded = Store.load(self.path).records[0]
        self.assertEqual(loaded.predicted_mib, 1024)
        self.assertEqual(loaded.predicted_safety_mib, 7)
        self.assertEqual(loaded.miss_mib, record.miss_mib)

    def test_an_absent_promise_is_not_written(self):
        """Computable values are not stored, and unfilled ones even less so."""
        stored = copied().to_json()
        self.assertNotIn("predicted_mib", stored)
        self.assertNotIn("predicted_safety_mib", stored)
        self.assertNotIn("miss_mib", stored)
        self.assertNotIn("minimum_mib", stored)

    def test_an_old_file_reads_as_before(self):
        Store(path=self.path, records=[copied()]).save()
        self.assertIsNone(Store.load(self.path).records[0].predicted_mib)


class SlackStorageTests(unittest.TestCase):
    """Both kinds of machine measurements share one file and do not clash."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"
        self.volume = 1024 * MIB - HEADERS_AND_TAIL
        self.point = Record(
            id="Калибровка 1 GiB",
            container_mib=1024,
            mounted_bytes=self.volume,
            empty_free_bytes=self.volume - 18 * MIB,
        )
        self.sample = copied(id="Запас 10 файлов", fileset="tiny")

    def tearDown(self):
        self._dir.cleanup()

    def store(self, *records):
        return Store(path=self.path, calibration=list(records))

    def test_the_key_is_the_set_and_falls_back_to_the_file_count(self):
        self.assertEqual(slack_key(self.sample), "tiny")
        self.assertEqual(slack_key(replace(self.sample, fileset="")), "n=10")

    def test_the_two_kinds_are_told_apart_without_a_new_field(self):
        self.assertTrue(self.point.is_calibration_point)
        self.assertFalse(self.sample.is_calibration_point)

    def test_the_store_hands_them_out_separately(self):
        store = self.store(self.point, self.sample)
        self.assertEqual(store.calibration_points(), [self.point])
        self.assertEqual(store.slack_measurements(), [self.sample])

    def test_a_new_point_replaces_only_a_point(self):
        """One mounted_bytes key would knock out the slack measurement too."""
        store = self.store(self.point, replace(self.sample, mounted_bytes=self.volume))
        store.put_calibration(replace(self.point, id="Пересняли"))
        self.assertEqual(len(store.calibration_points()), 1)
        self.assertEqual(store.calibration_points()[0].id, "Пересняли")
        self.assertEqual(len(store.slack_measurements()), 1)

    def test_a_new_sample_replaces_only_a_sample_of_the_same_set(self):
        store = self.store(self.point, self.sample)
        store.put_calibration(replace(self.sample, id="Пересняли"))
        self.assertEqual(len(store.slack_measurements()), 1)
        self.assertEqual(store.slack_measurements()[0].id, "Пересняли")
        self.assertEqual(len(store.calibration_points()), 1)

    def test_a_sample_of_another_set_lands_beside(self):
        store = self.store(self.sample)
        store.put_calibration(replace(self.sample, id="Другой", fileset="fat"))
        self.assertEqual(len(store.slack_measurements()), 2)

    def test_two_sets_with_the_same_file_count_both_survive(self):
        """Comparing them is what backs "slack does not depend on file size".

        On the first real run a key by file count silently let the measurement
        of the "One file 4 GiB" set eat that of the "One file 64 MiB" set —
        together with its NTFS point.
        """
        store = self.store(replace(self.sample, fileset="one", file_count=1))
        store.put_calibration(
            replace(self.sample, id="Большой", fileset="huge", file_count=1)
        )
        self.assertEqual(
            sorted(item.fileset for item in store.slack_measurements()),
            ["huge", "one"],
        )

    def test_hand_made_samples_still_go_by_file_count(self):
        """Two hand-made measurements at one n measure the same thing twice."""
        hand = replace(self.sample, fileset="")
        store = self.store(hand)
        store.put_calibration(replace(hand, id="Пересняли"))
        self.assertEqual(len(store.slack_measurements()), 1)

    def test_a_slack_measurement_feeds_both_models(self):
        """The empty volume is measured before the files: a free NTFS point."""
        store = self.store(self.sample)
        ntfs, slack = store.models()
        self.assertIn(self.sample.volume_bytes, dict(ntfs.points))
        self.assertIn(
            (self.sample.file_count, self.sample.copy_slack_measured),
            slack_samples(store.all_for_model()),
        )

    def test_it_survives_a_round_trip(self):
        self.store(self.point, self.sample).save()
        loaded = Store.load(self.path)
        self.assertEqual(len(loaded.calibration_points()), 1)
        self.assertEqual(len(loaded.slack_measurements()), 1)
        self.assertEqual(loaded.slack_measurements()[0].fileset, "tiny")


class FactorySlackTests(unittest.TestCase):
    """Factory copy-slack measurements, modelled on the factory points."""

    def sample(self, count=500) -> FactorySample:
        volume = 1024 * MIB - HEADERS_AND_TAIL
        return FactorySample(
            fileset="small-500",
            title="500 файлов по 1 KiB",
            container_mib=1024,
            cluster_bytes=4096,
            mounted_bytes=volume,
            empty_free_bytes=volume - 18 * MIB,
            file_bytes=count * 1024,
            file_count=count,
            file_alloc_bytes=count * 4096,
            left_bytes=volume - 18 * MIB - count * 4096 - 300_000,
        )

    def test_the_shipped_samples_cover_several_file_counts(self):
        """That is why they were taken: with one n there is no slope at all."""
        counts = {sample.file_count for sample in factory_data().samples}
        self.assertGreaterEqual(len(counts), 4)

    def test_the_shipped_samples_are_plausible(self):
        """Broken numbers would reach the model disguised as measurements."""
        for sample in factory_data().samples:
            with self.subTest(sample.fileset):
                self.assertGreater(sample.copy_slack_bytes, 0)
                self.assertGreater(sample.mounted_bytes, sample.empty_free_bytes)
                self.assertGreater(sample.empty_free_bytes, sample.left_bytes)
                self.assertFalse(factory_slack_record(sample).is_calibration_point)

    def test_two_shipped_sets_share_a_file_count_but_not_a_size(self):
        """Checks "slack depends on the file count, not on the file size"."""
        singles = [s for s in factory_data().samples if s.file_count == 1]
        self.assertEqual(len(singles), 2)
        self.assertEqual(len({s.file_bytes for s in singles}), 2)
        self.assertEqual(len({s.copy_slack_bytes for s in singles}), 1)

    def test_a_sample_becomes_a_usable_record(self):
        record = factory_slack_record(self.sample())
        self.assertFalse(record.is_calibration_point)
        self.assertEqual(record.copy_slack_measured, 300_000)
        self.assertEqual(record.file_count, 500)

    def test_the_record_names_the_set(self):
        self.assertIn("500 файлов", factory_slack_record(self.sample()).id)

    def test_the_derived_slack_matches_the_sample(self):
        sample = self.sample()
        self.assertEqual(
            sample.copy_slack_bytes,
            factory_slack_record(sample).copy_slack_measured,
        )


if __name__ == "__main__":
    unittest.main()
