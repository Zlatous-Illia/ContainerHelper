"""Chart contents: which points ended up on them and what the tooltip says.

No Qt: building a chart is data, not presentation. The check needs no
window, and a window would be needed for exactly one thing: to look at it.
"""

import tempfile
import unittest
from pathlib import Path

from containerhelper import charts
from containerhelper.charts import forecast_misses
from containerhelper.factory import factory_data
from containerhelper.model import MIB, VC_HEADERS_BYTES, Payload, solve_container_mib
from containerhelper.plot import (
    KIND_BARS,
    KIND_DOTS,
    KIND_LINE,
    KIND_LINE_DOTS,
    KIND_STACK,
    KIND_STEMS,
    KIND_STEPS,
    LAYOUT_STACK,
)
from containerhelper.records import Record, Store

from .reference import CACHE_1, CACHE_4, HEADERS_AND_TAIL


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
        """A fresh copy calculates from the factory data and must show it."""
        chart = charts.ntfs_curve(empty_store())
        self.assertEqual(len(series_named(chart, "свои замеры").points), 0)
        self.assertEqual(
            len(series_named(chart, "заводские").points),
            len(factory_data().points) + len(factory_data().samples),
        )

    def test_an_own_measurement_moves_the_point_to_its_own_series(self):
        """An own measurement supersedes factory; the chart must show it."""
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
        """The axis is logarithmic: a straight segment becomes an arc on it.

        Drawn through two ends, the model would miss its own measurements.
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
        """Underestimate is the only dangerous side; it must stand apart."""
        chart = charts.ntfs_residuals(empty_store())
        under = series_named(chart, "модель занизила").points
        over = series_named(chart, "модель завысила").points
        self.assertTrue(all(point.y > 0 for point in under))
        self.assertTrue(all(point.y <= 0 for point in over))
        self.assertTrue(under or over)

    def test_a_residual_is_not_zero_at_its_own_point(self):
        """This is what the leave-one-out check is for.

        The piecewise-linear model passes exactly through its own
        measurements: without leaving out the point under check, the
        deviation would come out zero everywhere, and the chart would be a
        straight line at zero.
        """
        chart = charts.ntfs_residuals(empty_store())
        values = [
            point.y
            for item in chart.series
            for point in item.points
        ]
        self.assertTrue(any(abs(value) > 0 for value in values))


    def test_the_edge_measurements_are_kept_apart_and_hidden(self):
        """Without the edge points the model extrapolates.

        The miss there is of another magnitude. On real measurements the edge
        one gives −433 MiB against fractions of a megabyte for all the others:
        leave it in the common series and the axis stretches so far that
        everything that matters lies flat on the zero line.
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
        """Hidden silently is the same as lost."""
        note = charts.ntfs_residuals(empty_store()).note
        self.assertIn("Крайние замеры спрятаны", note)
        self.assertIn("легенде", note)


class SlopeTests(unittest.TestCase):
    def test_every_segment_becomes_a_step(self):
        store = empty_store()
        chart = charts.ntfs_slopes(store)
        steps = series_named(chart, "отрезки")
        self.assertEqual(steps.kind, KIND_STEPS)
        # One step per segment plus a closing point at the last measurement.
        expected = len(factory_data().points) + len(factory_data().samples)
        self.assertEqual(len(steps.points), expected)

    def test_the_slope_is_a_percentage_of_the_volume(self):
        """SPEC records them as percentages too: 0.215 %, 0.128 %, 0.324 %."""
        chart = charts.ntfs_slopes(empty_store())
        values = [point.y for point in series_named(chart, "отрезки").points]
        self.assertTrue(all(0 <= value < 5 for value in values), values[:5])

    def test_the_staircase_shows_up_as_flat_steps(self):
        """A plateau with zero slope is exactly the staircase from SPEC.

        Between 89 and 116 MiB the metadata does not grow at all, and on the
        next segment it jumps at once: $LogFile changes in steps at discrete
        thresholds. That is why the chart is drawn: on the curve itself this
        cannot be made out.
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
        """+5 MiB with a 5 MiB safety margin and +5 with 8 differ."""
        chart = charts.forecast_misses(self.store)
        bars = series_named(chart, "перезаклад").points
        dots = series_named(chart, "без страховки").points
        self.assertEqual(bars[0].y, float(CACHE_1.container_mib - CACHE_1.minimum_mib))
        self.assertEqual(dots[0].y, bars[0].y - 4)

    def test_records_without_a_forecast_are_left_out(self):
        """No prediction recorded: nothing to compare, a bar would be a lie."""
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
        """The bar lies if the sum of its parts does not equal the whole.

        Rounding to whole MiB is a part like any other, and without it the
        bar does not add up to the number typed into VeraCrypt.
        """
        total = sum(int(point.y) for point in self.chart.series[0].points)
        self.assertEqual(total, self.solution.container_bytes)

    def test_it_is_a_stack_without_axes(self):
        self.assertEqual(self.chart.layout, LAYOUT_STACK)
        self.assertEqual(self.chart.series[0].kind, KIND_STACK)

    def test_every_part_carries_its_exact_number(self):
        """The cursor cannot catch thin parts; they are read in the legend."""
        for point in self.chart.series[0].points:
            with self.subTest(point.tip):
                self.assertIn(" B · ", point.tip)


class ClusterTailTests(unittest.TestCase):
    def setUp(self):
        self.sizes = [1024] * 500 + [10 * MIB] * 50
        self.choices = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)
        self.chart = charts.cluster_tail(self.sizes, self.choices, 4096)

    def test_the_curve_and_its_dots_are_one_legend_entry(self):
        """Two series: two same-coloured legend entries for the same thing."""
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
        """The distance to it is the cluster tail."""
        flat = series_named(self.chart, "логический размер").points
        self.assertEqual({point.y for point in flat}, {float(sum(self.sizes))})

    def test_it_counts_real_files_not_a_model(self):
        self.assertIn("Файлов: 550", self.chart.note)


class ForecastSeriesTests(unittest.TestCase):
    """Overestimate is a bar, the miss without the safety margin is dots.

    A bar on top of a bar reads as "part of a whole", while this is a separate
    quantity of the same miss: how much would remain without the safety
    margin.
    """

    def store(self):
        return Store(
            path=Path("нет"),
            records=[
                Record(
                    id="Промах",
                    container_mib=1024,
                    mounted_bytes=1_073_475_584,
                    empty_free_bytes=1_055_596_544,
                    file_bytes=1_000_000_000,
                    file_count=10,
                    left_bytes=5_000_000,
                    predicted_mib=1024,
                    predicted_safety_mib=3,
                )
            ],
        )

    def test_the_two_series_are_drawn_differently(self):
        chart = forecast_misses(self.store())
        kinds = {series.name: series.kind for series in chart.series}
        self.assertEqual(kinds["перезаклад"], KIND_BARS)
        self.assertEqual(kinds["без страховки"], KIND_DOTS)

    def test_the_zero_line_is_asked_for(self):
        """The sign of a miss decides everything; zero must be in the frame."""
        self.assertTrue(forecast_misses(self.store()).zero_line)


class UncoveredTests(unittest.TestCase):
    """The note under the curve names the uncovered sizes.

    This already existed as a separate panel, a "coverage strip" with three
    rows of dots, and turned out to be redundant: own against factory shows
    on the curve by colour, and the rest fits in one line. The table on the
    Calibration tab says the same in more detail and with buttons.
    """

    #: A size found in neither factory nor own measurements. The factory
    #: empty-volume points cover exactly the rows of the recommended-sizes
    #: table, so only a size outside it can be "uncovered". The seven factory
    #: copy-slack measurements add NTFS points off the table too, but 3000 MiB
    #: is not one of them.
    ODD_MIB = 3000

    def test_a_size_without_any_measurement_is_named(self):
        store = Store(path=Path("нет"))
        self.assertEqual(charts.uncovered(store, (self.ODD_MIB,)), [self.ODD_MIB])

    def test_a_factory_size_counts_as_covered(self):
        store = Store(path=Path("нет"))
        self.assertEqual(charts.uncovered(store, (1024,)), [])

    def test_the_note_names_them(self):
        note = charts.ntfs_curve(Store(path=Path("нет")), (self.ODD_MIB,)).note
        self.assertIn("Не покрыто замерами", note)
        self.assertIn("3000 MiB", note)

    def test_a_covered_curve_says_nothing_extra(self):
        note = charts.ntfs_curve(Store(path=Path("нет")), (1024, 2048)).note
        self.assertNotIn("Не покрыто", note)

    def test_a_long_list_is_cut_short(self):
        sizes = tuple(3000 + step for step in range(12))
        note = charts.ntfs_curve(Store(path=Path("нет")), sizes).note
        self.assertIn("и ещё", note)

    def test_an_own_measurement_covers_the_size(self):
        volume = self.ODD_MIB * MIB - HEADERS_AND_TAIL
        store = Store(
            path=Path("нет"),
            calibration=[
                Record(
                    id="Своя",
                    container_mib=self.ODD_MIB,
                    mounted_bytes=volume,
                    empty_free_bytes=volume - 20 * MIB,
                )
            ],
        )
        self.assertEqual(charts.uncovered(store, (self.ODD_MIB,)), [])


class StemTests(unittest.TestCase):
    """Residuals as stems: zero is the model itself, and misses start at it."""

    def test_both_residual_charts_use_stems(self):
        store = Store(path=Path("нет"), records=[CACHE_1, CACHE_4])
        for chart in (charts.ntfs_residuals(store), charts.slack_residuals(store)):
            with self.subTest(chart.title):
                kinds = {series.kind for series in chart.series}
                self.assertEqual(kinds, {KIND_STEMS})


class ShareTests(unittest.TestCase):
    """Share of the volume taken by metadata: the same curve in other units.

    In bytes, the difference between 0.2 % and 0.4 % on a terabyte volume is
    one line thick, yet this is usually how people ask about metadata: "how
    much of the volume will it eat".
    """

    def store(self):
        return Store(
            path=Path("нет"),
            calibration=[
                Record(
                    id="Малый",
                    container_mib=64,
                    mounted_bytes=64 * MIB,
                    empty_free_bytes=54 * MIB,
                ),
                Record(
                    id="Большой",
                    container_mib=102400,
                    mounted_bytes=102400 * MIB,
                    empty_free_bytes=102400 * MIB - 180 * MIB,
                ),
            ],
        )

    def test_the_value_is_a_percentage_of_the_volume(self):
        chart = charts.ntfs_share(self.store())
        mine = next(s for s in chart.series if s.name == "свои замеры")
        small = min(mine.points, key=lambda point: point.x)
        volume = 64 * MIB - VC_HEADERS_BYTES
        self.assertAlmostEqual(
            small.y, (volume - 54 * MIB) / volume * 100, places=6
        )

    def test_the_small_volume_stands_far_above_the_big_one(self):
        chart = charts.ntfs_share(self.store())
        mine = next(s for s in chart.series if s.name == "свои замеры")
        shares = [point.y for point in sorted(mine.points, key=lambda p: p.x)]
        self.assertGreater(shares[0], shares[-1] * 10)

    def test_both_axes_are_logarithmic(self):
        """The share spans three orders of magnitude.

        On a linear axis the middle cannot be seen.
        """
        chart = charts.ntfs_share(self.store())
        self.assertTrue(chart.x.log)
        self.assertTrue(chart.y.log)

    def test_the_model_is_drawn_too(self):
        chart = charts.ntfs_share(self.store())
        model = next(s for s in chart.series if s.name == "модель")
        self.assertGreater(len(model.points), 2)
        self.assertTrue(all(point.y > 0 for point in model.points))

    def test_the_tip_names_the_share(self):
        chart = charts.ntfs_share(self.store())
        mine = next(s for s in chart.series if s.name == "свои замеры")
        self.assertIn("Доля тома", mine.points[0].tip)
        # A decimal point, as in the tables — not a comma.
        self.assertRegex(mine.points[0].tip.split("Доля тома")[1], r"^: \d+\.\d{3} %$")


if __name__ == "__main__":
    unittest.main()
