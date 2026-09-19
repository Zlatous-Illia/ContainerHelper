"""Leave-one-out check of the models: the record being checked is excluded."""

import unittest
from dataclasses import replace

from containerhelper.model import MIB, NtfsModel
from containerhelper.records import (
    Record,
    ntfs_cross_check,
    slack_cross_check,
    worst_shortfall,
)
from tests import reference


class NtfsCrossCheckTests(unittest.TestCase):
    def test_every_usable_record_is_checked(self):
        checks = ntfs_cross_check(reference.ALL)
        self.assertEqual(len(checks), 3)
        self.assertEqual(
            {check.record.id for check in checks}, {"Cache 1", "Cache 2", "Cache 4"}
        )

    def test_prediction_ignores_the_record_being_checked(self):
        """Else the piecewise-linear model would hit exactly: deviation 0."""
        checks = ntfs_cross_check(reference.ALL)
        self.assertTrue(all(check.deviation != 0 for check in checks))

    def test_measured_values_are_the_real_ones(self):
        for check in ntfs_cross_check(reference.ALL):
            with self.subTest(check.record.id):
                self.assertEqual(
                    check.measured, reference.EXPECTED_NTFS[check.record.id]
                )

    def test_two_records_fall_back_to_the_default_model(self):
        """With one of two removed, there is nothing left to calibrate on."""
        checks = ntfs_cross_check([reference.CACHE_1, reference.CACHE_4])
        self.assertEqual(len(checks), 2)
        self.assertTrue(all(not check.calibrated for check in checks))

    def test_three_records_leave_enough_to_calibrate(self):
        checks = ntfs_cross_check(reference.ALL)
        self.assertTrue(all(check.calibrated for check in checks))

    def test_shortfall_stays_within_the_default_safety_margin(self):
        """An underestimate must be covered by the 4 MiB safety margin."""
        self.assertLess(worst_shortfall(ntfs_cross_check(reference.ALL)), 4 * MIB)

    def test_records_without_measurements_are_skipped(self):
        partial = Record(id="probe", container_mib=1024)
        self.assertEqual(ntfs_cross_check([partial]), [])

    def test_flagged_records_are_skipped(self):
        flagged = replace(reference.CACHE_1, flagged=True)
        checks = ntfs_cross_check([flagged, reference.CACHE_4])
        self.assertEqual([check.record.id for check in checks], ["Cache 4"])

    def test_deviation_sign_means_underestimation(self):
        """A positive deviation means the model underestimated."""
        low = NtfsModel([(8 * 1024**3, 1 * MIB), (12 * 1024**3, 1 * MIB)])
        self.assertLess(low.overhead(reference.CACHE_1.mounted_bytes), 40_316_928)
        check = ntfs_cross_check(reference.ALL)[0]
        self.assertEqual(check.deviation, check.measured - check.predicted)


class SlackCrossCheckTests(unittest.TestCase):
    def test_only_records_with_copy_data_are_checked(self):
        checks = slack_cross_check(reference.ALL)
        self.assertEqual({check.record.id for check in checks}, {"Cache 1", "Cache 4"})

    def test_measured_values_are_the_real_ones(self):
        for check in slack_cross_check(reference.ALL):
            with self.subTest(check.record.id):
                self.assertEqual(
                    check.measured, reference.EXPECTED_SLACK[check.record.id]
                )

    def test_prediction_uses_the_other_record_only(self):
        """Cache 1 is predicted from Cache 4 and vice versa."""
        checks = {check.record.id: check for check in slack_cross_check(reference.ALL)}
        # A base from one record: measured minus the per-file slack, plus that
        # same per-file slack.
        self.assertEqual(checks["Cache 1"].predicted, reference.EXPECTED_SLACK["Cache 4"])
        self.assertEqual(checks["Cache 4"].predicted, reference.EXPECTED_SLACK["Cache 1"])

    def test_shortfall_is_small(self):
        shortfall = worst_shortfall(slack_cross_check(reference.ALL))
        self.assertLess(shortfall, MIB)

    def test_empty_input(self):
        self.assertEqual(slack_cross_check([]), [])
        self.assertEqual(worst_shortfall([]), 0)


if __name__ == "__main__":
    unittest.main()
