"""Арифметика графиков: деления, границы, отображение в пиксели, попадание.

Без Qt — как и сам модуль. Нарисованное глазами не проверяется: кривая,
проведённая мимо, выглядит так же убедительно, как проведённая верно.
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
    """Деления — то место, где самописный график обычно выдаёт себя."""

    def test_a_byte_axis_is_labelled_in_a_multiple_unit(self):
        """«50 MiB» вместо «52 428 800» — иначе подпись не читается."""
        axis = Axis("Метаданные", AXIS_BYTES)
        lo, hi = padded(17 * MIB, 143 * MIB)
        self.assertEqual(
            texts(axis, lo, hi), ["20", "40", "60", "80", "100", "120", "140"]
        )
        self.assertEqual(axis_caption(axis, lo, hi, UNIT_AUTO), "Метаданные, MiB")

    def test_the_step_is_the_nearest_one_not_the_next_one_up(self):
        """Округление вверх выглядит логично и разрежает ось вдвое.

        На размахе в 137 MiB оно отправляет с шага 20 сразу на 50: вместо семи
        делений остаётся два, и ось перестаёт говорить хоть что-то о
        промежуточных значениях. Проверено на настоящем размахе кривой NTFS.
        """
        axis = Axis("Метаданные", AXIS_BYTES)
        self.assertGreaterEqual(len(texts(axis, *padded(17 * MIB, 143 * MIB))), 5)

    def test_a_whole_step_gets_no_trailing_zeros(self):
        """«4.00 MiB» на оси — шум, из-за которого подписи налезают друг на друга."""
        axis = Axis("Метаданные", AXIS_BYTES)
        for text in texts(axis, 0, 20 * MIB):
            with self.subTest(text):
                self.assertNotIn(",", text.replace(" ", ""))

    def test_the_step_follows_the_one_two_five_ladder(self):
        """Без лестницы 1-2-5 подписи выходят вида 0,0037 — и это видно сразу."""
        axis = Axis("Наклон", AXIS_PLAIN)
        values = [tick.value for tick in ticks(axis, *padded(0.128, 0.324, include_zero=True))]
        self.assertGreaterEqual(len(values), 3)
        steps = {round(b - a, 9) for a, b in zip(values, values[1:])}
        self.assertEqual(len(steps), 1)
        step = steps.pop()
        mantissa = step / 10 ** math.floor(math.log10(step))
        self.assertAlmostEqual(min((1.0, 2.0, 5.0), key=lambda f: abs(f - mantissa)), mantissa)

    def test_a_logarithmic_byte_axis_walks_powers_of_two(self):
        """Все размеры тут кратны степени двойки; декады встали бы мимо них."""
        axis = Axis("Размер тома", AXIS_BYTES, log=True)
        labels = texts(axis, *padded(536604672, 1099511361536, log=True))
        self.assertIn("512 MiB", labels)
        self.assertIn("1 GiB", labels)
        self.assertIn("1 TiB", labels)

    def test_a_logarithmic_byte_axis_gives_every_tick_its_own_unit(self):
        """Общая единица превратила бы половину подписей в «0.001»."""
        axis = Axis("Размер тома", AXIS_BYTES, log=True)
        lo, hi = padded(536604672, 1099511361536, log=True)
        self.assertNotIn(",", axis_caption(axis, lo, hi, UNIT_AUTO))
        units = {text.split()[-1] for text in texts(axis, lo, hi)}
        self.assertEqual(units, {"MiB", "GiB", "TiB"})

    def test_a_count_axis_walks_powers_of_ten(self):
        """Число файлов степенью двойки не бывает: 1, 4, 16 читается хуже."""
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
        """Один замер — обычное состояние, а не поломка."""
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
        """Иначе выключенная щелчком по легенде серия держала бы масштаб."""
        shown = Series("a", (Point(1, 10),))
        hidden = Series("b", (Point(500, 900),), visible=False)
        self.assertEqual(bounds((shown, hidden), "x"), (1, 1))

    def test_zero_is_included_when_asked(self):
        """Остатки без нуля бессмысленны: ноль там и есть модель."""
        lo, hi = padded(120000, 202672, include_zero=True)
        self.assertLessEqual(lo, 0)

    def test_log_padding_is_measured_in_octaves(self):
        """Доля от значения съела бы у левого края целую октаву."""
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
        """Рамку тянут в любую сторону, и справа налево — тоже выделение."""
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
        """Экранный ноль сверху; забыть это — классический перевёрнутый график."""
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
        """Иначе подсказка липнет к точке через полэкрана."""
        self.assertIsNone(nearest(self.frame, self.series, 50, 50))

    def test_distance_is_measured_in_pixels_not_in_values(self):
        """По значениям ближайшей всегда была бы точка оси с мелким размахом.

        Курсор стоит вплотную к дальней по значению точке: до неё миллион по
        X, до соседней — единица по Y. В пикселях выигрывает первая, и это
        верно, потому что человек целится в экран, а не в число.
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
        """У полосы попадание — это площадь сегмента, а не близость к точке."""
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
        """900 : 99 : 1 на ста пикселях — это 90, 9.9 и 0.1 пикселя."""
        self.assertEqual(stack_hit(self.frame, self.series, 95, 10).tip, "NTFS")
        self.assertEqual(stack_hit(self.frame, self.series, 99.95, 10).tip, "заголовок")

    def test_a_subpixel_segment_is_not_made_catchable_by_stealing_room(self):
        """Расширить его зону — значит развести показанное и опрошенное.

        Курсор стоял бы на метаданных NTFS, а подсказка говорила про заголовок
        VeraCrypt. Такие слагаемые объясняет легенда, а не наведение.
        """
        self.assertEqual(stack_hit(self.frame, self.series, 99.5, 10).tip, "NTFS")

    def test_outside_the_rectangle_hits_nothing(self):
        self.assertIsNone(stack_hit(self.frame, self.series, 10, 100))


class ValueLabelTests(unittest.TestCase):
    """Подпись под перекрестьем считается тем же шагом, что и деления.

    Иначе одно и то же место оси подписано двумя способами: у деления «4»,
    а у перекрестья рядом — «4,0000001».
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
    """Дробные деления логарифмической счётной оси.

    Они шли через `fmt_bytes(int(...))`, и «0,01» превращалось в «0»: величин
    меньше единицы на такой оси до доли тома просто не бывало.
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
