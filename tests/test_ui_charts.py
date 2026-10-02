"""The chart widget, the chart windows and their link to the main window."""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QSplitter  # noqa: E402

_app = QApplication.instance() or QApplication([])

_settings_dir = tempfile.mkdtemp(prefix="containerhelper-charts-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from containerhelper.factory import factory_data  # noqa: E402
from containerhelper.formatting import UNIT_GIB  # noqa: E402
from containerhelper.i18n import tr  # noqa: E402
from containerhelper.model import MIB  # noqa: E402
from containerhelper.records import Record  # noqa: E402
from containerhelper.plot import (  # noqa: E402
    AXIS_BYTES,
    KIND_BARS,
    KIND_DOTS,
    KIND_STEMS,
    KIND_LINE,
    KIND_STACK,
    LAYOUT_STACK,
    Axis,
    Chart,
    Point,
    Series,
)
from containerhelper.ui.app import MainWindow  # noqa: E402
from containerhelper.ui.chart import (  # noqa: E402
    DETACH_TEXT,
    RETURN_TEXT,
    ChartView,
)
from containerhelper.ui.chart_window import (  # noqa: E402
    CHART_CALC,
    CHART_FORECAST,
    CHART_NTFS,
    CHART_SLACK,
    ChartWindow,
    EvenHandle,
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
        """A margin picked by eye cuts off "1 099 511 627 776".

        It does so exactly when such a number appears, and it appears on a
        terabyte volume.
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
        self.view.image(1.0)  # the legend is laid out while painting
        self.view.toggle_series("модель")
        self.assertEqual(
            [item.name for item in self.view.series() if item.visible], ["замеры"]
        )
        self.view.toggle_series("модель")
        self.assertEqual(len([i for i in self.view.series() if i.visible]), 2)

    def test_a_hidden_series_stops_holding_the_scale(self):
        """Otherwise a switched-off series would keep stretching the axis."""
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
        """The zoom was about the old numbers.

        On new ones it would mean something else.
        """
        frame = self.view.frame()
        self.view._zoom_to(
            QPointF(frame.left + 40, frame.top + 40),
            QPointF(frame.left + 200, frame.top + 200),
        )
        self.view.set_chart(sample_chart())
        self.assertFalse(self.view.zoomed)

    def test_the_unit_reaches_the_labels(self):
        """The chart follows the unit switch just as the tables do."""
        self.view.set_unit(UNIT_GIB)
        self.assertEqual(self.view._unit, UNIT_GIB)
        self.assertFalse(self.view.image(1.0).isNull())

    def test_a_series_hidden_by_the_chart_starts_hidden(self):
        """Only the chart knows which series gets in the way by default."""
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
        """A one-line note was cut off right in the middle of a word."""
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
        """The bar has no axis: one line and a legend, and that is all.

        Without a height of its own, the splitter divides the window equally,
        and an empty area two thirds of the screen high hangs under the bar. A
        chart with axes asks for no height of its own at all: any height suits
        it, and there is nothing to override there.
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
        """Otherwise the window would silently show yesterday's picture."""
        self.window.refresh()
        self.assertEqual(self.built, 4)

    def test_linked_charts_share_the_x_axis(self):
        """A hump in the residuals must stand exactly under its step."""
        first, second = self.window.views
        frame = first.frame()
        first._zoom_to(
            QPointF(frame.left + 30, frame.top + 30),
            QPointF(frame.left + 180, frame.top + 180),
        )
        self.assertEqual(first.x_span(), second.x_span())

    def test_linked_charts_keep_their_own_y_axis(self):
        """The residuals span kilobytes, the curve spans megabytes."""
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
        """The table lives in the main window; no row to highlight here."""
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
        """A second window would lose the zoom and the size set by hand."""
        self.window.open_chart(CHART_NTFS)
        first = self.window._charts[CHART_NTFS]
        self.window.open_chart(CHART_NTFS)
        self.assertIs(self.window._charts[CHART_NTFS], first)

    def test_an_unknown_key_opens_nothing(self):
        self.window.open_chart("такого нет")
        self.assertNotIn("такого нет", self.window._charts)

    def test_a_new_measurement_reaches_the_open_window(self):
        """Otherwise the window shows yesterday's picture.

        And nothing tells it apart from today's. The measurement is stored
        the same way automatic collection stores it: `add_calibration_point`
        would raise a modal dialog, and the test has nothing to close it
        with. It is the same rule by which logic raises no modal windows
        itself.
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

    def test_a_new_measurement_keeps_the_scale(self):
        """Collection runs step by step; each step would snap to full view.

        Examining a section of the curve during collection was impossible
        altogether, and that is exactly what the window is opened for.
        """
        self.window.open_chart(CHART_NTFS)
        window = self.window._charts[CHART_NTFS]
        view = window.views[0]
        frame = view.frame()
        # The middle of the plot area, not a corner: a corner may hold no point
        # at all, and the zoom would be reset by the "nothing in the frame"
        # rule.
        view._zoom_to(
            QPointF(frame.left + frame.width * 0.2, frame.top + frame.height * 0.2),
            QPointF(frame.left + frame.width * 0.8, frame.top + frame.height * 0.8),
        )
        span = view.x_span()

        point = factory_data().points[2]
        self.window.records_tab.store_calibration_point(
            Record(
                id="свой замер",
                container_mib=point.container_mib,
                mounted_bytes=point.mounted_bytes,
                empty_free_bytes=point.empty_free_bytes - 4096,
            )
        )
        self.window._on_records_changed()
        self.assertTrue(view.zoomed)
        self.assertEqual(view.x_span(), span)

    def test_a_picked_point_follows_the_data(self):
        """A measurement may have changed; the line would show old numbers."""
        self.window.open_chart(CHART_NTFS)
        window = self.window._charts[CHART_NTFS]
        point = window.views[0].chart().series[2].points[0]
        window._on_picked(point.key, point.tip)
        before = window.detail.text()
        self.assertTrue(before)
        window.refresh()
        self.assertEqual(window.detail.text(), before)

    def test_a_vanished_point_empties_the_line(self):
        self.window.open_chart(CHART_NTFS)
        window = self.window._charts[CHART_NTFS]
        window._on_picked("такой точки нет", "описание")
        window.refresh()
        self.assertEqual(window.detail.text(), "")

    def test_the_calc_window_follows_the_calculation(self):
        """The calculation changes with every keypress; the records do not."""
        self.window.open_chart(CHART_CALC)
        window = self.window._charts[CHART_CALC]
        window.show()
        self.window.calc_tab.size_edit.setText("700 000 000")
        self.window.calc_tab.recalculate()
        self.assertIn("контейнер", window.views[0].chart().title.lower())

    def test_the_geometry_is_stored_by_name(self):
        """Rearranging windows would silently shift a list stored by number."""
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
        """An empty area with no caption cannot be told apart from a fault."""
        self.window.open_chart(CHART_CALC)
        window = self.window._charts[CHART_CALC]
        chart = window.views[1].chart()
        self.assertTrue(chart.empty)
        self.assertIn("источники", chart.note)


def mouse(view, kind, button, position):
    """A mouse event right into the widget: in tests no one else sends it."""
    point = QPointF(*position)
    event = QMouseEvent(kind, point, point, button, button, Qt.NoModifier)
    _app.sendEvent(view, event)


def middle_of(view):
    frame = view.frame()
    return frame.left + frame.width / 2, frame.top + frame.height / 2


class ChartMouseTests(unittest.TestCase):
    """Right button pans, left selects, a double click brings up the menu."""

    def setUp(self):
        self.view = ChartView(sample_chart())
        self.view.resize(640, 420)

    def test_the_right_button_drags_the_view(self):
        centre = middle_of(self.view)
        before = self.view.frame().x.lo
        mouse(self.view, QMouseEvent.MouseButtonPress, Qt.RightButton, centre)
        mouse(
            self.view,
            QMouseEvent.MouseMove,
            Qt.RightButton,
            (centre[0] - 60, centre[1]),
        )
        mouse(
            self.view,
            QMouseEvent.MouseButtonRelease,
            Qt.RightButton,
            (centre[0] - 60, centre[1]),
        )
        self.assertNotEqual(self.view.frame().x.lo, before)

    def test_a_double_right_click_resets(self):
        frame = self.view.frame()
        self.view._zoom_to(
            QPointF(frame.left + 40, frame.top + 40),
            QPointF(frame.left + 200, frame.top + 200),
        )
        mouse(
            self.view,
            QMouseEvent.MouseButtonDblClick,
            Qt.RightButton,
            middle_of(self.view),
        )
        self.assertFalse(self.view.zoomed)

    def test_a_double_left_click_does_not_reset(self):
        """The reset moved to the right button, following the pan."""
        frame = self.view.frame()
        self.view._zoom_to(
            QPointF(frame.left + 40, frame.top + 40),
            QPointF(frame.left + 200, frame.top + 200),
        )
        self.view._show_menu = lambda where: None
        mouse(
            self.view,
            QMouseEvent.MouseButtonDblClick,
            Qt.LeftButton,
            middle_of(self.view),
        )
        self.assertTrue(self.view.zoomed)

    def test_the_menu_comes_from_the_double_left_click_and_the_middle_button(self):
        opened = []
        self.view._show_menu = lambda where: opened.append(where)
        mouse(
            self.view,
            QMouseEvent.MouseButtonDblClick,
            Qt.LeftButton,
            middle_of(self.view),
        )
        mouse(
            self.view,
            QMouseEvent.MouseButtonPress,
            Qt.MiddleButton,
            middle_of(self.view),
        )
        self.assertEqual(len(opened), 2)

    def test_the_right_button_no_longer_opens_a_menu_of_its_own(self):
        """Otherwise the menu pops up over the chart on every pan."""
        self.assertEqual(self.view.contextMenuPolicy(), Qt.PreventContextMenu)

    def test_the_menu_offers_a_reset_only_when_there_is_something_to_reset(self):
        menu = self.view.build_menu()
        self.assertFalse(menu.actions()[0].isEnabled())
        frame = self.view.frame()
        self.view._zoom_to(
            QPointF(frame.left + 40, frame.top + 40),
            QPointF(frame.left + 200, frame.top + 200),
        )
        self.assertTrue(self.view.build_menu().actions()[0].isEnabled())


class CrosshairTests(unittest.TestCase):
    def setUp(self):
        self.view = ChartView(sample_chart())
        self.view.resize(640, 420)

    def test_the_cursor_is_remembered_and_forgotten(self):
        mouse(self.view, QMouseEvent.MouseMove, Qt.NoButton, middle_of(self.view))
        self.assertIsNotNone(self.view._cursor)
        self.view.leaveEvent(QEvent(QEvent.Leave))
        self.assertIsNone(self.view._cursor)

    def test_it_tells_the_neighbours_where_it_stands(self):
        seen = []
        self.view.cursorMoved.connect(seen.append)
        frame = self.view.frame()
        mouse(self.view, QMouseEvent.MouseMove, Qt.NoButton, middle_of(self.view))
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], frame.x.lo)
        self.assertLess(seen[0], frame.x.hi)

    def test_leaving_the_frame_says_so(self):
        left = []
        self.view.cursorLeft.connect(lambda: left.append(True))
        mouse(self.view, QMouseEvent.MouseMove, Qt.NoButton, (2.0, 2.0))
        self.assertTrue(left)

    def test_a_linked_crosshair_is_kept(self):
        self.view.set_linked_cursor(4096.0 * MIB)
        self.assertEqual(self.view._linked_x, 4096.0 * MIB)
        self.assertFalse(self.view.image(1.0).isNull())

    def test_the_crosshair_stays_out_of_the_saved_picture(self):
        """In a picture a crosshair would say something was measured there."""
        plain = self.view.image(1.0).toImage()
        mouse(self.view, QMouseEvent.MouseMove, Qt.NoButton, middle_of(self.view))
        with_cursor = self.view.image(1.0).toImage()
        self.assertEqual(plain, with_cursor)


class KeepViewTests(unittest.TestCase):
    """A data update does not reset what the user has set up by hand."""

    def setUp(self):
        self.view = ChartView(sample_chart())
        self.view.resize(640, 420)
        frame = self.view.frame()
        self.view._zoom_to(
            QPointF(frame.left + 10, frame.top + 10),
            QPointF(frame.left + 300, frame.top + 300),
        )

    def test_the_zoom_survives_fresh_data(self):
        span = self.view.x_span()
        self.view.set_chart(sample_chart(), keep_view=True)
        self.assertEqual(self.view.x_span(), span)

    def test_hidden_series_survive_fresh_data(self):
        self.view.toggle_series("модель")
        self.view.set_chart(sample_chart(), keep_view=True)
        self.assertIn("модель", self.view._hidden)

    def test_a_series_shown_by_hand_stays_shown(self):
        """A series hidden by default and shown with a click stays shown."""
        hidden = Series("край", (Point(1.0, 1.0),), KIND_DOTS, visible=False)
        chart = Chart("проба", Axis("x"), Axis("y"), (hidden,))
        self.view.set_chart(chart)
        self.assertIn("край", self.view._hidden)
        self.view.toggle_series("край")
        self.view.set_chart(chart, keep_view=True)
        self.assertNotIn("край", self.view._hidden)

    def test_a_hidden_series_survives_a_new_name(self):
        """A language switch renames every series; what was hidden by key
        stays hidden, and an edge series hidden by the chart stays in the
        shade."""
        def chart(model: str, edge: str) -> Chart:
            return Chart(
                "t",
                Axis("x"),
                Axis("y"),
                (
                    Series(model, (Point(1.0, 1.0),), KIND_LINE, key="model"),
                    Series(edge, (Point(2.0, 2.0),), KIND_DOTS, visible=False, key="edge"),
                ),
            )

        self.view.set_chart(chart("модель", "край"))
        self.view.toggle_series("model")
        self.view.set_chart(chart("model", "edge"), keep_view=True)
        self.assertEqual(self.view._hidden, {"model", "edge"})
        self.assertEqual([item.visible for item in self.view.series()], [False, False])

    def test_points_outside_the_view_are_counted(self):
        self.view.set_chart(sample_chart(), keep_view=True)
        self.assertGreater(self.view._outside, 0)

    def test_an_empty_view_gives_the_scale_back(self):
        """A zoom with not a single point left in it is not worth keeping."""
        far = Chart(
            "далеко",
            Axis("Размер тома", AXIS_BYTES, log=True),
            Axis("Метаданные", AXIS_BYTES),
            (Series("замеры", (Point(2.0 ** 42, 2.0 ** 30, "далеко"),), KIND_DOTS),),
        )
        self.view.set_chart(far, keep_view=True)
        self.assertFalse(self.view.zoomed)
        self.assertEqual(self.view._outside, 0)


class DetachTests(unittest.TestCase):
    def setUp(self):
        self.window = ChartWindow(
            "проба", "Проба", (sample_chart, sample_chart, sample_chart), link_x=True
        )

    def tearDown(self):
        self.window.close()

    def test_a_chart_leaves_and_comes_back_to_its_place(self):
        self.window.detach(1)
        self.assertEqual(self.window.detached_indexes(), [1])
        self.assertEqual(self.window.splitter.count(), 2)
        self.window.attach(1)
        self.assertEqual(self.window.detached_indexes(), [])
        self.assertIs(self.window.splitter.widget(1), self.window.views[1])

    def test_the_common_window_gives_up_the_height(self):
        before = self.window.height()
        self.window.detach(0)
        self.assertLess(self.window.height(), before)

    def test_a_detached_chart_still_gets_fresh_data(self):
        """The window's list goes by number, not by where each chart lives."""
        self.window.detach(2)
        self.window.refresh()
        self.assertIsNotNone(self.window.views[2].chart())

    def test_a_synced_window_keeps_the_common_x(self):
        self.window.detach(1)
        first, second = self.window.views[0], self.window.views[1]
        frame = first.frame()
        first._zoom_to(
            QPointF(frame.left + 30, frame.top + 30),
            QPointF(frame.left + 180, frame.top + 180),
        )
        self.assertEqual(first.x_span(), second.x_span())

    def test_without_the_checkbox_the_window_lives_on_its_own(self):
        self.window.detach(1)
        self.window._windows[1].sync_check.setChecked(False)
        first, second = self.window.views[0], self.window.views[1]
        frame = first.frame()
        first._zoom_to(
            QPointF(frame.left + 30, frame.top + 30),
            QPointF(frame.left + 180, frame.top + 180),
        )
        self.assertIsNone(second.x_span())

    def test_an_unsynced_window_does_not_drag_the_others(self):
        self.window.detach(0)
        self.window._windows[0].sync_check.setChecked(False)
        first, second = self.window.views[0], self.window.views[1]
        frame = first.frame()
        first._zoom_to(
            QPointF(frame.left + 30, frame.top + 30),
            QPointF(frame.left + 180, frame.top + 180),
        )
        self.assertIsNone(second.x_span())

    def test_the_crosshair_reaches_the_synced_neighbour(self):
        first, second = self.window.views[0], self.window.views[1]
        first.resize(640, 300)
        mouse(first, QMouseEvent.MouseMove, Qt.NoButton, middle_of(first))
        self.assertIsNotNone(second._linked_x)

    def test_a_single_chart_window_offers_no_detaching(self):
        """A single chart has nothing to be detached from."""
        alone = ChartWindow("один", "Один", (sample_chart,))
        self.assertFalse(alone.views[0].detachable)
        alone.close()

    def test_the_layout_is_stored_and_restored(self):
        settings = QSettings(
            str(Path(tempfile.mkdtemp()) / "settings.ini"), QSettings.IniFormat
        )
        self.window.detach(1)
        self.window._windows[1].sync_check.setChecked(False)
        self.window.save_layout(settings)

        again = ChartWindow(
            "проба", "Проба", (sample_chart, sample_chart, sample_chart), link_x=True
        )
        again.restore_layout(settings)
        self.assertEqual(again.detached_indexes(), [1])
        self.assertFalse(again._windows[1].sync_check.isChecked())
        again.close()


class NegativeBarTests(unittest.TestCase):
    """A bar with a negative value is filled downward from zero."""

    def setUp(self):
        wide = Series(
            "перезаклад",
            (Point(0.0, 5.0, "a"), Point(1.0, -3.0, "b")),
            KIND_BARS,
            tone=0,
        )
        bare = Series(
            "без страховки",
            (Point(0.0, 1.0, "a"), Point(1.0, -6.0, "b")),
            KIND_DOTS,
            tone=1,
        )
        self.chart = Chart(
            "промахи",
            Axis("Запись"),
            Axis("MiB"),
            (wide, bare),
            zero_line=True,
            categories=("a", "b"),
        )
        self.view = ChartView(self.chart)
        self.view.resize(640, 420)

    def test_zero_stays_in_the_frame(self):
        frame = self.view.frame()
        self.assertLessEqual(frame.y.lo, 0.0)
        self.assertGreaterEqual(frame.y.hi, 0.0)

    def test_a_negative_bar_is_painted_below_the_zero_line(self):
        frame = self.view.frame()
        image = self.view.image(1.0).toImage()
        background = image.pixelColor(3, 3)
        x = int(frame.px(1.0))
        middle = int((frame.py(0.0) + frame.py(-3.0)) / 2)
        self.assertNotEqual(image.pixelColor(x, middle), background)

    def test_the_dot_series_stays_dots(self):
        """A dot below zero is a separate quantity, not part of the bar."""
        frame = self.view.frame()
        image = self.view.image(1.0).toImage()
        background = image.pixelColor(3, 3)
        x = int(frame.px(1.0))
        # A dot is a dot: its colour must not appear between it and the end of
        # the bar, otherwise it was drawn as a bar all the way to zero.
        dot = image.pixelColor(x, int(frame.py(-6.0)))
        self.assertNotEqual(dot, background)
        between = int((frame.py(-3.0) + frame.py(-6.0)) / 2)
        self.assertNotEqual(image.pixelColor(x, between), dot)


def long_caption_chart() -> Chart:
    return Chart(
        "проба",
        Axis("Размер тома", AXIS_BYTES, log=True),
        Axis("Измерено минус модель этого замера и соседних", AXIS_BYTES),
        (Series("точки", (Point(1.0, 1.0), Point(2.0, 2.0)), KIND_DOTS),),
    )


class CaptionFitTests(unittest.TestCase):
    """A Y axis label longer than the plot area is the norm, not an exception.

    «Измерено минус модель, B» is 288 pixels against a plot area 186 high in
    a window with three charts: the label was cut right in the middle of a
    word.
    """

    def test_a_long_caption_gets_a_second_line(self):
        view = ChartView(long_caption_chart())
        view.resize(640, 260)
        chart, frame = view.chart(), view.frame()
        self.assertEqual(view._y_caption_lines(chart, frame.y), 2)

    def test_the_second_line_gets_its_own_room_on_the_left(self):
        """Otherwise the second line lies on top of the tick labels."""
        short = Chart(
            "проба",
            Axis("x", AXIS_BYTES, log=True),
            Axis("Y", AXIS_BYTES),
            (Series("точки", (Point(1.0, 1.0), Point(2.0, 2.0)), KIND_DOTS),),
        )
        one = ChartView(short)
        one.resize(640, 260)
        two = ChartView(long_caption_chart())
        two.resize(640, 260)
        self.assertGreater(two.frame().left, one.frame().left)

    def test_the_caption_room_is_measured_by_the_widget_not_the_frame(self):
        """The label sits beside the plot area, not limited by its height."""
        view = ChartView(sample_chart())
        view.resize(640, 300)
        self.assertGreater(view._y_caption_room(), view.frame().height)

    def test_both_lines_of_the_caption_are_inside_the_widget(self):
        """After rotate(-90) the y coordinate turns into screen x.

        With a rectangle growing to the left, the label was drawn left of the
        padding: on one line it lost its edge, and the second line missed the
        widget entirely. From the outside this is exactly what looked like
        "the axis label is cut off".
        """
        from PySide6.QtGui import QFontMetricsF

        from containerhelper.ui.chart import PADDING

        view = ChartView(long_caption_chart())
        view.resize(640, 260)
        image = view.image(1.0).toImage()
        background = image.pixelColor(view.width() - 3, 3)
        rows = range(int(view.height() * 0.3), int(view.height() * 0.7))

        def inked(columns):
            return [
                x
                for x in columns
                if any(image.pixelColor(x, y) != background for y in rows)
            ]

        self.assertEqual(inked(range(0, PADDING)), [])
        painted = inked(range(0, int(view.frame().left)))
        self.assertTrue(painted)
        # Two lines take twice the room of one, so both are drawn.
        metrics = QFontMetricsF(view.font())
        self.assertGreater(painted[-1] - painted[0], metrics.lineSpacing())

    def test_a_long_title_is_elided_not_cut_in_half(self):
        chart = Chart(
            "Очень длинный заголовок, который заведомо не влезает в узкое окно",
            Axis("x"),
            Axis("y"),
            (Series("точки", (Point(1.0, 1.0),), KIND_DOTS),),
        )
        view = ChartView(chart)
        view.resize(200, 200)
        self.assertFalse(view.image(1.0).isNull())


class TitleBandTests(unittest.TestCase):
    """The title must fit entirely inside the widget.

    The title band was measured upward from the padding (`PADDING - height`)
    and started two pixels above zero: the tops of the letters were cut off,
    and to the eye it looked like the splitter between charts creeping down
    from above.
    """

    def setUp(self):
        self.view = ChartView(sample_chart())
        self.view.resize(640, 300)

    def painted_rows(self):
        image = self.view.image(1.0).toImage()
        background = image.pixelColor(3, self.view.height() // 2)
        middle = range(self.view.width() // 2 - 120, self.view.width() // 2 + 120)
        # Only the title band: the plot area itself starts below it, and its
        # pixels have nothing to do with the title.
        return [
            y
            for y in range(0, int(self.view.frame().top))
            if any(image.pixelColor(x, y) != background for x in middle)
        ]

    def test_the_title_starts_below_the_top_edge(self):
        rows = self.painted_rows()
        self.assertTrue(rows)
        self.assertGreater(rows[0], 0)

    def test_the_title_stays_out_of_the_plot(self):
        self.assertLessEqual(max(self.painted_rows()), self.view.frame().top)

    def test_the_plot_starts_below_the_head(self):
        self.view.set_detachable(True)
        self.assertGreaterEqual(
            self.view.frame().top, self.view.detach_button.height()
        )


class DetachButtonTests(unittest.TestCase):
    def setUp(self):
        self.view = ChartView(sample_chart())
        self.view.resize(640, 300)

    def test_there_is_no_button_where_there_is_nothing_to_detach(self):
        self.assertFalse(self.view.detach_button.isVisible())

    def test_the_button_shows_up_and_names_the_action(self):
        self.view.set_detachable(True)
        self.view.show()
        _app.processEvents()
        self.assertTrue(self.view.detach_button.isVisible())
        self.assertEqual(self.view.detach_button.text(), tr(DETACH_TEXT))
        self.assertIn("отдельном окне", self.view.detach_button.toolTip())
        self.view.set_detached(True)
        self.assertEqual(self.view.detach_button.text(), tr(RETURN_TEXT))

    def test_the_corner_leaves_the_title_its_room(self):
        """The buttons sit in the title row and take width away from the title.

        In the grid there are five of them on a chart half the usual width,
        and at QToolButton's own size the four arrows ate two hundred pixels
        of the title.
        """
        self.view.set_detachable(True)
        self.view.set_place(1, 4, 2)
        self.view.show()
        _app.processEvents()
        self.assertLess(self.view._button_width(), self.view.width() / 3)

    def test_the_button_asks_the_window_to_move_the_chart(self):
        asked = []
        self.view.detachRequested.connect(lambda: asked.append(True))
        self.view.set_detachable(True)
        self.view.detach_button.click()
        self.assertEqual(len(asked), 1)

    def test_the_button_sits_in_the_top_right_corner(self):
        self.view.set_detachable(True)
        self.view.show()
        _app.processEvents()
        button = self.view.detach_button
        self.assertLessEqual(button.x() + button.width(), self.view.width())
        self.assertGreater(button.x(), self.view.width() / 2)
        self.assertLess(button.y(), 10)

    def test_the_button_stays_out_of_the_saved_picture(self):
        """It has no place in the picture: it is a control, not the chart."""
        self.view.set_detachable(True)
        self.view.show()
        _app.processEvents()
        image = self.view.image(1.0).toImage()
        background = image.pixelColor(3, self.view.height() // 2)
        corner = image.pixelColor(self.view.width() - 20, 8)
        self.assertEqual(corner, background)


class SplitterTests(unittest.TestCase):
    def setUp(self):
        self.window = ChartWindow(
            "проба", "Проба", (sample_chart, sample_chart, sample_chart), link_x=True
        )
        self.window.resize(760, 780)
        self.window.show()
        _app.processEvents()

    def tearDown(self):
        self.window.close()

    def test_the_handles_know_the_double_click(self):
        handle = self.window.splitter.handle(1)
        self.assertIsInstance(handle, EvenHandle)
        self.assertTrue(handle.toolTip())

    def test_a_double_click_evens_the_heights(self):
        """Dragging the splitter can never get back to "equal"."""
        self.window.splitter.setSizes([500, 200, 100])
        _app.processEvents()
        self.window.splitter.even_out()
        sizes = self.window.splitter.sizes()
        self.assertLessEqual(max(sizes) - min(sizes), 2)

    def test_the_window_keeps_its_height_across_a_round_trip(self):
        """The layout minimum blocks shrinking; nothing blocks growing back."""
        before = self.window.height()
        for _ in range(3):
            self.window.detach(1)
            _app.processEvents()
            self.window.attach(1)
            _app.processEvents()
        self.assertEqual(self.window.height(), before)

    def test_detaching_shrinks_the_common_window(self):
        before = self.window.height()
        self.window.detach(1)
        _app.processEvents()
        self.assertLess(self.window.height(), before)

    def test_the_two_sizes_are_kept_apart(self):
        """Common-window height and own-window size are different values."""
        self.window.detach(1)
        _app.processEvents()
        self.window._windows[1].resize(900, 320)
        _app.processEvents()
        self.window.attach(1)
        _app.processEvents()
        self.window.detach(1)
        _app.processEvents()
        self.assertEqual(self.window._windows[1].height(), 320)

    def test_the_height_stays_with_its_own_chart(self):
        """Charts swap places, sizes do not.

        Swapping heights on top of swapping places left one thing in memory
        and another on screen, and the very next layout change handed the
        charts each other's sizes.
        """
        self.window.resize(760, 1200)
        _app.processEvents()
        self.window.splitter.setSizes([500, 260, 220])
        _app.processEvents()
        before = list(self.window.splitter.sizes())
        self.window.move_view(0, 1)
        _app.processEvents()
        after = self.window.splitter.sizes()
        # The first chart moved to second place, and its height moved with it.
        self.assertEqual(after[1], before[0])
        self.assertEqual(after[0], before[1])


class StemDrawingTests(unittest.TestCase):
    def setUp(self):
        points = (Point(1.0, 4.0, "вверх"), Point(2.0, -3.0, "вниз"))
        self.chart = Chart(
            "остатки",
            Axis("x"),
            Axis("y"),
            (Series("промах", points, KIND_STEMS, tone=0),),
            zero_line=True,
        )
        self.view = ChartView(self.chart)
        self.view.resize(640, 420)

    def test_the_stem_reaches_from_zero_to_the_dot(self):
        frame = self.view.frame()
        image = self.view.image(1.0).toImage()
        background = image.pixelColor(3, 3)
        x = int(frame.px(1.0))
        middle = int((frame.py(0.0) + frame.py(4.0)) / 2)
        self.assertNotEqual(image.pixelColor(x, middle), background)

    def test_a_stem_goes_down_as_well(self):
        frame = self.view.frame()
        image = self.view.image(1.0).toImage()
        background = image.pixelColor(3, 3)
        x = int(frame.px(2.0))
        middle = int((frame.py(0.0) + frame.py(-3.0)) / 2)
        self.assertNotEqual(image.pixelColor(x, middle), background)

    def test_the_stem_is_thinner_than_a_bar(self):
        """The value stays a dot: the stem only shows the deviation."""
        bars = Chart(
            "столбики",
            Axis("x"),
            Axis("y"),
            (Series("промах", self.chart.series[0].points, KIND_BARS, tone=0),),
            zero_line=True,
        )
        other = ChartView(bars)
        other.resize(640, 420)

        def painted(view):
            """Width painted in the series colour halfway from zero to the dot.

            By that colour, not by "not background": a full-width grid line
            also lies at this height, and it alone outweighs any difference.
            """
            frame = view.frame()
            image = view.image(1.0).toImage()
            row = int((frame.py(0.0) + frame.py(4.0)) / 2)
            centre = int(frame.px(1.0))
            colour = image.pixelColor(centre, row)
            return sum(
                1
                for x in range(centre - 40, centre + 41)
                if image.pixelColor(x, row) == colour
            )

        self.assertLess(painted(self.view), painted(other))


class CompactHeightTests(unittest.TestCase):
    """The splitter gave the bar as much room as a real chart."""

    def bar(self):
        points = tuple(Point(0.0, float(value), f"часть {value}") for value in (5, 3, 2))
        return Chart(
            "полоса",
            Axis(""),
            Axis(""),
            (Series("слагаемые", points, KIND_STACK),),
            layout=LAYOUT_STACK,
        )

    def test_the_bar_asks_for_less_than_a_chart(self):
        bar = ChartView(self.bar())
        plain = ChartView(sample_chart())
        self.assertLess(bar.sizeHint().height(), plain.minimumHeight())

    def test_the_bar_lowers_its_own_minimum(self):
        """An explicit widget minimum beats minimumSizeHint."""
        view = ChartView(self.bar())
        self.assertEqual(view.minimumHeight(), view.sizeHint().height())

    def test_a_plain_chart_keeps_the_common_minimum(self):
        from containerhelper.ui.chart import MIN_HEIGHT

        view = ChartView(sample_chart())
        self.assertEqual(view.minimumHeight(), MIN_HEIGHT)


class SplitSyncTests(unittest.TestCase):
    """Zoom and crosshair are linked separately."""

    def setUp(self):
        self.window = ChartWindow(
            "проба", "Проба", (sample_chart, sample_chart), link_x=True
        )
        self.window.show()
        _app.processEvents()
        self.window.detach(1)
        _app.processEvents()
        self.detached = self.window._windows[1]

    def tearDown(self):
        self.window.close()

    def test_the_window_offers_two_checkboxes(self):
        self.assertTrue(self.detached.sync_check.isVisibleTo(self.detached))
        self.assertTrue(self.detached.cross_check.isVisibleTo(self.detached))

    def test_the_crosshair_survives_an_unlinked_scale(self):
        """The line goes by value; each chart draws it at its own scale."""
        self.detached.sync_check.setChecked(False)
        first, second = self.window.views
        first.resize(640, 300)
        mouse(first, QMouseEvent.MouseMove, Qt.NoButton, middle_of(first))
        self.assertIsNotNone(second._linked_x)

    def test_the_second_checkbox_stops_the_crosshair(self):
        self.detached.cross_check.setChecked(False)
        first, second = self.window.views
        second.set_linked_cursor(None)
        first.resize(640, 300)
        mouse(first, QMouseEvent.MouseMove, Qt.NoButton, middle_of(first))
        self.assertIsNone(second._linked_x)

    def test_both_flags_are_stored(self):
        settings = QSettings(
            str(Path(tempfile.mkdtemp()) / "settings.ini"), QSettings.IniFormat
        )
        self.detached.cross_check.setChecked(False)
        self.window.save_layout(settings)
        again = ChartWindow(
            "проба", "Проба", (sample_chart, sample_chart), link_x=True
        )
        again.restore_layout(settings)
        self.assertFalse(again._windows[1].cross_check.isChecked())
        self.assertTrue(again._windows[1].sync_check.isChecked())
        again.close()


class GridLayoutTests(unittest.TestCase):
    """A column of four charts asks for 1000 px of height, a grid for half."""

    def setUp(self):
        self.window = ChartWindow(
            "проба",
            "Проба",
            (sample_chart, sample_chart, sample_chart, sample_chart),
            link_x=True,
        )
        self.window.show()
        _app.processEvents()

    def tearDown(self):
        self.window.close()

    def rows(self):
        """What the splitter holds: a row of several, or a single chart."""
        out = []
        for place in range(self.window.splitter.count()):
            item = self.window.splitter.widget(place)
            if isinstance(item, QSplitter):
                out.append([item.widget(i) for i in range(item.count())])
            else:
                out.append([item])
        return out

    def test_a_column_puts_one_chart_in_a_row(self):
        self.assertEqual([len(row) for row in self.rows()], [1, 1, 1, 1])

    def test_a_grid_puts_two(self):
        self.window.set_grid(True)
        _app.processEvents()
        self.assertEqual([len(row) for row in self.rows()], [2, 2])

    def test_the_charts_keep_their_order_in_the_grid(self):
        self.window.set_grid(True)
        _app.processEvents()
        flat = [view for row in self.rows() for view in row]
        self.assertEqual(flat, self.window.views)

    def test_an_odd_last_chart_takes_the_whole_row(self):
        """An empty half of a row is just wasted space."""
        self.window.detach(3)
        self.window.set_grid(True)
        _app.processEvents()
        self.assertEqual([len(row) for row in self.rows()], [2, 1])

    def test_going_back_to_a_column_restores_the_rows(self):
        self.window.set_grid(True)
        _app.processEvents()
        self.window.set_grid(False)
        _app.processEvents()
        self.assertEqual([len(row) for row in self.rows()], [1, 1, 1, 1])
        self.assertEqual([row[0] for row in self.rows()], self.window.views)

    def test_the_button_names_the_other_layout(self):
        self.assertEqual(self.window.grid_button.text(), "Сеткой")
        self.window.grid_button.click()
        _app.processEvents()
        self.assertEqual(self.window.grid_button.text(), "Столбцом")

    def test_each_layout_keeps_its_own_window_size(self):
        """A grid needs width, a column height: one number cannot hold both."""
        # A height above the layout minimum: four charts in a column ask for
        # close to a thousand on their own, and the window simply will not
        # accept a smaller number.
        self.window.resize(700, 1100)
        _app.processEvents()
        tall = self.window.height()
        self.window.set_grid(True)
        _app.processEvents()
        self.window.resize(1200, 600)
        _app.processEvents()
        wide = self.window.width()
        self.window.set_grid(False)
        _app.processEvents()
        self.assertEqual(self.window.height(), tall)
        self.window.set_grid(True)
        _app.processEvents()
        self.assertEqual(self.window.width(), wide)

    def test_a_detached_chart_stays_out_of_the_grid(self):
        self.window.detach(1)
        self.window.set_grid(True)
        _app.processEvents()
        flat = [view for row in self.rows() for view in row]
        self.assertNotIn(self.window.views[1], flat)
        self.assertEqual(len(flat), 3)

    def test_a_single_chart_window_offers_no_grid(self):
        alone = ChartWindow("один", "Один", (sample_chart,))
        self.assertFalse(alone.grid_button.isVisibleTo(alone))
        alone.close()


class ChartOrderTests(unittest.TestCase):
    """Chart order is up to the viewer."""

    def setUp(self):
        self.window = ChartWindow(
            "проба",
            "Проба",
            (sample_chart, sample_chart, sample_chart),
            link_x=True,
        )
        self.window.show()
        _app.processEvents()

    def tearDown(self):
        self.window.close()

    def places(self):
        return [self.window.splitter.widget(i) for i in range(self.window.splitter.count())]

    def test_a_chart_moves_up(self):
        self.window.move_view(2, -1)
        _app.processEvents()
        self.assertEqual(self.window._order, [0, 2, 1])
        self.assertIs(self.places()[1], self.window.views[2])

    def test_a_chart_moves_down(self):
        self.window.move_view(0, 1)
        _app.processEvents()
        self.assertEqual(self.window._order, [1, 0, 2])

    def test_the_first_one_does_not_leave_the_window(self):
        self.window.move_view(0, -1)
        self.assertEqual(self.window._order, [0, 1, 2])

    def test_the_last_one_does_not_either(self):
        self.window.move_view(2, 1)
        self.assertEqual(self.window._order, [0, 1, 2])

    def test_the_buttons_know_the_ends(self):
        first, last = self.window.views[0], self.window.views[2]
        self.assertFalse(first.up_button.isEnabled())
        self.assertTrue(first.down_button.isEnabled())
        self.assertTrue(last.up_button.isEnabled())
        self.assertFalse(last.down_button.isEnabled())

    def test_a_column_shows_no_sideways_arrows(self):
        """There are no horizontal neighbours there, so no move to promise."""
        view = self.window.views[0]
        self.assertFalse(view.left_button.isVisibleTo(view))
        self.assertFalse(view.right_button.isVisibleTo(view))

    def test_moving_steps_over_a_detached_chart(self):
        """Otherwise the click looks ignored: the visible order is the same."""
        self.window.detach(1)
        _app.processEvents()
        self.window.move_view(2, -1)
        _app.processEvents()
        self.assertEqual(self.window._visible_order(), [2, 0])

    def test_a_returned_chart_comes_back_to_its_place(self):
        self.window.move_view(2, -1)
        self.window.detach(2)
        _app.processEvents()
        self.window.attach(2)
        _app.processEvents()
        self.assertEqual(self.window._visible_order(), [0, 2, 1])

    def test_the_button_asks_the_window_to_move(self):
        self.window.views[2].up_button.click()
        _app.processEvents()
        self.assertEqual(self.window._order, [0, 2, 1])

    def test_the_order_and_the_layout_are_stored(self):
        settings = QSettings(
            str(Path(tempfile.mkdtemp()) / "settings.ini"), QSettings.IniFormat
        )
        self.window.move_view(2, -1)
        self.window.set_grid(True)
        self.window.save_layout(settings)

        again = ChartWindow(
            "проба", "Проба", (sample_chart, sample_chart, sample_chart), link_x=True
        )
        again.restore_layout(settings)
        self.assertEqual(again._order, [0, 2, 1])
        self.assertTrue(again._grid)
        again.close()

    def test_a_stale_order_is_ignored(self):
        """The chart count may have changed while the order is from before."""
        settings = QSettings(
            str(Path(tempfile.mkdtemp()) / "settings.ini"), QSettings.IniFormat
        )
        settings.setValue("chart_проба/order", [0, 1, 2, 3, 4])
        again = ChartWindow(
            "проба", "Проба", (sample_chart, sample_chart, sample_chart), link_x=True
        )
        again.restore_layout(settings)
        self.assertEqual(again._order, [0, 1, 2])
        again.close()


class GridSizesTests(unittest.TestCase):
    """A grid must stay a grid: all rows share the same columns."""

    def setUp(self):
        self.window = ChartWindow(
            "проба",
            "Проба",
            (sample_chart, sample_chart, sample_chart, sample_chart),
            link_x=True,
        )
        self.window.resize(1400, 800)
        self.window.show()
        _app.processEvents()
        self.window.set_grid(True)
        _app.processEvents()

    def tearDown(self):
        self.window.close()

    def rows(self):
        return [
            self.window.splitter.widget(place)
            for place in range(self.window.splitter.count())
        ]

    def test_dragging_a_column_moves_it_in_every_row(self):
        """Two rows with different boundaries are not a grid but two pairs."""
        first, second = self.rows()
        first.moveSplitter(900, 1)
        _app.processEvents()
        self.assertEqual(second.sizes(), first.sizes())

    def test_the_common_width_is_remembered(self):
        first, second = self.rows()
        first.moveSplitter(900, 1)
        _app.processEvents()
        self.assertEqual(self.window._columns, first.sizes())

    def test_a_double_click_on_the_vertical_handle_lines_the_grid_up(self):
        """The rows must line up with each other, not all to the middle."""
        first, second = self.rows()
        second.setSizes([900, 300])
        _app.processEvents()
        self.window.splitter.even_out()
        _app.processEvents()
        self.assertEqual(first.sizes(), second.sizes())

    def test_a_double_click_on_a_row_handle_evens_every_row(self):
        first, second = self.rows()
        first.setSizes([900, 300])
        _app.processEvents()
        first.even_out()
        _app.processEvents()
        self.assertEqual(first.sizes(), second.sizes())
        self.assertLessEqual(abs(first.sizes()[0] - first.sizes()[1]), 2)

    def test_reordering_keeps_the_grid_lined_up(self):
        """The widths travelled with the widgets, and the grid fell apart."""
        first, second = self.rows()
        first.moveSplitter(900, 1)
        _app.processEvents()
        widths = first.sizes()
        self.window.move_view(3, -2)
        _app.processEvents()
        for row in self.rows():
            with self.subTest(row=row):
                self.assertEqual(row.sizes(), widths)

    def test_reordering_keeps_the_row_heights(self):
        self.window.splitter.moveSplitter(560, 1)
        _app.processEvents()
        heights = self.window.splitter.sizes()
        self.window.move_view(0, 1)
        _app.processEvents()
        self.assertEqual(self.window.splitter.sizes(), heights)

    def test_a_chart_cannot_be_squeezed_out_of_existence(self):
        """A collapsed chart does not fold into its title, it disappears."""
        first = self.rows()[0]
        first.moveSplitter(5000, 1)
        _app.processEvents()
        self.assertTrue(all(size > 0 for size in first.sizes()))

    def test_the_grid_sizes_are_stored(self):
        settings = QSettings(
            str(Path(tempfile.mkdtemp()) / "settings.ini"), QSettings.IniFormat
        )
        self.rows()[0].moveSplitter(900, 1)
        _app.processEvents()
        self.window.save_layout(settings)
        self.assertTrue(settings.value("chart_проба/columns", [], type=list))
        self.assertTrue(settings.value("chart_проба/rows", [], type=list))


class GridMoveTests(unittest.TestCase):
    """In a grid "up" is a whole row back, and "left" is one place back."""

    def setUp(self):
        self.window = ChartWindow(
            "проба",
            "Проба",
            (sample_chart, sample_chart, sample_chart, sample_chart),
            link_x=True,
        )
        self.window.show()
        _app.processEvents()
        self.window.set_grid(True)
        _app.processEvents()

    def tearDown(self):
        self.window.close()

    def test_moving_up_swaps_with_the_chart_above(self):
        self.window.views[2].up_button.click()
        _app.processEvents()
        self.assertEqual(self.window._order, [2, 1, 0, 3])

    def test_moving_left_swaps_with_the_neighbour(self):
        self.window.views[1].left_button.click()
        _app.processEvents()
        self.assertEqual(self.window._order, [1, 0, 2, 3])

    def test_the_left_arrow_is_off_in_the_first_column(self):
        """Otherwise it swapped the chart with the end of the previous row."""
        self.assertFalse(self.window.views[0].left_button.isEnabled())
        self.assertTrue(self.window.views[1].left_button.isEnabled())

    def test_the_top_row_cannot_go_up(self):
        self.assertFalse(self.window.views[0].up_button.isEnabled())
        self.assertFalse(self.window.views[1].up_button.isEnabled())
        self.assertTrue(self.window.views[2].up_button.isEnabled())

    def test_the_bottom_row_cannot_go_down(self):
        self.assertTrue(self.window.views[0].down_button.isEnabled())
        self.assertFalse(self.window.views[2].down_button.isEnabled())

    def test_the_sideways_arrows_show_up_only_in_the_grid(self):
        view = self.window.views[0]
        self.assertTrue(view.right_button.isVisibleTo(view))
        self.window.set_grid(False)
        _app.processEvents()
        self.assertFalse(view.right_button.isVisibleTo(view))

    def test_an_odd_last_chart_has_no_right_neighbour(self):
        self.window.detach(3)
        _app.processEvents()
        alone = self.window.views[2]
        self.assertFalse(alone.right_button.isEnabled())
        self.assertTrue(alone.up_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
