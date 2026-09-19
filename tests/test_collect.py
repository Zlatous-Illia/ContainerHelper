"""Automatic collection: step order, the self-check and cleaning up after."""

import tempfile
import unittest
from pathlib import Path

from containerhelper.collect import (
    SELF_CHECK_MIB,
    SELF_CHECK_TOLERANCE,
    Collector,
    Measurement,
    Step,
    measure,
    plan,
    self_check_verdict,
)
from containerhelper.model import MIB, VC_HEADER_BYTES
from containerhelper.veracrypt import (
    VeraCrypt,
    VeraCryptError,
    container_name,
    install_at,
)

from tests.test_veracrypt import Fake, make_install

#: Tiny sizes: the step does not care what size it is, and there is no reason
#: to create gigabyte files in the test suite.
TINY = (1, 2, 4)


def measurement(mib, ntfs):
    volume = mib * MIB - VC_HEADER_BYTES
    return Measurement(
        container_mib=mib,
        mounted_bytes=volume,
        empty_free_bytes=volume - ntfs,
        cluster_bytes=4096,
        filesystem="NTFS",
    )


class PlanTests(unittest.TestCase):
    def test_the_self_check_goes_first(self):
        """Finding out that dynamic can't be trusted must take seconds."""
        steps = plan((512, 2048))
        self.assertTrue(steps[0].self_check)
        self.assertTrue(steps[1].self_check)
        self.assertFalse(steps[2].self_check)

    def test_the_self_check_is_one_size_measured_two_ways(self):
        fast, slow = plan(())[:2]
        self.assertEqual(fast.container_mib, SELF_CHECK_MIB)
        self.assertEqual(slow.container_mib, SELF_CHECK_MIB)
        self.assertEqual((fast.dynamic, fast.quick), (True, True))
        self.assertEqual((slow.dynamic, slow.quick), (False, False))

    def test_the_self_check_size_is_not_measured_a_third_time(self):
        sizes = [step.container_mib for step in plan((512, SELF_CHECK_MIB, 2048))]
        self.assertEqual(sizes.count(SELF_CHECK_MIB), 2)

    def test_without_the_self_check_that_size_is_an_ordinary_step(self):
        steps = plan((SELF_CHECK_MIB, 2048), self_check=False)
        self.assertEqual([step.container_mib for step in steps], [SELF_CHECK_MIB, 2048])
        self.assertFalse(any(step.self_check for step in steps))

    def test_covered_sizes_are_skipped(self):
        steps = plan((512, 2048, 4096), covered=(2048,), self_check=False)
        self.assertEqual([step.container_mib for step in steps], [512, 4096])

    def test_everything_covered_leaves_nothing_to_do(self):
        self.assertEqual(plan((512,), covered=(512,), self_check=False), [])

    def test_ordinary_steps_are_dynamic(self):
        """Otherwise a terabyte container would need a terabyte free."""
        step = plan((2048,), self_check=False)[0]
        self.assertTrue(step.dynamic)
        self.assertTrue(step.quick)

    def test_the_title_says_the_size_and_the_method(self):
        fast, slow = plan(())[:2]
        self.assertIn("1 GiB", fast.title)
        self.assertIn("динамический", fast.title)
        self.assertIn("обычный", slow.title)


class SelfCheckTests(unittest.TestCase):
    def test_equal_measurements_pass(self):
        same = measurement(SELF_CHECK_MIB, 17_879_040)
        self.assertEqual(self_check_verdict(same, same), "")

    def test_a_small_difference_still_passes(self):
        """On overlapping sizes the difference was 8 KiB and 3 KiB."""
        fast = measurement(SELF_CHECK_MIB, 17_879_040)
        slow = measurement(SELF_CHECK_MIB, 17_879_040 + 8 * 1024)
        self.assertEqual(self_check_verdict(fast, slow), "")

    def test_a_real_difference_stops_the_run(self):
        fast = measurement(SELF_CHECK_MIB, 17_879_040)
        slow = measurement(SELF_CHECK_MIB, 17_879_040 + 4 * SELF_CHECK_TOLERANCE)
        verdict = self_check_verdict(fast, slow)
        self.assertIn("не сошлась", verdict)
        self.assertIn(str(fast.ntfs_bytes), verdict)
        self.assertIn(str(slow.ntfs_bytes), verdict)


class MeasurementTests(unittest.TestCase):
    def test_the_record_keeps_only_what_was_measured(self):
        """Nothing computable is stored, or the hand records' error repeats."""
        stored = measurement(1024, 17_879_040).as_record().to_json()
        self.assertNotIn("ntfs_bytes", stored)
        self.assertNotIn("vc_header", stored)
        self.assertEqual(stored["container_mib"], 1024)
        self.assertEqual(stored["filesystem"], "NTFS")

    def test_the_record_is_named_like_a_hand_made_one(self):
        self.assertEqual(measurement(1024, 1).as_record().id, "Калибровка 1 GiB")
        self.assertEqual(measurement(512, 1).as_record().id, "Калибровка 512 MiB")

    def test_it_counts_as_a_calibration_point(self):
        self.assertTrue(measurement(1024, 1).as_record().is_calibration_point)

    def test_the_note_travels_into_the_record(self):
        record = measurement(1024, 1).as_record("Автоматический сбор, VeraCrypt 1.26")
        self.assertIn("VeraCrypt", record.note)


class Fixture(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.workdir = Path(self._dir.name)
        folder = make_install(
            self.workdir / "VeraCrypt", "VeraCrypt Format.exe", "VeraCrypt.exe"
        )
        self.fake = Fake()
        self.vc = VeraCrypt(
            install=install_at(folder),
            run=self.fake.run,
            pause=self.fake.pause,
            volumes=self.fake.volumes(),
        )

    def tearDown(self):
        self._dir.cleanup()

    def containers(self):
        return sorted(path.name for path in self.workdir.glob("*.hc"))


class MeasureTests(Fixture):
    def test_one_step_gives_the_volume_facts(self):
        result = measure(self.vc, self.workdir, Step(1))
        self.assertEqual(result.mounted_bytes, MIB - VC_HEADER_BYTES)
        self.assertEqual(result.cluster_bytes, 4096)
        self.assertEqual(result.filesystem, "NTFS")

    def test_the_container_is_removed_afterwards(self):
        measure(self.vc, self.workdir, Step(1))
        self.assertEqual(self.containers(), [])

    def test_the_volume_is_unmounted_afterwards(self):
        measure(self.vc, self.workdir, Step(1))
        self.assertEqual(self.fake.drives, ["C:"])

    def test_a_failure_still_cleans_up(self):
        """Otherwise terabyte files would pile up silently."""
        self.fake.broken.add("mount")
        with self.assertRaises(VeraCryptError):
            measure(self.vc, self.workdir, Step(1))
        self.assertEqual(self.containers(), [])

    def test_the_size_asked_of_veracrypt_is_exact(self):
        measure(self.vc, self.workdir, Step(2))
        create = self.fake.commands[0]
        self.assertEqual(create[create.index("/size") + 1], str(2 * MIB))


class CollectorTests(Fixture):
    def collector(self, steps):
        return Collector(veracrypt=self.vc, workdir=self.workdir, steps=list(steps))

    def test_orphans_are_swept_before_the_run(self):
        (self.workdir / container_name(1024)).write_bytes(b"")
        removed = self.collector([]).prepare()
        self.assertEqual([path.name for path in removed], [container_name(1024)])
        self.assertEqual(self.containers(), [])

    def test_every_step_yields_a_measurement(self):
        collector = self.collector(Step(mib) for mib in TINY)
        results = [collector.run_step(step) for step in collector.steps]
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(
            [result.measurement.container_mib for result in results], list(TINY)
        )

    def test_a_failed_step_does_not_stop_the_rest(self):
        """One failed size is no reason to drop the other twenty."""
        collector = self.collector([Step(1)])
        self.fake.broken.add("create")
        result = collector.run_step(collector.steps[0])
        self.assertFalse(result.ok)
        self.assertFalse(result.fatal)

    def test_a_failed_self_check_stops_everything(self):
        collector = self.collector([Step(1, self_check=True)])
        self.fake.broken.add("create")
        self.assertTrue(collector.run_step(collector.steps[0]).fatal)

    def test_matching_self_check_lets_the_run_go_on(self):
        steps = [
            Step(1, dynamic=True, quick=True, self_check=True),
            Step(1, dynamic=False, quick=False, self_check=True),
        ]
        collector = self.collector(steps)
        results = [collector.run_step(step) for step in steps]
        self.assertTrue(all(result.ok for result in results))
        self.assertFalse(any(result.fatal for result in results))

    def test_a_diverging_self_check_is_fatal_but_keeps_the_measurement(self):
        """Real measurement: the second of the pair is the normal container."""
        steps = [
            Step(1, dynamic=True, quick=True, self_check=True),
            Step(1, dynamic=False, quick=False, self_check=True),
        ]
        collector = self.collector(steps)
        collector.run_step(steps[0])
        # The normal container with a full format gave 8 MiB more metadata:
        # on such a machine dynamic containers cannot be trusted.
        self.fake.ntfs += 8 * MIB
        result = collector.run_step(steps[1])
        self.assertTrue(result.fatal)
        self.assertIsNotNone(result.measurement)
        self.assertIn("не сошлась", result.error)


if __name__ == "__main__":
    unittest.main()
