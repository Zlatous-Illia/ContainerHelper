"""Automatic collection of copy slack: the plan, the space, one step.

Kept apart from test_collect.py: that one measures an empty volume, this one
a filled one. All they share is that VeraCrypt is faked in both.
"""

import tempfile
import unittest
from pathlib import Path

from containerhelper.collect import (
    PHASE_CLEANUP,
    PHASE_CREATE,
    PHASE_EMPTY,
    PHASE_LEFT,
    PHASE_MOUNT,
    PHASE_REMOUNT,
    PHASE_VERIFY,
    PHASE_WRITE,
    FILE_WEIGHT_BYTES,
    SLACK_CUSHION_MIB,
    SPACE_MARGIN_BYTES,
    Collector,
    Progress,
    Step,
    disk_bytes,
    measure,
    required_bytes,
    slack_plan,
    slack_step,
    total_bytes,
    weight_bytes,
)
from containerhelper.factory import factory_data
from containerhelper.fileset import KIB, FileSet, Group
from containerhelper.model import (
    MIB,
    VC_HEADER_BYTES,
    CopySlackModel,
    MetadataModel,
    solve_container_mib,
)
from containerhelper.veracrypt import VeraCrypt, VeraCryptError, install_at

from tests.test_veracrypt import Fake, make_install

GIB = 1024 * MIB

#: Tiny file sets: the step does not care which one it gets, and there is no
#: point writing gigabytes in the test suite. They differ in size, and that is
#: what the plan order is checked on.
TINY_SET = FileSet("tiny", "10 файлов по 1 KiB", (Group(10, KIB),))
FAT_SET = FileSet("fat", "4 файла по 1 MiB", (Group(4, MIB),))


def factory_model() -> MetadataModel:
    """A model on the factory points, the same one Store builds."""
    return MetadataModel(
        [(point.mounted_bytes, point.metadata_bytes) for point in factory_data().points]
    )


class SlackPlanTests(unittest.TestCase):
    """What the program promises for a set and which container it makes."""

    def test_the_promise_is_kept_separately_from_the_container(self):
        """The container is larger than promised on purpose: should the model
        miss low, the set would not fit, and a failure would replace the
        measurement."""
        step = slack_step(TINY_SET)
        self.assertEqual(step.container_mib, step.predicted_mib + SLACK_CUSHION_MIB)

    def test_the_promise_matches_what_the_calc_tab_would_say(self):
        ntfs, slack = MetadataModel(), CopySlackModel()
        step = slack_step(TINY_SET, ntfs, slack)
        expected = solve_container_mib(TINY_SET.payload(4096), ntfs=ntfs, slack=slack)
        self.assertEqual(step.predicted_mib, expected.container_mib)

    def test_the_safety_it_promised_is_remembered_too(self):
        """Without it a miss cannot be split into error and intended margin."""
        self.assertGreater(slack_step(TINY_SET).predicted_safety_mib, 0)

    def test_the_container_avoids_the_coverage_table_sizes(self):
        """Otherwise the Use factory button in that row would disable the
        copy-slack measurement too."""
        plain = slack_step(TINY_SET)
        nudged = slack_step(TINY_SET, forbidden=(plain.container_mib,))
        self.assertNotEqual(nudged.container_mib, plain.container_mib)

    def test_cheap_sets_go_first(self):
        """One shortage of space must not cancel what would fit just fine."""
        steps = slack_plan([FAT_SET, TINY_SET])
        self.assertEqual([step.fileset.key for step in steps], ["tiny", "fat"])

    def test_measured_sets_are_skipped(self):
        steps = slack_plan([TINY_SET, FAT_SET], covered=(TINY_SET.key,))
        self.assertEqual([step.fileset.key for step in steps], ["fat"])

    def test_sets_are_skipped_by_key_not_by_file_count(self):
        """Two sets with n = 1 differ in size, and both must be measured."""
        small = FileSet("small-one", "1 файл 1 MiB", (Group(1, MIB),))
        big = FileSet("big-one", "1 файл 8 MiB", (Group(1, 8 * MIB),))
        steps = slack_plan([small, big], covered=(small.key,))
        self.assertEqual([step.fileset.key for step in steps], ["big-one"])

    def test_the_title_names_the_set(self):
        self.assertIn(TINY_SET.title, slack_step(TINY_SET).title)


class SpaceTests(unittest.TestCase):
    """A dynamic container costs its metadata, not its size."""

    def test_a_terabyte_of_empty_volume_costs_megabytes(self):
        """Not a terabyte: a dynamic container lands on disk as its metadata.

        Computed from the factory points, that is, exactly as in the live
        program: Store always mixes them into the model.
        """
        self.assertLess(required_bytes(Step(1024 * 1024), factory_model()), 512 * MIB)

    def test_an_uncalibrated_model_only_overestimates(self):
        """The only possible mistake here is too much caution.

        The default model overestimates the metadata of a terabyte thirteen
        times over, and a step would be skipped needlessly, but never started
        on a disk where it will not fit.
        """
        self.assertGreater(
            required_bytes(Step(1024 * 1024)),
            required_bytes(Step(1024 * 1024), factory_model()),
        )

    def test_a_full_format_costs_the_whole_container(self):
        step = Step(1024, dynamic=False, quick=False)
        self.assertEqual(weight_bytes(step), 1024 * MIB)
        self.assertGreater(required_bytes(step), 1024 * MIB)

    def test_a_slack_step_costs_its_payload_as_well(self):
        empty = required_bytes(Step(200))
        with_files = required_bytes(Step(200, fileset=FAT_SET))
        self.assertGreaterEqual(with_files - empty, FAT_SET.alloc_bytes(4096))

    def test_the_margin_is_only_in_the_requirement(self):
        """Weight is about work, the requirement is about caution."""
        step = Step(2048)
        self.assertGreaterEqual(
            required_bytes(step) - disk_bytes(step), SPACE_MARGIN_BYTES
        )

    def test_creating_files_adds_to_the_weight_but_not_to_the_space(self):
        """Otherwise the bar would flash past ten thousand small files.

        And the other way round: adding this correction to the required space
        would mean needlessly skipping steps that fit perfectly well.
        """
        many = FileSet("many", "many", (Group(10_000, KIB),))
        step = Step(200, fileset=many)
        self.assertEqual(
            weight_bytes(step) - disk_bytes(step),
            FILE_WEIGHT_BYTES * many.file_count,
        )
        self.assertEqual(required_bytes(step), disk_bytes(step) + SPACE_MARGIN_BYTES)

    def test_an_empty_volume_step_weighs_exactly_what_it_writes(self):
        step = Step(2048)
        self.assertEqual(weight_bytes(step), disk_bytes(step))


class ProgressShareTests(unittest.TestCase):
    """A step's share uses the same weight the whole plan is weighed by."""

    def test_outside_writing_the_share_is_zero(self):
        """How much VeraCrypt writes while creating is not visible outside."""
        self.assertEqual(Progress(PHASE_CREATE).share, 0.0)

    def test_bytes_and_files_count_together(self):
        """Alone each lies: one stalls on small files, one on a big file."""
        half = Progress(
            PHASE_WRITE,
            files_done=5,
            files_total=10,
            bytes_done=50,
            bytes_total=100,
        )
        self.assertAlmostEqual(half.share, 0.5)

    def test_a_set_of_small_files_still_reaches_the_end(self):
        """Small files' logical size is 1/4 of their cluster-rounded size."""
        done = Progress(
            PHASE_WRITE,
            files_done=500,
            files_total=500,
            bytes_done=500 * KIB,
            bytes_total=500 * KIB,
        )
        self.assertEqual(done.share, 1.0)

    def test_the_share_never_runs_past_one(self):
        over = Progress(
            PHASE_WRITE, files_done=20, files_total=10, bytes_done=999, bytes_total=1
        )
        self.assertEqual(over.share, 1.0)

    def test_the_detail_names_the_file_counter(self):
        writing = Progress(PHASE_WRITE, files_done=3, files_total=10)
        self.assertEqual(writing.detail, "файлов 3 из 10")
        self.assertEqual(Progress(PHASE_CREATE).detail, "")

    def test_the_detail_names_the_written_volume_too(self):
        """A one-file set has a single file boundary in minutes of writing.

        On it the file counter stands still for the whole write, and it cannot
        tell a working program from a hung one.
        """
        writing = Progress(
            PHASE_WRITE,
            files_done=0,
            files_total=1,
            bytes_done=1024 * 1024 * 1024,
            bytes_total=4 * 1024 * 1024 * 1024,
        )
        self.assertIn("файлов 0 из 1", writing.detail)
        self.assertIn("1.000 GiB из 4.000 GiB", writing.detail)

    def test_the_plan_weight_is_the_sum_of_step_weights(self):
        steps = [Step(1024), Step(2048, fileset=TINY_SET)]
        self.assertEqual(total_bytes(steps), sum(weight_bytes(s) for s in steps))

    def test_a_calibrated_model_sharpens_the_estimate(self):
        """Space is estimated by the very model the collection calibrates."""
        volume = 2048 * MIB - VC_HEADER_BYTES
        model = MetadataModel([(volume, 3 * MIB), (2 * volume, 4 * MIB)])
        self.assertLess(required_bytes(Step(2048), model), required_bytes(Step(2048)))


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


class SlackMeasureTests(Fixture):
    """The whole copy-slack measurement path on a faked VeraCrypt."""

    def test_the_measured_slack_is_what_the_volume_actually_lost(self):
        result = measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertEqual(
            result.copy_slack_bytes,
            self.fake.slack_base + self.fake.slack_per_file * TINY_SET.file_count,
        )

    def test_the_payload_is_measured_by_walking_the_volume(self):
        """Not by the set's design: a file may have landed otherwise."""
        result = measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertEqual(result.file_count, TINY_SET.file_count)
        self.assertEqual(result.file_alloc_bytes, TINY_SET.alloc_bytes(4096))
        self.assertEqual(result.file_bytes, TINY_SET.logical_bytes)

    def test_the_empty_volume_is_measured_before_the_files_land(self):
        """One step gives both an NTFS point and a copy-slack measurement."""
        result = measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertEqual(result.metadata_bytes, self.fake.ntfs)

    def test_the_left_space_is_read_on_a_freshly_mounted_volume(self):
        """The model predicts Left space, which is what VeraCrypt will show."""
        measure(self.vc, self.workdir, slack_step(TINY_SET))
        mounts = [item for item in self.fake.commands if "/volume" in item]
        self.assertEqual(len(mounts), 2)

    def test_the_record_is_a_slack_sample_and_a_calibration_point_at_once(self):
        record = measure(self.vc, self.workdir, slack_step(TINY_SET)).as_record()
        self.assertFalse(record.is_calibration_point)
        self.assertIsNotNone(record.metadata_bytes)
        self.assertIsNotNone(record.copy_slack_measured)
        self.assertEqual(record.fileset, TINY_SET.key)

    def test_the_forecast_travels_into_the_record(self):
        step = slack_step(TINY_SET)
        record = measure(self.vc, self.workdir, step).as_record()
        self.assertEqual(record.predicted_mib, step.predicted_mib)
        self.assertEqual(record.predicted_safety_mib, step.predicted_safety_mib)
        self.assertIsNotNone(record.miss_mib)

    def test_everything_is_cleaned_up_afterwards(self):
        measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertEqual(self.containers(), [])
        self.assertEqual(self.fake.drives, ["C:"])

    def test_a_failure_mid_write_still_cleans_up(self):
        """Otherwise four gigabytes would silently stay on the disk."""

        def boom(_done):
            raise OSError("диск устал")

        with self.assertRaises(OSError):
            measure(self.vc, self.workdir, slack_step(TINY_SET), check=boom)
        self.assertEqual(self.containers(), [])
        self.assertEqual(self.fake.drives, ["C:"])

    def test_a_volume_too_small_for_the_set_is_refused_before_writing(self):
        """The volume cluster is read, not assumed: at 65536 the set grows."""
        self.fake.cluster_bytes = 65536
        with self.assertRaises(VeraCryptError) as caught:
            measure(self.vc, self.workdir, Step(2, fileset=FAT_SET))
        self.assertIn("Замер не начат", str(caught.exception))

    def test_the_phases_are_named_in_order(self):
        seen = []
        measure(
            self.vc,
            self.workdir,
            slack_step(TINY_SET),
            progress=lambda item: seen.append(item.phase),
        )
        self.assertEqual(
            list(dict.fromkeys(seen)),
            [
                PHASE_CREATE,
                PHASE_MOUNT,
                PHASE_EMPTY,
                PHASE_WRITE,
                PHASE_VERIFY,
                PHASE_REMOUNT,
                PHASE_LEFT,
                PHASE_CLEANUP,
            ],
        )

    def test_writing_reports_files_done(self):
        seen = []
        measure(
            self.vc,
            self.workdir,
            slack_step(TINY_SET),
            progress=seen.append,
        )
        writing = [item for item in seen if item.phase == PHASE_WRITE]
        self.assertEqual(writing[-1].files_done, TINY_SET.file_count)
        self.assertEqual(writing[-1].files_total, TINY_SET.file_count)
        self.assertIn("из", writing[-1].detail)

    def test_a_volume_that_refuses_the_first_unmounts_is_still_measured(self):
        """Exactly how four measurements of the first real run failed.

        The refusal came on remounting: VeraCrypt would not release a volume
        just filled with files. An accidental retry in cleanup was what saved
        it; now the retry is deliberate, and the measurement runs to the end.
        """
        self.fake.stubborn = 2
        result = measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertIsNotNone(result.left_bytes)
        self.assertEqual(result.file_count, TINY_SET.file_count)
        self.assertEqual(self.containers(), [])
        self.assertEqual(self.fake.drives, ["C:"])

    def test_a_volume_that_never_lets_go_fails_the_step_but_cleans_up(self):
        """Cleanup forces the volume off: the container is deleted anyway."""
        self.fake.stubborn = 99
        with self.assertRaises(VeraCryptError):
            measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertEqual(self.containers(), [])
        self.assertEqual(self.fake.drives, ["C:"])

    def test_two_sets_of_the_same_size_do_not_share_a_container_file(self):
        """The first is not yet deleted when the second is being created.

        VeraCrypt would refuse silently.
        """
        first = Step(64, fileset=TINY_SET)
        second = Step(64, fileset=FAT_SET)
        for step in (first, second):
            measure(self.vc, self.workdir, step)
        names = {
            Path(item[item.index("/create") + 1]).name
            for item in self.fake.commands
            if "/create" in item
        }
        self.assertEqual(len(names), 2)


class CollectorSpaceTests(Fixture):
    """A step short of space is skipped, not counted as a failure."""

    def collector(self, steps, free):
        return Collector(
            veracrypt=self.vc,
            workdir=self.workdir,
            steps=list(steps),
            free_bytes=free,
        )

    def test_a_step_that_does_not_fit_is_skipped(self):
        step = Step(1024, fileset=FAT_SET)
        result = self.collector([step], free=lambda: MIB).run_step(step)
        self.assertTrue(result.skipped)
        self.assertFalse(result.fatal)
        self.assertIsNone(result.measurement)
        self.assertIn("не хватает места", result.error)

    def test_the_skip_says_how_much_was_needed(self):
        step = Step(1024, fileset=FAT_SET)
        result = self.collector([step], free=lambda: MIB).run_step(step)
        self.assertEqual(result.required_bytes, required_bytes(step))
        self.assertEqual(result.free_bytes, MIB)

    def test_nothing_is_created_for_a_skipped_step(self):
        step = Step(1024, fileset=FAT_SET)
        self.collector([step], free=lambda: MIB).run_step(step)
        self.assertEqual(self.containers(), [])
        self.assertEqual(self.fake.commands, [])

    def test_a_step_that_fits_runs(self):
        step = slack_step(TINY_SET)
        self.assertTrue(self.collector([step], free=lambda: 64 * GIB).run_step(step).ok)

    def test_space_running_out_mid_write_stops_that_step_only(self):
        """Writing to a dynamic container on a full disk breaks the volume."""
        readings = iter([64 * GIB])
        step = slack_step(TINY_SET)
        collector = self.collector([step], free=lambda: next(readings, MIB))
        result = collector.run_step(step)
        self.assertFalse(result.ok)
        self.assertFalse(result.fatal)
        self.assertFalse(result.skipped)
        self.assertEqual(self.containers(), [])
        self.assertEqual(self.fake.drives, ["C:"])

    def test_cancelling_mid_write_is_not_a_failure_of_the_step(self):
        step = slack_step(TINY_SET)
        collector = self.collector([step], free=lambda: 64 * GIB)
        collector.should_stop = lambda: True
        result = collector.run_step(step)
        self.assertIn("остановлено", result.error)
        self.assertIsNone(result.measurement)
        self.assertFalse(result.fatal)
        self.assertEqual(self.containers(), [])


if __name__ == "__main__":
    unittest.main()
