"""Tests of the calculation core on three real records."""

import math
import unittest

from containerhelper.model import (
    DEFAULT_SLACK_PER_FILE,
    DEFAULT_NTFS_RATE,
    MIB,
    VC_HEADER_BYTES,
    CopySlackModel,
    NtfsModel,
    Payload,
    ceil_div,
    round_up,
    solve_container_mib,
)
from containerhelper.records import build_models
from tests import reference


class ArithmeticTests(unittest.TestCase):
    def test_ceil_div_rounds_up(self):
        self.assertEqual(ceil_div(0, 4096), 0)
        self.assertEqual(ceil_div(1, 4096), 1)
        self.assertEqual(ceil_div(4096, 4096), 1)
        self.assertEqual(ceil_div(4097, 4096), 2)

    def test_round_up_to_cluster(self):
        self.assertEqual(round_up(0, 4096), 0)
        self.assertEqual(round_up(1, 4096), 4096)
        self.assertEqual(round_up(4096, 4096), 4096)
        self.assertEqual(round_up(4097, 4096), 8192)


class PayloadTests(unittest.TestCase):
    def test_single_file_rounds_to_cluster(self):
        payload = Payload.for_file(10_941_734_967, 4096)
        self.assertEqual(payload.alloc_bytes, 10_941_739_008)
        self.assertEqual(payload.file_count, 1)
        self.assertEqual(payload.cluster_tail, 4041)

    def test_folder_sums_cluster_sizes_not_logical(self):
        """A naive sum of logical sizes underestimates the result."""
        payload = Payload.for_files([1, 4097, 4096], 4096)
        self.assertEqual(payload.logical_bytes, 8194)
        self.assertEqual(payload.alloc_bytes, 4096 + 8192 + 4096)
        self.assertEqual(payload.file_count, 3)

    def test_empty_folder(self):
        payload = Payload.for_files([], 4096)
        self.assertEqual(payload.alloc_bytes, 0)
        self.assertEqual(payload.file_count, 0)


class DerivedValueTests(unittest.TestCase):
    def test_veracrypt_header_is_constant_across_records(self):
        for record in reference.ALL:
            with self.subTest(record.id):
                self.assertEqual(record.vc_header, VC_HEADER_BYTES)

    def test_ntfs_overhead_matches_measurements(self):
        for record in reference.ALL:
            with self.subTest(record.id):
                self.assertEqual(record.ntfs_bytes, reference.EXPECTED_NTFS[record.id])

    def test_copy_slack_matches_measurements(self):
        for record_id, expected in reference.EXPECTED_SLACK.items():
            record = next(r for r in reference.ALL if r.id == record_id)
            with self.subTest(record_id):
                self.assertEqual(record.copy_slack_measured, expected)

    def test_broken_record_yields_negative_slack(self):
        """Cache 2 uses less than the file itself: the value is meaningless."""
        self.assertLess(reference.CACHE_2.copy_slack_measured, 0)


class NtfsModelTests(unittest.TestCase):
    def test_falls_back_to_affine_without_points(self):
        model = NtfsModel()
        self.assertFalse(model.calibrated)
        self.assertTrue(model.is_extrapolation(10 * 1024**3))

    def test_default_model_never_underestimates_measurements_by_much(self):
        """An underestimate must fit within the 4 MiB safety margin."""
        model = NtfsModel()
        for record in reference.ALL:
            with self.subTest(record.id):
                shortfall = record.ntfs_bytes - model.overhead(record.mounted_bytes)
                self.assertLess(shortfall, 4 * MIB)

    def test_calibrated_model_is_exact_at_measured_points(self):
        points = [(r.mounted_bytes, r.ntfs_bytes) for r in reference.ALL]
        model = NtfsModel(points)
        self.assertTrue(model.calibrated)
        for volume, overhead in points:
            with self.subTest(volume=volume):
                self.assertEqual(model.overhead(volume), overhead)

    def test_interpolates_between_points(self):
        model = NtfsModel([(8 * 1024**3, 32 * MIB), (12 * 1024**3, 40 * MIB)])
        self.assertEqual(model.overhead(10 * 1024**3), 36 * MIB)

    def test_extrapolates_from_the_edge_point_at_the_baseline_rate(self):
        edge = 12 * 1024**3
        model = NtfsModel([(8 * 1024**3, 32 * MIB), (edge, 40 * MIB)])
        far = 60 * 1024**3

        expected = 40 * MIB + math.ceil(DEFAULT_NTFS_RATE * (far - edge))
        self.assertEqual(model.overhead(far), expected)
        self.assertTrue(model.is_extrapolation(far))
        self.assertFalse(model.is_extrapolation(10 * 1024**3))

    def test_extrapolation_ignores_a_steep_local_slope(self):
        """Two close points must not set the slope all the way out."""
        points = [
            (11 * 1024**3, 38 * MIB),
            (11 * 1024**3 + 1024**2, 44 * MIB),  # close by, huge slope
        ]
        model = NtfsModel(points)
        local_slope = 6 * MIB / 1024**2

        far = 60 * 1024**3
        grew = model.overhead(far) - 44 * MIB
        self.assertLess(grew / (far - 11 * 1024**3 - 1024**2), local_slope / 1000)

    def test_downward_extrapolation_stays_within_the_safety_margin(self):
        """Where the leave-one-out check gave a 10.8 MiB underestimate."""
        without_smallest = [
            (reference.CACHE_2.mounted_bytes, reference.CACHE_2.ntfs_bytes),
            (reference.CACHE_1.mounted_bytes, reference.CACHE_1.ntfs_bytes),
        ]
        predicted = NtfsModel(without_smallest).overhead(reference.CACHE_4.mounted_bytes)
        shortfall = reference.CACHE_4.ntfs_bytes - predicted
        self.assertLess(shortfall, 4 * MIB)

    def test_real_measurements_extrapolate_close_to_the_baseline(self):
        """At 60 GiB, calibration on 8–12 GiB must not fly off the baseline."""
        points = [(r.mounted_bytes, r.ntfs_bytes) for r in reference.ALL]
        calibrated = NtfsModel(points).overhead(60 * 1024**3)
        baseline = NtfsModel().overhead(60 * 1024**3)
        self.assertLess(abs(calibrated - baseline), 32 * MIB)

    def test_never_returns_degenerate_value(self):
        """Extrapolating down a steep slope must not go to zero."""
        model = NtfsModel([(1_000_000, 900_000), (2_000_000, 1_800_000)])
        self.assertGreaterEqual(model.overhead(1000), MIB)

    def test_duplicate_volumes_keep_the_larger_overhead(self):
        volume = 8 * 1024**3
        model = NtfsModel(
            [(volume, 32 * MIB), (volume, 34 * MIB), (12 * 1024**3, 40 * MIB)]
        )
        self.assertEqual(model.overhead(volume), 34 * MIB)


class CopySlackModelTests(unittest.TestCase):
    def test_defaults_when_no_samples(self):
        model = CopySlackModel.calibrate([])
        self.assertFalse(model.calibrated)
        self.assertFalse(model.per_file_calibrated)

    def test_single_file_count_keeps_default_per_file(self):
        """With a single value of n the per-file slack cannot be separated."""
        samples = [(1, 143_360), (1, 114_688)]
        model = CopySlackModel.calibrate(samples)
        self.assertEqual(model.per_file, DEFAULT_SLACK_PER_FILE)
        self.assertFalse(model.per_file_calibrated)
        self.assertEqual(model.slack(1), 143_360)

    def test_never_underestimates_any_sample(self):
        samples = [(1, 143_360), (1, 114_688), (500, 900_000), (10_000, 13_000_000)]
        model = CopySlackModel.calibrate(samples)
        self.assertTrue(model.per_file_calibrated)
        for count, measured in samples:
            with self.subTest(n=count):
                self.assertGreaterEqual(model.slack(count), measured)

    def test_negative_slope_is_rejected(self):
        """Falling slack is physically impossible — the default stays."""
        model = CopySlackModel.calibrate([(1, 900_000), (1000, 100_000)])
        self.assertEqual(model.per_file, DEFAULT_SLACK_PER_FILE)


class SolverTests(unittest.TestCase):
    def _solve(self, record):
        payload = Payload.for_file(record.file_bytes, record.cluster_bytes)
        return solve_container_mib(payload)

    def test_result_covers_the_true_minimum(self):
        """The main check: the calculation must never miss downwards."""
        for record_id, minimum in reference.TRUE_MINIMUM_MIB.items():
            record = next(r for r in reference.ALL if r.id == record_id)
            with self.subTest(record_id):
                self.assertGreaterEqual(self._solve(record).container_mib, minimum)

    def test_result_is_not_wasteful(self):
        """Overspend beyond the true minimum is at most 8 MiB."""
        for record_id, minimum in reference.TRUE_MINIMUM_MIB.items():
            record = next(r for r in reference.ALL if r.id == record_id)
            with self.subTest(record_id):
                self.assertLessEqual(self._solve(record).container_mib - minimum, 8)

    def test_known_results(self):
        expected = {"Cache 1": 11061, "Cache 2": 10477, "Cache 4": 8024}
        for record in reference.ALL:
            with self.subTest(record.id):
                self.assertEqual(self._solve(record).container_mib, expected[record.id])

    def test_breakdown_adds_up(self):
        record = reference.CACHE_1
        solution = self._solve(record)
        self.assertEqual(solution.container_bytes, solution.container_mib * MIB)
        self.assertEqual(
            solution.volume_bytes, solution.container_bytes - solution.vc_header
        )
        self.assertEqual(
            solution.predicted_left_bytes,
            solution.volume_bytes
            - solution.ntfs_bytes
            - solution.payload_alloc
            - solution.copy_slack,
        )
        self.assertGreaterEqual(solution.predicted_left_bytes, 0)

    def test_solution_fits_the_payload(self):
        for record in reference.ALL:
            with self.subTest(record.id):
                solution = self._solve(record)
                available = solution.volume_bytes - solution.ntfs_bytes
                self.assertGreaterEqual(
                    available, solution.payload_alloc + solution.copy_slack
                )

    def test_larger_payload_never_yields_smaller_container(self):
        previous = 0
        for gib in range(1, 40):
            payload = Payload.for_file(gib * 1024**3)
            current = solve_container_mib(payload).container_mib
            self.assertGreater(current, previous)
            previous = current

    def test_file_count_increases_the_result(self):
        size = 8 * 1024**3
        one = solve_container_mib(Payload(size, size, 1))
        many = solve_container_mib(Payload(size, size, 50_000))
        self.assertGreater(many.container_mib, one.container_mib)
        self.assertTrue(many.slack_unverified)
        self.assertFalse(one.slack_unverified)

    def test_calibrated_models_still_cover_the_minimum(self):
        ntfs, slack = build_models(reference.ALL)
        for record_id, minimum in reference.TRUE_MINIMUM_MIB.items():
            record = next(r for r in reference.ALL if r.id == record_id)
            with self.subTest(record_id):
                payload = Payload.for_file(record.file_bytes, record.cluster_bytes)
                solution = solve_container_mib(payload, ntfs=ntfs, slack=slack)
                self.assertGreaterEqual(solution.container_mib, minimum)


if __name__ == "__main__":
    unittest.main()
