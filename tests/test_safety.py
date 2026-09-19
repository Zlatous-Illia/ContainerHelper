"""Tests of the safety margin: it must depend on size, not be one number.

The data are twelve empty-container points from 1 to 100 GiB. On them the
metadata curve is convex up to 16 GiB and concave above, and exactly this
split must show through in the advice.
"""

import unittest

from containerhelper.model import (
    DEFAULT_SAFETY_BYTES,
    MIB,
    MIN_SAFETY_BYTES,
    NtfsModel,
    SafetyModel,
)
from containerhelper.records import Record, build_safety

GIB = 1024**3

#: (container size in MiB, free on the empty volume) — taken from real
#: containers, the same numbers as in the working records file.
GRID = (
    (1024, 1_055_596_544),
    (2048, 2_127_495_168),
    (4096, 4_270_129_152),
    (8192, 8_555_397_120),
    (16384, 17_125_949_440),
    (20480, 21_413_314_560),
    (24576, 25_702_776_832),
    (32768, 34_281_717_760),
    (40960, 42_860_658_688),
    (49152, 51_440_582_656),
    (65536, 68_619_722_752),
    (102400, 107_273_248_768),
)


def grid_records():
    return [
        Record(
            id=f"Test {mib // 1024} GiB",
            container_mib=mib,
            mounted_bytes=mib * MIB - 266_240,
            empty_free_bytes=free,
        )
        for mib, free in GRID
    ]


def volume_of(mib):
    return mib * MIB - 266_240


class InterpolationBoundTests(unittest.TestCase):
    """How far the model can underestimate between two measurements."""

    def setUp(self):
        self.model = NtfsModel(
            [(r.mounted_bytes, r.ntfs_bytes) for r in grid_records()]
        )

    def test_measured_points_have_nothing_to_interpolate(self):
        for record in grid_records():
            with self.subTest(record.id):
                self.assertEqual(
                    self.model.interpolation_bound(record.mounted_bytes), 0
                )

    def test_convex_stretch_cannot_be_underestimated(self):
        """Below 8 GiB the slope grows, and the chord runs above the curve."""
        for gib in (1.5, 3, 6):
            with self.subTest(gib=gib):
                self.assertEqual(self.model.interpolation_bound(int(gib * GIB)), 0)

    def test_almost_straight_stretch_costs_almost_nothing(self):
        """8–16 GiB is not perfectly linear, but the gap there is kilobytes."""
        self.assertLess(self.model.interpolation_bound(int(12 * GIB)), 64 * 1024)

    def test_concave_stretch_has_a_real_gap(self):
        """Above 16 GiB the metadata components hit their ceilings."""
        self.assertGreater(self.model.interpolation_bound(int(18 * GIB)), 0)
        self.assertGreater(self.model.interpolation_bound(int(47 * GIB)), 0)

    def test_gap_stays_small_on_the_measured_grid(self):
        worst = max(
            self.model.interpolation_bound(int(gib * GIB / 4))
            for gib in range(4, 400)
        )
        self.assertLess(worst, 2 * MIB)

    def test_outside_the_range_it_defers(self):
        self.assertEqual(self.model.interpolation_bound(int(0.5 * GIB)), 0)
        self.assertEqual(self.model.interpolation_bound(int(200 * GIB)), 0)

    def test_uncalibrated_model_has_no_bound(self):
        self.assertEqual(NtfsModel().interpolation_bound(10 * GIB), 0)


class AdviceTests(unittest.TestCase):
    def setUp(self):
        self.safety = build_safety(grid_records())

    def advise(self, mib, files=1):
        return self.safety.advise(volume_of(mib), files)

    def test_advice_varies_with_size(self):
        """The main property: this is not one number for every calculation."""
        totals = {self.advise(mib).total_mib for mib in (1024, 8192, 18432, 49152)}
        self.assertGreater(len(totals), 1)

    def test_measured_size_needs_only_the_floor(self):
        """At a measurement the model is exact: no error to charge it with."""
        advice = self.advise(40960)
        self.assertEqual(advice.ntfs_bytes, 0)
        self.assertEqual(advice.total_bytes, MIN_SAFETY_BYTES)

    def test_gap_between_measurements_costs_more_than_a_measured_point(self):
        self.assertGreater(
            self.advise(18432).ntfs_bytes, self.advise(16384).ntfs_bytes
        )

    def test_far_neighbours_do_not_leak_in(self):
        """The knee at 48 GiB must not make a 5 GiB container dearer."""
        near_the_knee = self.safety.advise(int(56 * GIB), 1).ntfs_bytes
        small = self.safety.advise(int(5 * GIB), 1).ntfs_bytes
        self.assertLess(small, MIB // 2)
        self.assertGreater(near_the_knee, small)

    def test_flat_stretch_does_not_inherit_the_knee(self):
        """Above 64 GiB, by the measurements, only the bitmap grows.

        The miss at 48 GiB, where the curve has a knee, must not get there: the
        window of "similar size" is the ends of its own segment, not everything
        within a factor of two.
        """
        self.assertLess(self.safety.advise(int(90 * GIB), 1).ntfs_bytes, MIB)

    def test_advice_names_the_records_it_leaned_on(self):
        advice = self.advise(18432)
        self.assertTrue(advice.basis)
        self.assertIn("Test 16 GiB", advice.basis)

    def test_outside_the_measured_range_it_gets_careful(self):
        far = self.advise(153600)
        near = self.advise(40960)
        self.assertGreater(far.total_bytes, near.total_bytes)
        self.assertIn("вне измеренного диапазона", far.ntfs_reason)

    def test_many_files_cost_more_than_one(self):
        """Nothing confirms the per-file slack — that has to be visible."""
        one = self.advise(40960, files=1)
        many = self.advise(40960, files=500_000)
        self.assertGreater(many.slack_bytes, one.slack_bytes)
        self.assertEqual(many.slack_bytes, DEFAULT_SAFETY_BYTES)

    def test_advice_is_whole_mib(self):
        for mib in (1024, 18432, 49152, 153600):
            with self.subTest(mib=mib):
                self.assertEqual(self.advise(mib).total_bytes % MIB, 0)

    def test_advice_never_drops_to_zero(self):
        for mib in (1024, 40960, 102400):
            with self.subTest(mib=mib):
                self.assertGreaterEqual(self.advise(mib).total_bytes, MIN_SAFETY_BYTES)


class ScalingTests(unittest.TestCase):
    """A miss is scaled to the relevant segment's width, not taken as is."""

    def setUp(self):
        self.safety = build_safety(grid_records())

    def test_deviation_shrinks_when_the_real_gap_is_narrower(self):
        """The leave-one-out check sees a gap twice as wide as the real one.

        Taking its miss as is would be exactly the single "largest miss" we
        are moving away from: it does not shrink as measurements are added.
        """
        raw = max(
            deviation for _, deviation, _ in self.safety.ntfs_deviations
        )
        worst_advice = max(
            self.safety.advise(int(gib * GIB / 2), 1).ntfs_bytes
            for gib in range(4, 200)
        )
        self.assertGreater(raw, 4 * MIB)
        self.assertLess(worst_advice, raw / 2)

    def test_advice_leans_on_the_two_bounding_records(self):
        advice = self.safety.advise(int(18 * GIB), 1)
        self.assertEqual(set(advice.basis), {"Test 16 GiB", "Test 20 GiB"})

    def test_overshoot_is_not_a_risk(self):
        """A negative deviation is overestimate and adds no safety margin."""
        model = SafetyModel(
            ntfs=NtfsModel([(r.mounted_bytes, r.ntfs_bytes) for r in grid_records()]),
            ntfs_deviations=[
                (r.mounted_bytes, -50 * MIB, r.id) for r in grid_records()
            ],
        )
        self.assertEqual(model.advise(int(18 * GIB), 1).ntfs_bytes, 
                         model.ntfs.interpolation_bound(int(18 * GIB)))


class FallbackTests(unittest.TestCase):
    def test_without_records_it_falls_back_to_the_default(self):
        advice = SafetyModel().advise(10 * GIB, 1)
        self.assertEqual(advice.ntfs_bytes, DEFAULT_SAFETY_BYTES)
        self.assertIn("не откалибрована", advice.ntfs_reason)

    def test_one_file_trusts_the_measured_constant_part(self):
        advice = SafetyModel().advise(10 * GIB, 1)
        self.assertEqual(advice.slack_bytes, MIN_SAFETY_BYTES)

    def test_a_folder_does_not(self):
        advice = SafetyModel().advise(10 * GIB, 5000)
        self.assertEqual(advice.slack_bytes, DEFAULT_SAFETY_BYTES)


if __name__ == "__main__":
    unittest.main()
