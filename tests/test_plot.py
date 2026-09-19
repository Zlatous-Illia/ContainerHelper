"""Chart arithmetic: ticks, bounds, mapping to pixels, hit testing.

Without Qt — like the module itself. What is drawn is not checked by eye: a
curve drawn in the wrong place looks just as convincing as one drawn right.
"""

import math
import unittest

from containerhelper.formatting import UNIT_AUTO
from containerhelper.model import MIB
from containerhelper.plot import (
    AXIS_BYTES,
    AXIS_COUNT,
    AXIS_MIB,
    AXIS_PLAIN,
    KIND_STACK,
    Axis,
    Frame,
    Point,
    Series,
    Span,
    axis_caption,
    bounds,
    nearest,
    padded,
    stack_hit,
    ticks,
    value_label,
)

GIB = 1024 * MIB


def texts(axis, lo, hi, unit=UNIT_AUTO):
    return [tick.text for tick in ticks(axis, lo, hi, unit)]


class TickTests(unittest.TestCase):
    """Ticks are where a home-made chart usually gives itself away."""

    def test_a_byte_axis_is_labelled_in_a_multiple_unit(self):
        """Labels read "50 MiB", not "52 428 800" — otherwise unreadable."""
        axis = Axis("Метаданные", AXIS_BYTES)
        lo, hi = padded(17 * MIB, 143 * MIB)
        self.assertEqual(
            texts(axis, lo, hi), ["20", "40", "60", "80", "100", "120", "140"]
        )
        self.assertEqual(axis_caption(axis, lo, hi, UNIT_AUTO), "Метаданные, MiB")

    def test_the_step_is_the_nearest_one_not_the_next_one_up(self):
        """Rounding up looks logical and thins the axis out by half.

        On a 137 MiB range it jumps from a step of 20 straight to 50: instead
        of seven ticks two remain, and the axis stops saying anything at all
        about the values in between. Checked on the real range of the NTFS
        curve.
        """
        axis = Axis("Метаданные", AXIS_BYTES)
        self.assertGreaterEqual(len(texts(axis, *padded(17 * MIB, 143 * MIB))), 5)

    def test_a_whole_step_gets_no_trailing_zeros(self):
        """A "4.00 MiB" on an axis is noise that makes the labels overlap."""
        axis = Axis("Метаданные", AXIS_BYTES)
        for text in texts(axis, 0, 20 * MIB):
            with self.subTest(text):
                self.assertNotIn(",", text.replace(" ", ""))

    def test_the_step_follows_the_one_two_five_ladder(self):
        """Without the 1-2-5 ladder labels read 0,0037 — obvious at once."""
        axis = Axis("Наклон", AXIS_PLAIN)
        values = [tick.value for tick in ticks(axis, *padded(0.128, 0.324, include_zero=True))]
        self.assertGreaterEqual(len(values), 3)
        steps = {round(b - a, 9) for a, b in zip(values, values[1:])}
        self.assertEqual(len(steps), 1)
        step = steps.pop()
        mantissa = step / 10 ** math.floor(math.log10(step))
        self.assertAlmostEqual(min((1.0, 2.0, 5.0), key=lambda f: abs(f - mantissa)), mantissa)

    def test_a_logarithmic_byte_axis_walks_powers_of_two(self):
        """Sizes here are power-of-two multiples; decades would miss them."""
        axis = Axis("Размер тома", AXIS_BYTES, log=True)
        labels = texts(axis, *padded(536604672, 1099511361536, log=True))
        self.assertIn("512 MiB", labels)
        self.assertIn("1 GiB", labels)
        self.assertIn("1 TiB", labels)

    def test_a_logarithmic_byte_axis_gives_every_tick_its_own_unit(self):
        """A shared unit would turn half the labels into "0.001"."""
        axis = Axis("Размер тома", AXIS_BYTES, log=True)
        lo, hi = padded(536604672, 1099511361536, log=True)
        self.assertNotIn(",", axis_caption(axis, lo, hi, UNIT_AUTO))
        units = {text.split()[-1] for text in texts(axis, lo, hi)}
        self.assertEqual(units, {"MiB", "GiB", "TiB"})

    def test_a_count_axis_walks_powers_of_ten(self):
        """A file count is never a power of two: 1, 4, 16 reads worse."""
        axis = Axis("Файлов", AXIS_COUNT, log=True)
        self.assertEqual(
            texts(axis, *padded(1, 10000, log=True)),
            ["1", "10", "100", "1 000", "10 000"],
        )

    def test_a_mib_axis_says_so_in_the_caption(self):
        axis = Axis("Промах", AXIS_MIB)
        self.assertEqual(axis_caption(axis, 0, 12), "Промах, MiB")

    def test_ticks_stay_inside_the_range(self):
        axis = Axis("Метаданные", AXIS_BYTES)
        lo, hi = padded(17 * MIB, 143 * MIB)
        for tick in ticks(axis, lo, hi, UNIT_AUTO):
            with self.subTest(tick.text):
                self.assertGreaterEqual(tick.value, lo)
                self.assertLessEqual(tick.value, hi)

    def test_a_flat_range_does_not_divide_by_zero(self):
        """A single measurement is a normal state, not a breakage."""
        axis = Axis("Метаданные", AXIS_BYTES)
        lo, hi = padded(19 * MIB, 19 * MIB)
        self.assertLess(lo, hi)
        self.assertIsInstance(ticks(axis, lo, hi, UNIT_AUTO), list)


class BoundsTests(unittest.TestCase):
    def test_nothing_measured_yet_is_not_a_crash(self):
        self.assertEqual(bounds(()), (0.0, 1.0))

    def test_bounds_cover_every_visible_series(self):
        first = Series("a", (Point(1, 10), Point(3, 30)))
        second = Series("b", (Point(2, 5), Point(9, 20)))
        self.assertEqual(bounds((first, second), "x"), (1, 9))
        self.assertEqual(bounds((first, second), "y"), (5, 30))

    def test_a_hidden_series_does_not_stretch_the_axis(self):
        """Else a series hidden via the legend would still hold the scale."""
        shown = Series("a", (Point(1, 10),))
        hidden = Series("b", (Point(500, 900),), visible=False)
        self.assertEqual(bounds((shown, hidden), "x"), (1, 1))

    def test_zero_is_included_when_asked(self):
        """Residuals without zero are meaningless: zero there is the model."""
        lo, hi = padded(120000, 202672, include_zero=True)
        self.assertLessEqual(lo, 0)

    def test_log_padding_is_measured_in_octaves(self):
        """A fraction of the value would eat a whole octave at the left end."""
        lo, hi = padded(1024, 1048576, log=True)
        self.assertLess(lo, 1024)
        self.assertGreater(hi, 1048576)
        self.assertAlmostEqual(
            math.log2(1024 / lo), math.log2(hi / 1048576), places=6
        )


class SpanTests(unittest.TestCase):
    def test_share_and_value_are_inverse(self):
        span = Span(100, 500)
        for value in (100, 213, 500):
            with self.subTest(value):
                self.assertAlmostEqual(span.value(span.share(value)), value)

    def test_a_log_span_is_inverse_too(self):
        span = Span(512 * MIB, 1024 * GIB, log=True)
        for value in (512 * MIB, 8 * GIB, 1024 * GIB):
            with self.subTest(value):
                self.assertAlmostEqual(span.value(span.share(value)) / value, 1.0)

    def test_a_log_span_puts_the_geometric_middle_in_the_middle(self):
        span = Span(1, 1024, log=True)
        self.assertAlmostEqual(span.value(0.5), 32.0)

    def test_a_flat_span_answers_the_middle_instead_of_dividing_by_zero(self):
        self.assertEqual(Span(7, 7).share(7), 0.5)

    def test_zoom_by_frame_takes_the_selected_part(self):
        span = Span(0, 100)
        zoomed = span.zoomed(0.25, 0.75)
        self.assertAlmostEqual(zoomed.lo, 25)
        self.assertAlmostEqual(zoomed.hi, 75)

    def test_zoom_by_frame_survives_a_backwards_drag(self):
        """A frame is dragged either way; right to left is a selection too."""
        self.assertEqual(Span(0, 100).zoomed(0.75, 0.25), Span(0, 100).zoomed(0.25, 0.75))

    def test_the_wheel_keeps_the_point_under_the_cursor(self):
        span = Span(0, 100)
        before = span.value(0.3)
        self.assertAlmostEqual(span.scaled(0.5, 0.3).value(0.3), before)

    def test_the_wheel_keeps_the_point_on_a_log_axis_too(self):
        span = Span(1, 1024, log=True)
        before = span.value(0.3)
        self.assertAlmostEqual(span.scaled(0.5, 0.3).value(0.3) / before, 1.0)

    def test_panning_moves_by_a_share_of_the_span(self):
        span = Span(0, 100).shifted(0.1)
        self.assertAlmostEqual(span.lo, 10)
        self.assertAlmostEqual(span.hi, 110)

    def test_panning_a_log_axis_keeps_the_ratio(self):
        span = Span(1, 1024, log=True).shifted(0.1)
        self.assertAlmostEqual(span.hi / span.lo, 1024.0)


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.frame = Frame(50, 10, 200, 100, Span(0, 100), Span(0, 50))

    def test_the_y_axis_grows_upwards(self):
        """Screen zero is on top; forget it: the classic upside-down chart."""
        self.assertAlmostEqual(self.frame.py(0), 110)
        self.assertAlmostEqual(self.frame.py(50), 10)

    def test_the_x_axis_starts_at_the_left_edge(self):
        self.assertAlmostEqual(self.frame.px(0), 50)
        self.assertAlmostEqual(self.frame.px(100), 250)

    def test_pixels_and_values_are_inverse(self):
        self.assertAlmostEqual(self.frame.at_px(self.frame.px(37)), 37)
        self.assertAlmostEqual(self.frame.at_py(self.frame.py(21)), 21)

    def test_contains_knows_the_plot_rectangle(self):
        self.assertTrue(self.frame.contains(60, 20))
        self.assertFalse(self.frame.contains(10, 20))


class HitTests(unittest.TestCase):
    def setUp(self):
        self.frame = Frame(0, 0, 100, 100, Span(0, 100), Span(0, 100))
        self.series = (
            Series("замеры", (Point(10, 10, "первая"), Point(90, 90, "вторая"))),
        )

    def test_the_nearest_point_wins(self):
        hit = nearest(self.frame, self.series, 10, 90)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.point.tip, "первая")

    def test_far_from_everything_hits_nothing(self):
        """Otherwise the tooltip sticks to a point half a screen away."""
        self.assertIsNone(nearest(self.frame, self.series, 50, 50))

    def test_distance_is_measured_in_pixels_not_in_values(self):
        """By value, the nearest would always be on the small-range axis.

        The cursor sits right against the point that is far by value: it is a
        million away in X, the other one is one away in Y. In pixels the first
        wins, and that is right, because a person aims at the screen, not at a
        number.
        """
        frame = Frame(0, 0, 100, 100, Span(0, 1_000_000), Span(0, 1))
        series = (Series("a", (Point(1_000_000, 0.0), Point(0, 1.0))),)
        hit = nearest(frame, series, 98, 98)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.point.x, 1_000_000)

    def test_a_hidden_series_is_not_hit(self):
        hidden = (Series("замеры", (Point(10, 10),), visible=False),)
        self.assertIsNone(nearest(self.frame, hidden, 10, 90))

    def test_a_stack_is_not_hit_as_points(self):
        """For a bar, a hit is the segment's area, not closeness to a point."""
        stack = (Series("разложение", (Point(0, 10),), kind=KIND_STACK),)
        self.assertIsNone(nearest(self.frame, stack, 0, 100))


class StackTests(unittest.TestCase):
    def setUp(self):
        self.frame = Frame(0, 0, 100, 20, Span(0, 1), Span(0, 1))
        self.series = Series(
            "разложение",
            (Point(0, 900, "данные"), Point(0, 99, "NTFS"), Point(0, 1, "заголовок")),
            kind=KIND_STACK,
        )

    def test_a_wide_segment_is_caught_where_it_lies(self):
        self.assertEqual(stack_hit(self.frame, self.series, 10, 10).tip, "данные")

    def test_segments_are_laid_out_by_their_share(self):
        """900 : 99 : 1 over a hundred pixels is 90, 9.9 and 0.1 pixels."""
        self.assertEqual(stack_hit(self.frame, self.series, 95, 10).tip, "NTFS")
        self.assertEqual(stack_hit(self.frame, self.series, 99.95, 10).tip, "заголовок")

    def test_a_subpixel_segment_is_not_made_catchable_by_stealing_room(self):
        """Widening its zone would split what is shown from what is queried.

        The cursor would stand on the NTFS metadata while the tooltip spoke of
        the VeraCrypt header. Such components are explained by the legend, not
        by hovering.
        """
        self.assertEqual(stack_hit(self.frame, self.series, 99.5, 10).tip, "NTFS")

    def test_outside_the_rectangle_hits_nothing(self):
        self.assertIsNone(stack_hit(self.frame, self.series, 10, 100))


class ValueLabelTests(unittest.TestCase):
    """The label under the crosshair uses the same step as the ticks.

    Otherwise one and the same spot on the axis is labelled two ways: "4" at
    the tick and "4,0000001" at the crosshair next to it.
    """

    def test_a_log_byte_axis_names_the_power_of_two(self):
        axis = Axis("Размер тома", AXIS_BYTES, log=True)
        self.assertEqual(
            value_label(axis, 2 ** 31, 2 ** 29, 2 ** 40), "2 GiB"
        )

    def test_a_linear_byte_axis_follows_the_chosen_unit(self):
        axis = Axis("Метаданные", AXIS_BYTES)
        self.assertEqual(
            value_label(axis, 36_573_184, 0, 40_000_000), "36 573 184"
        )

    def test_a_counted_axis_stays_whole(self):
        axis = Axis("Файлов", AXIS_COUNT, log=True)
        self.assertEqual(value_label(axis, 5000, 1, 10000), "5 000")

    def test_a_listed_axis_names_the_category(self):
        axis = Axis("Запись")
        self.assertEqual(
            value_label(axis, 1.0, -0.5, 2.5, categories=("a", "b", "c")), "b"
        )

    def test_a_cursor_beyond_the_list_names_nothing(self):
        axis = Axis("Запись")
        self.assertEqual(value_label(axis, 9.0, -0.5, 2.5, categories=("a",)), "")


class LogCountTicksTests(unittest.TestCase):
    """Fractional ticks on a logarithmic count axis.

    They went through `fmt_bytes(int(...))`, and "0,01" turned into "0":
    values below one simply never occurred on such an axis until the share of
    the volume came along.
    """

    axis = Axis("Доля тома, %", AXIS_PLAIN, log=True)

    def test_a_decade_below_one_keeps_its_digits(self):
        labels = [tick.text for tick in ticks(self.axis, 0.01, 20)]
        self.assertEqual(labels, ["0,01", "0,1", "1", "10"])

    def test_whole_values_stay_whole(self):
        self.assertEqual(value_label(self.axis, 12.0, 0.01, 20), "12")

    def test_a_fraction_is_not_rounded_to_zero(self):
        self.assertNotEqual(value_label(self.axis, 0.35, 0.01, 20), "0")


if __name__ == "__main__":
    unittest.main()
