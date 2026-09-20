"""Calibration points: flag, filter, filesystem, simplified dialog."""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])

_settings_dir = tempfile.mkdtemp(prefix="containerhelper-settings-")
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, _settings_dir)

from dataclasses import replace  # noqa: E402

from containerhelper.factory import factory_data  # noqa: E402
from containerhelper.model import (  # noqa: E402
    FACTORY_MARGIN_BYTES,
    MIB,
    VC_HEADER_BYTES,
)
from containerhelper.paths import CALIBRATION_NAME  # noqa: E402
from containerhelper.records import (  # noqa: E402
    SCHEMA_VERSION,
    SCOPE_NTFS,
    SCOPE_SLACK,
    Record,
    Store,
    StoreError,
    is_usable,
    ntfs_points,
    validate,
)
from containerhelper.sizes import SUPPORTED_FS  # noqa: E402
from containerhelper.ui.record_dialog import (  # noqa: E402
    MAX_BYTES_CHARS,
    MAX_CLUSTER_CHARS,
    RecordDialog,
)


def point(mib, ntfs=17_879_040, **extra):
    volume = mib * MIB - VC_HEADER_BYTES
    return Record(
        id=f"Точка {mib}",
        container_mib=mib,
        mounted_bytes=volume,
        empty_free_bytes=volume - ntfs,
        **extra,
    )


class CalibrationFlagTests(unittest.TestCase):
    def test_empty_volume_measurement_is_a_point(self):
        self.assertTrue(point(1024).is_calibration_point)

    def test_a_record_with_data_is_not(self):
        self.assertFalse(point(1024, file_bytes=10_000).is_calibration_point)

    def test_a_record_with_leftover_is_not(self):
        """Cache 1 has left space but no data size — still not a point."""
        self.assertFalse(point(1024, left_bytes=5_464_064).is_calibration_point)


class FilesystemTests(unittest.TestCase):
    def test_ntfs_passes(self):
        record = point(1024, filesystem=SUPPORTED_FS)
        self.assertEqual([issue.code for issue in validate(record)], [])

    def test_other_filesystem_is_flagged(self):
        record = point(1024, filesystem="exFAT")
        self.assertIn("filesystem", {issue.code for issue in validate(record)})

    def test_a_non_ntfs_record_leaves_the_ntfs_calibration(self):
        """exFAT overhead is another story: the point would spoil the model."""
        record = point(1024, filesystem="exFAT")
        self.assertFalse(is_usable(record, SCOPE_NTFS))
        self.assertEqual(ntfs_points([record]), [])

    def test_it_does_not_touch_the_copy_slack(self):
        """Cluster arithmetic does not depend on the filesystem."""
        record = point(1024, filesystem="exFAT")
        self.assertTrue(is_usable(record, SCOPE_SLACK))

    def test_empty_filesystem_means_not_read(self):
        """Every record made before this check existed is silently NTFS."""
        self.assertEqual([issue.code for issue in validate(point(1024))], [])

    def test_it_survives_a_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.json"
            Store(path=path, records=[point(1024, filesystem="exFAT")]).save()
            store = Store.load(path)
            self.assertEqual(store.records[0].filesystem, "exFAT")

    def test_saving_writes_the_current_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "records.json"
            Store(path=path, records=[point(1024)]).save()
            import json

            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["schema"], SCHEMA_VERSION)


class SeparateStoreTests(unittest.TestCase):
    """Empty-volume measurements live in their own file, not among records."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"
        self.points_path = Path(self._dir.name) / CALIBRATION_NAME
        self.cache = Record(
            id="Cache 1",
            container_mib=11062,
            mounted_bytes=11_599_081_472,
            empty_free_bytes=11_558_866_944,
            left_bytes=5_464_064,
        )

    def tearDown(self):
        self._dir.cleanup()

    def seeded(self):
        Store(
            path=self.path,
            records=[self.cache],
            calibration=[point(1024), point(2048)],
        ).save()
        return Store.load(self.path)

    def written(self, path):
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def test_points_go_to_their_own_file_beside_the_records(self):
        store = self.seeded()
        self.assertEqual(store.calibration_path, self.points_path)
        self.assertTrue(self.points_path.exists())
        self.assertEqual(len(self.written(self.points_path)["calibration"]), 2)

    def test_the_records_file_holds_no_measurements_at_all(self):
        """No calibration key in the records file any more — not even empty."""
        self.seeded()
        raw = self.written(self.path)
        self.assertEqual(sorted(raw), ["records", "schema"])
        self.assertEqual([item["id"] for item in raw["records"]], ["Cache 1"])

    def test_records_hold_only_real_measurements(self):
        store = self.seeded()
        self.assertEqual([r.id for r in store.records], ["Cache 1"])
        self.assertEqual(len(store.calibration), 2)

    def test_both_files_survive_a_round_trip(self):
        store = self.seeded()
        store.save()
        again = Store.load(self.path)
        self.assertEqual(len(again.records), 1)
        self.assertEqual(len(again.calibration), 2)

    def test_an_empty_calibration_leaves_no_file_behind(self):
        """A new machine has no own measurements: no empty file is needed."""
        Store(path=self.path, records=[self.cache]).save()
        self.assertFalse(self.points_path.exists())

    def test_deleting_every_point_rewrites_the_file(self):
        """Otherwise deleted measurements would come back on the next load."""
        store = self.seeded()
        store.calibration = []
        store.save()
        self.assertEqual(Store.load(self.path).calibration, [])

    def test_old_files_migrate_their_points(self):
        """Before schema 4, empty-volume measurements lay among the records."""
        import json

        self.path.write_text(
            json.dumps(
                {
                    "schema": 3,
                    "records": [self.cache.to_json(), point(1024).to_json()],
                }
            ),
            encoding="utf-8",
        )
        store = Store.load(self.path)
        self.assertEqual([r.id for r in store.records], ["Cache 1"])
        self.assertEqual([r.id for r in store.calibration], ["Точка 1024"])
        self.assertTrue(store.migrated)

    def test_the_fourth_schema_moves_its_key_into_the_new_file(self):
        import json

        self.path.write_text(
            json.dumps(
                {
                    "schema": 4,
                    "records": [self.cache.to_json()],
                    "calibration": [point(1024).to_json()],
                }
            ),
            encoding="utf-8",
        )
        store = Store.load(self.path)
        self.assertTrue(store.migrated)
        store.save()

        self.assertNotIn("calibration", self.written(self.path))
        self.assertEqual(len(self.written(self.points_path)["calibration"]), 1)
        self.assertFalse(Store.load(self.path).migrated)

    def test_the_new_file_wins_over_a_leftover_copy(self):
        """An old copy in the records file does not supersede a fresh one."""
        import json

        fresh = point(1024, ntfs=20 * MIB)
        Store(path=self.path, calibration=[fresh]).save()
        self.path.write_text(
            json.dumps(
                {"schema": 4, "records": [], "calibration": [point(1024).to_json()]}
            ),
            encoding="utf-8",
        )
        store = Store.load(self.path)
        self.assertEqual(len(store.calibration), 1)
        self.assertEqual(store.calibration[0].ntfs_bytes, 20 * MIB)

    def test_nothing_to_migrate_leaves_the_flag_down(self):
        self.assertFalse(self.seeded().migrated)

    def test_the_model_sees_both_files(self):
        store = self.seeded()
        volumes = {r.mounted_bytes for r in store.all_for_model()}
        self.assertIn(self.cache.mounted_bytes, volumes)
        self.assertIn(point(1024).mounted_bytes, volumes)

    def test_a_second_records_file_shares_one_calibration(self):
        """Calibration belongs to the machine, not to a set of records."""
        self.seeded()
        other = Store.load(Path(self._dir.name) / "other.json")
        self.assertEqual(other.records, [])
        self.assertEqual(len(other.calibration), 2)

    def test_a_broken_calibration_file_is_reported(self):
        self.points_path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(StoreError) as caught:
            Store.load(self.path)
        self.assertIn(CALIBRATION_NAME, str(caught.exception))


class FactoryOverrideTests(unittest.TestCase):
    """An own measurement supersedes factory; a disabled one lets it back."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"
        self.factory = factory_data().points[0]
        self.mine = Record(
            id="Своя 1 GiB",
            container_mib=self.factory.container_mib,
            mounted_bytes=self.factory.mounted_bytes,
            # deliberately below the factory value: an own measurement is
            # truer than any foreign one
            empty_free_bytes=self.factory.empty_free_bytes + 5 * MIB,
        )

    def tearDown(self):
        self._dir.cleanup()

    def overhead_at(self, store):
        return dict(store.models()[0].points)[self.factory.mounted_bytes]

    def factory_count(self):
        """All factory records: the points plus the copy-slack measurements.

        A copy-slack measurement also gives an NTFS point — its empty volume
        was measured before the files were written — so it enters the model on
        a par with the points.
        """
        data = factory_data()
        return len(data.points) + len(data.samples)

    def test_factory_fills_an_empty_store(self):
        store = Store(path=self.path)
        self.assertEqual(len(store.calibration_records()), self.factory_count())
        self.assertEqual(self.overhead_at(store), self.factory.ntfs_bytes)

    def test_own_point_wins_even_when_smaller(self):
        """_dedupe takes the larger; for factory values that rule is wrong."""
        store = Store(path=self.path, calibration=[self.mine])
        self.assertLess(self.overhead_at(store), self.factory.ntfs_bytes)
        self.assertEqual(self.overhead_at(store), self.mine.ntfs_bytes)

    def test_disabling_brings_the_factory_value_back(self):
        store = Store(path=self.path, calibration=[replace(self.mine, disabled=True)])
        self.assertEqual(self.overhead_at(store), self.factory.ntfs_bytes)

    def test_disabled_point_stays_in_the_file(self):
        """Resetting to factory values loses nothing."""
        Store(
            path=self.path, calibration=[replace(self.mine, disabled=True)]
        ).save()
        store = Store.load(self.path)
        self.assertEqual(len(store.calibration), 1)
        self.assertTrue(store.calibration[0].disabled)

    def test_factory_volumes_shrink_as_own_points_appear(self):
        empty = Store(path=self.path)
        covered = Store(path=self.path, calibration=[self.mine])
        self.assertEqual(len(empty.factory_volumes()), self.factory_count())
        self.assertEqual(len(covered.factory_volumes()), self.factory_count() - 1)

    def test_factory_only_segments_carry_a_margin(self):
        """A different Windows build may have chosen a different $LogFile."""
        empty = Store(path=self.path)
        volume = int(18 * 1024**3)
        with_factory = empty.safety().advise(volume, 1)
        self.assertIn("заводских", with_factory.ntfs_reason)
        self.assertGreaterEqual(with_factory.ntfs_bytes, FACTORY_MARGIN_BYTES)


class SimplifiedDialogTests(unittest.TestCase):
    def dialog(self, mib=12288):
        return RecordDialog(
            Record(id=f"Калибровка {mib}", container_mib=mib), calibration=True
        )

    def test_only_the_empty_volume_measurement_is_offered(self):
        dialog = self.dialog()
        self.assertTrue(dialog.measure_button.isVisibleTo(dialog))
        self.assertFalse(dialog.payload_button.isVisibleTo(dialog))
        self.assertFalse(dialog.left_button.isVisibleTo(dialog))

    def test_payload_fields_are_out_of_the_way(self):
        dialog = self.dialog()
        for edit in (dialog.file_edit, dialog.count_edit, dialog.alloc_edit,
                     dialog.left_edit):
            with self.subTest(edit.objectName()):
                self.assertFalse(edit.isVisibleTo(dialog))

    def test_name_and_size_are_prefilled(self):
        dialog = self.dialog(20480)
        self.assertEqual(dialog.container_edit.text(), "20480")
        self.assertEqual(dialog.build_record().container_mib, 20480)

    def test_the_result_is_a_calibration_point(self):
        dialog = self.dialog()
        dialog.mounted_edit.setText("12884635648")
        dialog.free_edit.setText("12844421120")
        self.assertTrue(dialog.build_record().is_calibration_point)

    def test_the_full_dialog_still_shows_everything(self):
        dialog = RecordDialog()
        self.assertTrue(dialog.left_button.isVisibleTo(dialog))
        self.assertTrue(dialog.file_edit.isVisibleTo(dialog))


class InputLimitTests(unittest.TestCase):
    def test_byte_fields_are_capped(self):
        dialog = RecordDialog()
        for edit in (dialog.mounted_edit, dialog.free_edit, dialog.file_edit,
                     dialog.alloc_edit, dialog.left_edit):
            with self.subTest(edit.objectName()):
                self.assertEqual(edit.maxLength(), MAX_BYTES_CHARS)

    def test_cluster_is_capped_to_five_digits_plus_separators(self):
        dialog = RecordDialog()
        self.assertEqual(dialog.cluster_combo.lineEdit().maxLength(), MAX_CLUSTER_CHARS)

    def test_a_cap_does_not_cut_a_real_value(self):
        """The caps must let through everything that makes physical sense."""
        dialog = RecordDialog()
        biggest = "1 125 899 906 842 624"  # 1 PiB with separators
        dialog.mounted_edit.setText(biggest)
        self.assertEqual(dialog.mounted_edit.text(), biggest)
        dialog.cluster_combo.setCurrentText("65536")
        self.assertEqual(dialog.cluster_combo.currentText(), "65536")


if __name__ == "__main__":
    unittest.main()
