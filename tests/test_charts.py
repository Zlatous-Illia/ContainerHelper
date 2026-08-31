"""Содержимое графиков: какие точки на них попали и что сказано в подсказке.

Без Qt — сборка графика это данные, а не оформление. Окно для проверки не
нужно, а нужно оно было бы ровно затем, чтобы смотреть глазами.
"""

import tempfile
import unittest
from pathlib import Path

from containerhelper import charts
from containerhelper.factory import factory_data
from containerhelper.model import MIB, Payload, solve_container_mib
from containerhelper.plot import (
    KIND_BARS,
    KIND_DOTS,
    KIND_LINE,
    KIND_LINE_DOTS,
    KIND_STACK,
    KIND_STEPS,
    LAYOUT_STACK,
)
from containerhelper.records import Record, Store

from .reference import CACHE_1, CACHE_4


def empty_store() -> Store:
    return Store.load(Path(tempfile.mkdtemp()) / "Records.json")


def point_at(record: Record) -> Record:
    return record


def series_named(chart, name):
    for item in chart.series:
        if item.name == name:
            return item
    raise AssertionError(f"нет серии «{name}»")


class NtfsCurveTests(unittest.TestCase):
    def test_a_fresh_copy_draws_the_factory_points(self):
        """Новая копия считает по заводским — и показывать должна их же."""
        chart = charts.ntfs_curve(empty_store())
        self.assertEqual(len(series_named(chart, "свои замеры").points), 0)
        self.assertEqual(
            len(series_named(chart, "заводские").points),
            len(factory_data().points) + len(factory_data().samples),
        )

    def test_an_own_measurement_moves_the_point_to_its_own_series(self):
        """Свой замер вытесняет заводской, и на графике это должно быть видно."""
        store = empty_store()
        point = factory_data().points[0]
        store.calibration.append(
            Record(
                id="свой 512 MiB",
                container_mib=point.container_mib,
                mounted_bytes=point.mounted_bytes,
                empty_free_bytes=point.empty_free_bytes,
            )
        )
        chart = charts.ntfs_curve(store)
        mine = series_named(chart, "свои замеры").points
        factory = series_named(chart, "заводские").points
        self.assertEqual([p.tip.splitlines()[0] for p in mine], ["свой 512 MiB"])
        self.assertNotIn(
            float(point.mounted_bytes), [p.x for p in factory]
        )

    def test_the_model_line_is_sampled_not_drawn_through_two_ends(self):
        """Ось логарифмическая: прямой отрезок на ней становится дугой.

        Двумя концами модель прошла бы мимо собственных замеров.
        """
        line = series_named(charts.ntfs_curve(empty_store()), "модель")
        self.assertEqual(line.kind, KIND_LINE)
        self.assertGreater(len(line.points), 100)

    def test_the_tooltip_names_the_record_and_both_numbers(self):
        chart = charts.ntfs_curve(empty_store())
        tip = series_named(chart, "заводские").points[0].tip
        self.assertIn("Том:", tip)
        self.assertIn("Метаданные:", tip)
        self.assertIn(" B · ", tip)


class ResidualTests(unittest.TestCase):
    def test_the_zero_line_is_the_model_itself(self):
        chart = charts.ntfs_residuals(empty_store())
        self.assertTrue(chart.zero_line)

    def test_underestimates_and_overestimates_go_to_different_series(self):
        """Занижение — единственная опасная сторона, и различать надо её."""
        chart = charts.ntfs_residuals(empty_store())
        under = series_named(chart, "модель занизила").points
        over = series_named(chart, "модель завысила").points
        self.assertTrue(all(point.y > 0 for point in under))
        self.assertTrue(all(point.y <= 0 for point in over))
        self.assertTrue(under or over)

    def test_a_residual_is_not_zero_at_its_own_point(self):
        """Ради этого и делается проверка исключением.

        Кусочно-линейная модель проходит ровно через свои замеры: без
        исключения проверяемой точки отклонение везде вышло бы нулевым, и
        график был бы прямой линией на нуле.
        """
        chart = charts.ntfs_residuals(empty_store())
        values = [
            point.y
            for item in chart.series
            for point in item.points
        ]
        self.assertTrue(any(abs(value) > 0 for value in values))


    def test_the_edge_measurements_are_kept_apart_and_hidden(self):
        """Без крайних точек модель экстраполирует, и промах там иной величины.

        На настоящих замерах крайняя даёт −433 MiB против долей мегабайта у
        всех остальных: оставь её в общей серии — и ось растянется так, что
        весь предмет разговора ляжет на нулевую линию.
        """
        chart = charts.ntfs_residuals(empty_store())
        edge = series_named(chart, "край диапазона")
        self.assertFalse(edge.visible)
        self.assertEqual(len(edge.points), 2)

        volumes = [
            point.x
            for item in chart.series
            for point in item.points
        ]
        self.assertEqual(
            {point.x for point in edge.points}, {min(volumes), max(volumes)}
        )

    def test_the_note_says_where_the_hidden_points_went(self):
        """Спрятанное молча — то же самое, что потерянное."""
        note = charts.ntfs_residuals(empty_store()).note
        self.assertIn("Крайние замеры спрятаны", note)
        self.assertIn("легенде", note)


class SlopeTests(unittest.TestCase):
    def test_every_segment_becomes_a_step(self):
        store = empty_store()
        chart = charts.ntfs_slopes(store)
        steps = series_named(chart, "отрезки")
        self.assertEqual(steps.kind, KIND_STEPS)
        # По ступени на отрезок плюс замыкающая точка на последнем замере.
        expected = len(factory_data().points) + len(factory_data().samples)
        self.assertEqual(len(steps.points), expected)

    def test_the_slope_is_a_percentage_of_the_volume(self):
        """В SPEC они и записаны процентами: 0,215 %, 0,128 %, 0,324 %."""
        chart = charts.ntfs_slopes(empty_store())
        values = [point.y for point in series_named(chart, "отрезки").points]
        self.assertTrue(all(0 <= value < 5 for value in values), values[:5])

    def test_the_staircase_shows_up_as_flat_steps(self):
        """Плато с нулевым наклоном — это и есть лестница из SPEC.

        Между 89 и 116 MiB метаданные не растут вовсе, а на следующем отрезке
        прибавляют разом: $LogFile меняется ступенями на дискретных порогах.
        Ради этого график и рисуется — на самой кривой такого не разглядеть.
        """
        chart = charts.ntfs_slopes(empty_store())
        values = [point.y for point in series_named(chart, "отрезки").points]
        self.assertIn(0.0, values)
        self.assertGreater(max(values), 0.5)


class SlackTests(unittest.TestCase):
    def test_the_note_names_the_measured_slope(self):
        chart = charts.slack_curve(empty_store())
        self.assertIn("на файл", chart.note)

    def test_the_axis_counts_files_not_bytes(self):
        chart = charts.slack_curve(empty_store())
        self.assertEqual(chart.x.title, "Файлов")
        self.assertTrue(chart.x.log)


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.store = empty_store()
        first = Record(**{**vars(CACHE_1), "predicted_mib": 11130, "predicted_safety_mib": 4})
        second = Record(**{**vars(CACHE_4), "predicted_mib": 8050, "predicted_safety_mib": 4})
        self.store.records.extend([first, second])

    def test_every_checked_record_gets_a_column(self):
        chart = charts.forecast_misses(self.store)
        self.assertEqual(chart.categories, ("Cache 1", "Cache 4"))
        self.assertEqual(len(series_named(chart, "перезаклад").points), 2)
        self.assertEqual(series_named(chart, "перезаклад").kind, KIND_BARS)

    def test_the_bar_is_the_miss_and_the_dot_is_the_miss_without_safety(self):
        """+5 MiB при страховке 5 MiB и +5 при страховке 8 — разные вещи."""
        chart = charts.forecast_misses(self.store)
        bars = series_named(chart, "перезаклад").points
        dots = series_named(chart, "без страховки").points
        self.assertEqual(bars[0].y, float(CACHE_1.container_mib - CACHE_1.minimum_mib))
        self.assertEqual(dots[0].y, bars[0].y - 4)

    def test_records_without_a_forecast_are_left_out(self):
        """Обещание не записано — сверять не с чем, и столбик был бы враньём."""
        self.store.records.append(Record(**vars(CACHE_4)))
        self.assertEqual(len(charts.forecast_misses(self.store).categories), 2)

    def test_nothing_measured_yet_is_an_empty_chart_not_a_crash(self):
        self.assertTrue(charts.forecast_misses(empty_store()).empty)


class BreakdownTests(unittest.TestCase):
    def setUp(self):
        payload = Payload.for_files([700 * MIB], 4096)
        self.solution = solve_container_mib(payload, safety_bytes=4 * MIB)
        self.chart = charts.container_breakdown(self.solution)

    def test_the_parts_add_up_to_the_container_exactly(self):
        """Полоса врёт, если сумма долей не равна целому.

        Округление до целых MiB — такое же слагаемое, как остальные, и без
        него полоса не сходится с числом, которое вводят в VeraCrypt.
        """
        total = sum(int(point.y) for point in self.chart.series[0].points)
        self.assertEqual(total, self.solution.container_bytes)

    def test_it_is_a_stack_without_axes(self):
        self.assertEqual(self.chart.layout, LAYOUT_STACK)
        self.assertEqual(self.chart.series[0].kind, KIND_STACK)

    def test_every_part_carries_its_exact_number(self):
        """Тонкие слагаемые курсором не поймать — их читают в легенде."""
        for point in self.chart.series[0].points:
            with self.subTest(point.tip):
                self.assertIn(" B · ", point.tip)


class ClusterTailTests(unittest.TestCase):
    def setUp(self):
        self.sizes = [1024] * 500 + [10 * MIB] * 50
        self.choices = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
        self.chart = charts.cluster_tail(self.sizes, self.choices, 4096)

    def test_the_curve_and_its_dots_are_one_legend_entry(self):
        """Двумя сериями это давало бы две записи одного цвета про одно и то же."""
        names = [item.name for item in self.chart.series]
        self.assertNotIn("замеры", names)
        self.assertEqual(series_named(self.chart, "по кластерам").kind, KIND_LINE_DOTS)

    def test_a_bigger_cluster_never_takes_less_room(self):
        values = [point.y for point in series_named(self.chart, "по кластерам").points]
        self.assertEqual(values, sorted(values))

    def test_the_current_choice_is_marked(self):
        chosen = series_named(self.chart, "выбрано сейчас").points
        self.assertEqual([point.x for point in chosen], [4096.0])

    def test_the_flat_line_is_the_logical_size(self):
        """Расстояние до неё и есть кластерный хвост."""
        flat = series_named(self.chart, "логический размер").points
        self.assertEqual({point.y for point in flat}, {float(sum(self.sizes))})

    def test_it_counts_real_files_not_a_model(self):
        self.assertIn("Файлов: 550", self.chart.note)


if __name__ == "__main__":
    unittest.main()
