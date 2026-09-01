"""Автоматический сбор запаса на копирование: план, место, один шаг.

Отдельно от test_collect.py: там замер пустого тома, здесь — заполненного.
Общее у них только то, что VeraCrypt в обоих подменён.
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
    NtfsModel,
    solve_container_mib,
)
from containerhelper.veracrypt import VeraCrypt, VeraCryptError, install_at

from tests.test_veracrypt import Fake, make_install

GIB = 1024 * MIB

#: Крохотные наборы: шагу всё равно, какой он, а гигабайты в наборе тестов
#: писать незачем. Разные по объёму — на этом проверяется порядок плана.
TINY_SET = FileSet("tiny", "10 файлов по 1 KiB", (Group(10, KIB),))
FAT_SET = FileSet("fat", "4 файла по 1 MiB", (Group(4, MIB),))


def factory_model() -> NtfsModel:
    """Модель на заводских точках — та же, что собирает Store."""
    return NtfsModel(
        [(point.mounted_bytes, point.ntfs_bytes) for point in factory_data().points]
    )


class SlackPlanTests(unittest.TestCase):
    """Что программа обещает под набор и какой контейнер под него делает."""

    def test_the_promise_is_kept_separately_from_the_container(self):
        """Контейнер крупнее обещанного намеренно: промахнись модель вниз,
        набор не влез бы, и вместо замера вышла бы неудача."""
        step = slack_step(TINY_SET)
        self.assertEqual(step.container_mib, step.predicted_mib + SLACK_CUSHION_MIB)

    def test_the_promise_matches_what_the_calc_tab_would_say(self):
        ntfs, slack = NtfsModel(), CopySlackModel()
        step = slack_step(TINY_SET, ntfs, slack)
        expected = solve_container_mib(TINY_SET.payload(4096), ntfs=ntfs, slack=slack)
        self.assertEqual(step.predicted_mib, expected.container_mib)

    def test_the_safety_it_promised_is_remembered_too(self):
        """Без него промах не разложить на погрешность и намеренный запас."""
        self.assertGreater(slack_step(TINY_SET).predicted_safety_mib, 0)

    def test_the_container_avoids_the_coverage_table_sizes(self):
        """Иначе «К заводскому» в той строке отключил бы и замер запаса."""
        plain = slack_step(TINY_SET)
        nudged = slack_step(TINY_SET, forbidden=(plain.container_mib,))
        self.assertNotEqual(nudged.container_mib, plain.container_mib)

    def test_cheap_sets_go_first(self):
        """Одна нехватка места не должна отменять то, что прекрасно влезло бы."""
        steps = slack_plan([FAT_SET, TINY_SET])
        self.assertEqual([step.fileset.key for step in steps], ["tiny", "fat"])

    def test_measured_sets_are_skipped(self):
        steps = slack_plan([TINY_SET, FAT_SET], covered=(TINY_SET.key,))
        self.assertEqual([step.fileset.key for step in steps], ["fat"])

    def test_sets_are_skipped_by_key_not_by_file_count(self):
        """Два набора с n = 1 различаются объёмом, и снимать надо оба."""
        small = FileSet("small-one", "1 файл 1 MiB", (Group(1, MIB),))
        big = FileSet("big-one", "1 файл 8 MiB", (Group(1, 8 * MIB),))
        steps = slack_plan([small, big], covered=(small.key,))
        self.assertEqual([step.fileset.key for step in steps], ["big-one"])

    def test_the_title_names_the_set(self):
        self.assertIn(TINY_SET.title, slack_step(TINY_SET).title)


class SpaceTests(unittest.TestCase):
    """Динамический контейнер стоит метаданных, а не своего размера."""

    def test_a_terabyte_of_empty_volume_costs_megabytes(self):
        """Не терабайта: динамический контейнер ложится на диск метаданными.

        Считается по заводским точкам — то есть ровно так, как в живой
        программе: Store всегда подмешивает их в модель.
        """
        self.assertLess(required_bytes(Step(1024 * 1024), factory_model()), 512 * MIB)

    def test_an_uncalibrated_model_only_overestimates(self):
        """Ошибиться тут можно только в сторону лишней осторожности.

        Модель по умолчанию завышает метаданные терабайта в тринадцать раз, и
        шаг был бы пропущен зря — но не начат на диске, где ему не хватит.
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
        """Вес — про работу, требование — про осторожность."""
        step = Step(2048)
        self.assertGreaterEqual(
            required_bytes(step) - disk_bytes(step), SPACE_MARGIN_BYTES
        )

    def test_creating_files_adds_to_the_weight_but_not_to_the_space(self):
        """Иначе десять тысяч мелких файлов полоса проскакивала бы мгновенно.

        И наоборот: приписать эту поправку требуемому месту значило бы зря
        пропускать шаги, которые прекрасно помещаются.
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
    """Доля шага считается тем же весом, каким взвешен весь план."""

    def test_outside_writing_the_share_is_zero(self):
        """Сколько байт VeraCrypt уложил при создании, снаружи не видно."""
        self.assertEqual(Progress(PHASE_CREATE).share, 0.0)

    def test_bytes_and_files_count_together(self):
        """Порознь врут обе: одна стоит на мелких файлах, другая на крупном."""
        half = Progress(
            PHASE_WRITE,
            files_done=5,
            files_total=10,
            bytes_done=50,
            bytes_total=100,
        )
        self.assertAlmostEqual(half.share, 0.5)

    def test_a_set_of_small_files_still_reaches_the_end(self):
        """Логический объём мелких файлов вдвадцатеро меньше кластерного."""
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
        """Набор из одного файла — одна граница файла на минуты записи.

        Счётчик файлов на нём стоит неподвижно всю запись, и по нему нельзя
        отличить работающую программу от повисшей.
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
        """Место оценивает та самая модель, которую сбор и калибрует."""
        volume = 2048 * MIB - VC_HEADER_BYTES
        model = NtfsModel([(volume, 3 * MIB), (2 * volume, 4 * MIB)])
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
    """Весь путь замера запаса на подменённом VeraCrypt."""

    def test_the_measured_slack_is_what_the_volume_actually_lost(self):
        result = measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertEqual(
            result.copy_slack_bytes,
            self.fake.slack_base + self.fake.slack_per_file * TINY_SET.file_count,
        )

    def test_the_payload_is_measured_by_walking_the_volume(self):
        """Не по замыслу набора: файл мог лечь не так, как задумано."""
        result = measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertEqual(result.file_count, TINY_SET.file_count)
        self.assertEqual(result.file_alloc_bytes, TINY_SET.alloc_bytes(4096))
        self.assertEqual(result.file_bytes, TINY_SET.logical_bytes)

    def test_the_empty_volume_is_measured_before_the_files_land(self):
        """Один шаг даёт и точку NTFS, и замер запаса."""
        result = measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertEqual(result.ntfs_bytes, self.fake.ntfs)

    def test_the_left_space_is_read_on_a_freshly_mounted_volume(self):
        """Модель предсказывает Left space — то, что покажет VeraCrypt."""
        measure(self.vc, self.workdir, slack_step(TINY_SET))
        mounts = [item for item in self.fake.commands if "/volume" in item]
        self.assertEqual(len(mounts), 2)

    def test_the_record_is_a_slack_sample_and_a_calibration_point_at_once(self):
        record = measure(self.vc, self.workdir, slack_step(TINY_SET)).as_record()
        self.assertFalse(record.is_calibration_point)
        self.assertIsNotNone(record.ntfs_bytes)
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
        """Иначе четыре гигабайта остались бы на диске молча."""

        def boom(_done):
            raise OSError("диск устал")

        with self.assertRaises(OSError):
            measure(self.vc, self.workdir, slack_step(TINY_SET), check=boom)
        self.assertEqual(self.containers(), [])
        self.assertEqual(self.fake.drives, ["C:"])

    def test_a_volume_too_small_for_the_set_is_refused_before_writing(self):
        """Кластер тома читается, а не предполагается: на 65536 набор вырастает."""
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
        """Ровно так сорвались четыре замера первого настоящего прогона.

        Отказ приходил на перемонтировании: том, только что заполненный
        файлами, VeraCrypt не отдавала. Спасал случайный повтор в уборке —
        теперь повтор делается намеренно, и замер доходит до конца.
        """
        self.fake.stubborn = 2
        result = measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertIsNotNone(result.left_bytes)
        self.assertEqual(result.file_count, TINY_SET.file_count)
        self.assertEqual(self.containers(), [])
        self.assertEqual(self.fake.drives, ["C:"])

    def test_a_volume_that_never_lets_go_fails_the_step_but_cleans_up(self):
        """Уборка берёт том силой: контейнер всё равно удаляется."""
        self.fake.stubborn = 99
        with self.assertRaises(VeraCryptError):
            measure(self.vc, self.workdir, slack_step(TINY_SET))
        self.assertEqual(self.containers(), [])
        self.assertEqual(self.fake.drives, ["C:"])

    def test_two_sets_of_the_same_size_do_not_share_a_container_file(self):
        """Первый ещё не удалён, второй уже создаётся — VeraCrypt молча откажет."""
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
    """Шаг, которому не хватает места, пропускается, а не считается неудачей."""

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
        """Запись в динамический контейнер на кончившемся диске рвёт том."""
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
