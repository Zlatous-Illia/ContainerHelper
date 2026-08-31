"""Виджет графика, окна графиков и их связь с главным окном."""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

_settings_dir = tempfile.mkdtemp(prefix="containerhelper-charts-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from containerhelper.factory import factory_data  # noqa: E402
from containerhelper.formatting import UNIT_GIB  # noqa: E402
from containerhelper.model import MIB  # noqa: E402
from containerhelper.records import Record  # noqa: E402
from containerhelper.plot import (  # noqa: E402
    AXIS_BYTES,
    KIND_DOTS,
    KIND_LINE,
    KIND_STACK,
    LAYOUT_STACK,
    Axis,
    Chart,
    Point,
    Series,
)
from containerhelper.ui.app import MainWindow  # noqa: E402
from containerhelper.ui.chart import ChartView  # noqa: E402
from containerhelper.ui.chart_window import (  # noqa: E402
    CHART_CALC,
    CHART_FORECAST,
    CHART_NTFS,
    CHART_SLACK,
    ChartWindow,
)


def sample_chart() -> Chart:
    points = tuple(
        Point(float(volume), float(volume // 60), f"том {volume}", key=volume)
        for volume in (512 * MIB, 4096 * MIB, 65536 * MIB)
    )
    return Chart(
        "Пример",
        Axis("Размер тома", AXIS_BYTES, log=True),
        Axis("Метаданные", AXIS_BYTES),
        (
            Series("замеры", points, KIND_DOTS, tone=0),
            Series("модель", points, KIND_LINE, tone=1),
        ),
        note="подпись снизу",
    )


class ChartViewTests(unittest.TestCase):
    def setUp(self):
        self.view = ChartView(sample_chart())
        self.view.resize(640, 420)

    def test_it_draws_something(self):
        self.assertFalse(self.view.image(1.0).isNull())

    def test_the_left_margin_makes_room_for_the_widest_label(self):
        """Подобранное на глаз поле обрежет «1 099 511 627 776» ровно тогда,

        когда такое число появится, — а появится оно на терабайтном томе.
        """
        frame = self.view.frame()
        self.assertGreater(frame.left, 20)
        self.assertLess(frame.left, self.view.width() / 2)

    def test_hovering_a_point_explains_it(self):
        frame = self.view.frame()
        point = self.view.chart().series[0].points[0]
        tip = self.view.tip_at(frame.px(point.x), frame.py(point.y))
        self.assertEqual(tip, point.tip)

    def test_hovering_empty_space_says_nothing(self):
        frame = self.view.frame()
        self.assertEqual(self.view.tip_at(frame.left + 2, frame.top + 2), "")

    def test_the_legend_hides_and_returns_a_series(self):
        self.view.image(1.0)  # легенда размечается при рисовании
        self.view.toggle_series("модель")
        self.assertEqual(
            [item.name for item in self.view.series() if item.visible], ["замеры"]
        )
        self.view.toggle_series("модель")
        self.assertEqual(len([i for i in self.view.series() if i.visible]), 2)

    def test_a_hidden_series_stops_holding_the_scale(self):
        """Иначе выключенная серия продолжала бы растягивать ось."""
        wide = Series("далёкая", (Point(1e15, 1e15),), KIND_DOTS)
        chart = sample_chart()
        self.view.set_chart(Chart(chart.title, chart.x, chart.y, (*chart.series, wide)))
        with_it = self.view.frame().x.hi
        self.view.toggle_series("далёкая")
        self.assertLess(self.view.frame().x.hi, with_it)

    def test_a_frame_drag_zooms_and_a_double_click_resets(self):
        frame = self.view.frame()
        before = (frame.x.lo, frame.x.hi)
        self.view._zoom_to(
            QPointF(frame.left + 40, frame.top + 40),
            QPointF(frame.left + 200, frame.top + 200),
        )
        self.assertTrue(self.view.zoomed)
        self.assertNotEqual((self.view.frame().x.lo, self.view.frame().x.hi), before)
        self.view.reset_zoom()
        self.assertFalse(self.view.zoomed)

    def test_setting_a_new_chart_drops_the_zoom(self):
        """Масштаб был про прежние числа; на новых он означал бы другое."""
        frame = self.view.frame()
        self.view._zoom_to(
            QPointF(frame.left + 40, frame.top + 40),
            QPointF(frame.left + 200, frame.top + 200),
        )
        self.view.set_chart(sample_chart())
        self.assertFalse(self.view.zoomed)

    def test_the_unit_reaches_the_labels(self):
        """График следует переключателю единиц так же, как таблицы."""
        self.view.set_unit(UNIT_GIB)
        self.assertEqual(self.view._unit, UNIT_GIB)
        self.assertFalse(self.view.image(1.0).isNull())

    def test_a_series_hidden_by_the_chart_starts_hidden(self):
        """График один знает, какая серия по умолчанию мешает смотреть."""
        chart = sample_chart()
        quiet = Series("край диапазона", chart.series[0].points, KIND_DOTS, visible=False)
        self.view.set_chart(Chart(chart.title, chart.x, chart.y, (*chart.series, quiet)))
        self.assertEqual(
            [item.name for item in self.view.series() if not item.visible],
            ["край диапазона"],
        )
        self.view.toggle_series("край диапазона")
        self.assertTrue(all(item.visible for item in self.view.series()))

    def test_a_long_note_gets_more_room_than_a_short_one(self):
        """Подпись в одну строку обрезалась ровно на полуслове."""
        chart = sample_chart()
        short = Chart(chart.title, chart.x, chart.y, chart.series, note="коротко")
        long = Chart(
            chart.title,
            chart.x,
            chart.y,
            chart.series,
            note="Ноль — это сама модель. " * 8,
        )
        self.view.set_chart(short)
        low = self.view._note_height(short)
        self.assertGreater(self.view._note_height(long), low)

    def test_an_empty_chart_says_so_instead_of_drawing_nothing(self):
        self.view.set_chart(Chart("Пусто", Axis(""), Axis(""), ()))
        self.assertTrue(self.view.chart().empty)
        self.assertFalse(self.view.image(1.0).isNull())


class StackViewTests(unittest.TestCase):
    def setUp(self):
        points = (
            Point(0.0, 900.0, "данные: 900"),
            Point(0.0, 99.0, "NTFS: 99"),
            Point(0.0, 1.0, "заголовок: 1"),
        )
        self.view = ChartView(
            Chart(
                "Полоса",
                Axis(""),
                Axis(""),
                (Series("слагаемые", points, KIND_STACK),),
                layout=LAYOUT_STACK,
            )
        )
        self.view.resize(600, 240)

    def test_the_bar_is_drawn(self):
        self.assertFalse(self.view.image(1.0).isNull())

    def test_a_segment_answers_where_it_lies(self):
        frame = self.view.frame()
        self.assertIn("данные", self.view.tip_at(frame.left + 10, frame.top + 5))

    def test_the_bar_asks_for_only_the_height_it_uses(self):
        """У полосы нет оси: строка и легенда, и всё.

        Без своей высоты разделитель делит окно поровну, и под полосой висит
        пустое поле в две трети экрана. График с осями своей высоты не просит
        вовсе — ему годится любая, и там переопределять нечего.
        """
        asked = self.view.sizeHint().height()
        self.assertGreater(asked, 0)
        self.assertLess(asked, ChartView(sample_chart()).minimumHeight())


class ChartWindowTests(unittest.TestCase):
    def setUp(self):
        self.built = 0

        def builder():
            self.built += 1
            return sample_chart()

        self.window = ChartWindow("проба", "Проба", (builder, builder), link_x=True)

    def tearDown(self):
        self.window.close()

    def test_a_window_holds_one_view_per_builder(self):
        self.assertEqual(len(self.window.views), 2)
        self.assertEqual(self.built, 2)

    def test_refresh_rebuilds_every_chart(self):
        """Иначе окно молча показывало бы вчерашнюю картинку."""
        self.window.refresh()
        self.assertEqual(self.built, 4)

    def test_linked_charts_share_the_x_axis(self):
        """Горб на остатках должен стоять ровно под своей ступенькой."""
        first, second = self.window.views
        frame = first.frame()
        first._zoom_to(
            QPointF(frame.left + 30, frame.top + 30),
            QPointF(frame.left + 180, frame.top + 180),
        )
        self.assertEqual(first.x_span(), second.x_span())

    def test_linked_charts_keep_their_own_y_axis(self):
        """У остатков размах в килобайтах, у кривой — в мегабайтах."""
        first, second = self.window.views
        frame = first.frame()
        first._zoom_to(
            QPointF(frame.left + 30, frame.top + 30),
            QPointF(frame.left + 180, frame.top + 180),
        )
        self.assertIsNone(second._y)

    def test_the_reset_button_covers_the_whole_window(self):
        first = self.window.views[0]
        frame = first.frame()
        first._zoom_to(QPointF(frame.left + 5, frame.top + 5), QPointF(frame.left + 90, frame.top + 90))
        self.window.reset_zoom()
        self.assertFalse(any(view.zoomed for view in self.window.views))

    def test_a_picked_point_is_explained_in_place(self):
        """Таблица живёт в главном окне; подсвечивать в ней строку некуда."""
        self.window.views[0].pointPicked.emit(1, "том 512\nметаданные 17")
        self.assertIn("том 512", self.window.detail.text())

    def test_the_unit_reaches_every_view(self):
        self.window.set_unit(UNIT_GIB)
        self.assertTrue(all(view._unit is UNIT_GIB for view in self.window.views))


class MainWindowChartTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = Path(tempfile.mkdtemp())
        self.window = MainWindow(data_dir=self.data_dir)
        self.window.records_tab.report_error = lambda *_: None

    def tearDown(self):
        self.window.close()

    def test_every_button_opens_its_window(self):
        for key in (CHART_NTFS, CHART_SLACK, CHART_FORECAST, CHART_CALC):
            with self.subTest(key):
                self.window.open_chart(key)
                self.assertIn(key, self.window._charts)
                self.assertTrue(self.window._charts[key].views)

    def test_opening_twice_raises_the_same_window(self):
        """Второе окно потеряло бы и масштаб, и растянутый руками размер."""
        self.window.open_chart(CHART_NTFS)
        first = self.window._charts[CHART_NTFS]
        self.window.open_chart(CHART_NTFS)
        self.assertIs(self.window._charts[CHART_NTFS], first)

    def test_an_unknown_key_opens_nothing(self):
        self.window.open_chart("такого нет")
        self.assertNotIn("такого нет", self.window._charts)

    def test_a_new_measurement_reaches_the_open_window(self):
        """Иначе окно показывает вчерашнюю картинку, и отличить её нечем.

        Замер кладётся тем же путём, что и автоматический сбор:
        `add_calibration_point` поднял бы модальный диалог, а закрыть его из
        теста нечем — по тому же правилу, по которому логика не поднимает
        модальных окон сама.
        """
        self.window.open_chart(CHART_NTFS)
        window = self.window._charts[CHART_NTFS]
        mine = lambda: len(window.views[0].chart().series[1].points)
        before = mine()
        point = factory_data().points[1]
        self.window.records_tab.store_calibration_point(
            Record(
                id="свой замер",
                container_mib=point.container_mib,
                mounted_bytes=point.mounted_bytes,
                empty_free_bytes=point.empty_free_bytes - 4096,
            )
        )
        self.window._on_records_changed()
        self.assertEqual(mine(), before + 1)

    def test_the_calc_window_follows_the_calculation(self):
        """Расчёт меняется от каждого нажатия, записи — нет."""
        self.window.open_chart(CHART_CALC)
        window = self.window._charts[CHART_CALC]
        window.show()
        self.window.calc_tab.size_edit.setText("700 000 000")
        self.window.calc_tab.recalculate()
        self.assertIn("контейнер", window.views[0].chart().title.lower())

    def test_the_geometry_is_stored_by_name(self):
        """От перестановки окон список номеров разъехался бы молча."""
        self.window.open_chart(CHART_SLACK)
        self.window._store_preferences()
        self.assertIsNotNone(self.window.settings.value("chart_slack/geometry"))

    def test_the_unit_switch_reaches_open_windows(self):
        self.window.open_chart(CHART_NTFS)
        index = self.window.unit_combo.findData("GiB")
        self.window.unit_combo.setCurrentIndex(index)
        self.assertTrue(
            all(view._unit is UNIT_GIB for view in self.window._charts[CHART_NTFS].views)
        )

    def test_an_empty_calculation_explains_itself(self):
        """Пустое место без надписи не отличить от поломки."""
        self.window.open_chart(CHART_CALC)
        window = self.window._charts[CHART_CALC]
        chart = window.views[1].chart()
        self.assertTrue(chart.empty)
        self.assertIn("источники", chart.note)


if __name__ == "__main__":
    unittest.main()
