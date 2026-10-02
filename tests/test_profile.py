"""The volume profile: filesystem plus cluster size as the calibration key."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from containerhelper.factory import FactoryPoint, factory_data
from containerhelper.model import (
    DEFAULT_CLUSTER_BYTES,
    MIB,
    MIN_SAFETY_BYTES,
    VC_HEADERS_BYTES,
    CopySlackModel,
    MetadataModel,
    Payload,
    solve_container_mib,
)
from containerhelper.paths import CALIBRATION_NAME
from containerhelper.records import (
    SCHEMA_VERSION,
    DEFAULT_PROFILE,
    EXFAT,
    FAT,
    NO_FILESYSTEM,
    SCOPE_METADATA,
    SCOPE_SLACK,
    Record,
    Store,
    Uncalibrated,
    VolumeProfile,
    build_models,
    factory_points,
    factory_samples,
    filesystem_name,
    metadata_points,
    missing_calibration,
    profile_of,
    slack_cross_check,
    slack_samples,
    solve_for_profile,
)
from tests.reference import HEADERS_AND_TAIL
from tests.test_forecast import copied

EXFAT_32K = VolumeProfile(EXFAT, 32 * 1024)
VOLUME = 1024 * MIB - HEADERS_AND_TAIL


def empty_point(**changes) -> Record:
    base = Record(
        id="Калибровка 1 GiB",
        container_mib=1024,
        mounted_bytes=VOLUME,
        empty_free_bytes=VOLUME - 18 * MIB,
    )
    return replace(base, **changes)


def exfat(record: Record) -> Record:
    return replace(record, filesystem="exFAT", cluster_bytes=32 * 1024)


class ProfileOfTests(unittest.TestCase):
    def test_an_unread_filesystem_is_ntfs(self):
        """Every record from before the filesystem was read was taken on NTFS."""
        self.assertEqual(profile_of(empty_point()), DEFAULT_PROFILE)

    def test_names_are_taken_as_veracrypt_gives_them(self):
        cases = {
            "NTFS": "NTFS",
            "ntfs": "NTFS",
            "exFAT": EXFAT,
            "EXFAT": EXFAT,
            "FAT32": FAT,
            "FAT": FAT,
            "ReFS": "ReFS",
        }
        for raw, name in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(filesystem_name(raw), name)

    def test_an_unread_cluster_is_4_kib_on_ntfs_only(self):
        """As in validate. exFAT's default depends on the size: no guessing."""
        self.assertEqual(
            profile_of(empty_point(cluster_bytes=0)).cluster_bytes,
            DEFAULT_CLUSTER_BYTES,
        )
        self.assertEqual(
            profile_of(empty_point(cluster_bytes=0, filesystem="exFAT")),
            VolumeProfile(EXFAT, 0),
        )

    def test_the_cluster_is_part_of_the_profile(self):
        self.assertNotEqual(
            profile_of(empty_point(cluster_bytes=8192)), DEFAULT_PROFILE
        )

    def test_factory_data_is_ntfs_4_kib(self):
        data = factory_data()
        for item in (*data.points, *data.samples):
            with self.subTest(container_mib=item.container_mib):
                self.assertEqual(profile_of(item), DEFAULT_PROFILE)
        self.assertEqual(len(factory_points()), len(data.points))
        self.assertEqual(len(factory_samples()), len(data.samples))
        self.assertEqual(factory_points(EXFAT_32K), [])
        self.assertEqual(factory_samples(EXFAT_32K), [])

    def test_a_factory_point_without_the_field_is_ntfs(self):
        point = FactoryPoint(
            container_mib=1024,
            cluster_bytes=4096,
            mounted_bytes=VOLUME,
            empty_free_bytes=VOLUME - 18 * MIB,
        )
        self.assertEqual(profile_of(point), DEFAULT_PROFILE)


class FilterTests(unittest.TestCase):
    """A model is built from one profile's measurements only."""

    def test_an_exfat_slack_measurement_leaves_the_ntfs_model(self):
        """exFAT spends a fraction of NTFS's bytes per file: an underestimate."""
        ntfs = copied()
        foreign = exfat(copied(id="exFAT", file_alloc_bytes=900 * MIB))
        self.assertEqual(len(slack_samples([ntfs, foreign])), 1)
        self.assertEqual(slack_samples([ntfs, foreign], EXFAT_32K), [
            (foreign.file_count, foreign.copy_slack_measured)
        ])
        self.assertEqual(
            [check.record for check in slack_cross_check([ntfs, foreign])], [ntfs]
        )

    def test_an_8_kib_point_leaves_the_4_kib_curve(self):
        record = empty_point(cluster_bytes=8192)
        self.assertEqual(metadata_points([record]), [])
        self.assertEqual(
            profile_of(record), VolumeProfile("NTFS", 8192)
        )
        self.assertEqual(metadata_points([empty_point()]), [(
            empty_point().volume_bytes, empty_point().metadata_bytes
        )])

    def test_the_models_are_those_of_the_profile(self):
        ntfs, slack = build_models([exfat(copied())])
        self.assertFalse(ntfs.calibrated)
        self.assertFalse(slack.calibrated)


class StoreProfileTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_a_point_of_another_profile_lands_beside(self):
        """An exFAT point on a volume measured on NTFS is not a retake."""
        store = Store(path=self.path, calibration=[empty_point()])
        store.put_calibration(exfat(empty_point(id="exFAT")))
        self.assertEqual(len(store.calibration_points()), 2)
        store.put_calibration(empty_point(id="Пересняли"))
        self.assertEqual(
            {item.id for item in store.calibration_points()},
            {"Пересняли", "exFAT"},
        )

    def test_a_sample_of_another_profile_lands_beside(self):
        sample = copied(id="Запас", fileset="tiny")
        store = Store(path=self.path, calibration=[sample])
        store.put_calibration(exfat(replace(sample, id="exFAT")))
        self.assertEqual(len(store.slack_measurements()), 2)

    def test_another_profile_does_not_supersede_the_factory(self):
        """An own exFAT sample at a factory n leaves the factory NTFS one in."""
        factory = factory_data().samples[0]
        own = exfat(copied(file_count=factory.file_count))
        empty = Store(path=self.path)
        store = Store(path=self.path, calibration=[own])
        self.assertEqual(
            len(store.calibration_records()), len(empty.calibration_records())
        )
        self.assertEqual(store.factory_volumes(), empty.factory_volumes())
        self.assertEqual(store.models()[1].per_file, empty.models()[1].per_file)

    def test_a_point_of_another_profile_leaves_the_factory_point_in(self):
        """By volume alone it would cover the factory size: a node lost."""
        factory = factory_data().points[0]
        own = exfat(
            empty_point(
                container_mib=factory.container_mib,
                mounted_bytes=factory.mounted_bytes,
                empty_free_bytes=factory.empty_free_bytes,
            )
        )
        for store in (
            Store(path=self.path, calibration=[own]),
            Store(path=self.path, records=[own]),
        ):
            with self.subTest(calibration=bool(store.calibration)):
                self.assertIn(factory.volume_bytes, store.factory_volumes())
                self.assertIn(
                    factory.volume_bytes, dict(store.models()[0].points)
                )

    def test_another_profile_has_no_factory_data(self):
        store = Store(path=self.path, calibration=[exfat(empty_point())])
        self.assertEqual(len(store.calibration_records(EXFAT_32K)), 1)
        self.assertEqual(store.factory_volumes(EXFAT_32K), set())

    def test_the_model_records_are_those_of_the_profile(self):
        store = Store(path=self.path, records=[copied(), exfat(copied(id="exFAT"))])
        ids = {record.id for record in store.all_for_model()}
        self.assertIn("Проба", ids)
        self.assertNotIn("exFAT", ids)


class SchemaSixTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"

    def tearDown(self):
        self._dir.cleanup()

    def test_the_filesystem_is_written_out(self):
        """The file says which profile a record calibrates, unread ones too."""
        Store(path=self.path, records=[copied()]).save()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schema"], SCHEMA_VERSION)
        self.assertEqual(raw["records"][0]["filesystem"], "NTFS")

    def test_a_schema_5_record_without_it_reads_as_ntfs(self):
        stored = copied().to_json()
        del stored["filesystem"]
        self.path.write_text(
            json.dumps({"schema": 5, "records": [stored]}), encoding="utf-8"
        )
        record = Store.load(self.path).records[0]
        self.assertEqual(record.filesystem, "NTFS")
        self.assertEqual(profile_of(record), DEFAULT_PROFILE)

    def test_a_schema_5_measurement_without_it_reads_as_ntfs(self):
        stored = empty_point(cluster_bytes=0).to_json()
        del stored["filesystem"]
        (self.path.parent / CALIBRATION_NAME).write_text(
            json.dumps({"schema": 5, "calibration": [stored]}), encoding="utf-8"
        )
        store = Store.load(self.path)
        self.assertEqual(profile_of(store.calibration[0]), DEFAULT_PROFILE)
        self.assertEqual(len(metadata_points(store.all_for_model())), len(
            metadata_points(Store(path=self.path).all_for_model())
        ))

    def test_the_move_from_the_old_store_keeps_another_profile(self):
        """A key by volume alone dropped the exFAT point on the same volume."""
        own = empty_point(id="NTFS")
        legacy = exfat(empty_point(id="exFAT"))
        (self.path.parent / CALIBRATION_NAME).write_text(
            json.dumps({"schema": 6, "calibration": [own.to_json()]}),
            encoding="utf-8",
        )
        self.path.write_text(
            json.dumps({"schema": 4, "records": [], "calibration": [legacy.to_json()]}),
            encoding="utf-8",
        )
        store = Store.load(self.path)
        self.assertEqual({item.id for item in store.calibration}, {"NTFS", "exFAT"})


class CalculationTests(unittest.TestCase):
    """A profile calculates on its own measurements, or not at all."""

    PAYLOAD = Payload(700 * MIB, 700 * MIB, 100, cluster_bytes=32 * 1024)
    POINTS = (
        exfat(empty_point()),
        exfat(
            empty_point(
                id="Калибровка 2 GiB",
                container_mib=2048,
                mounted_bytes=2048 * MIB - HEADERS_AND_TAIL,
                empty_free_bytes=2048 * MIB - HEADERS_AND_TAIL - 20 * MIB,
            )
        ),
    )
    SAMPLES = (
        exfat(copied(id="10 файлов", fileset="ten")),
        exfat(
            copied(
                id="100 файлов", fileset="hundred", file_count=100,
                left_bytes=99 * MIB,
            )
        ),
    )

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "records.json"

    def tearDown(self):
        self._dir.cleanup()

    def solve(self, profile: VolumeProfile, *calibration: Record):
        store = Store(path=self.path, calibration=list(calibration))
        ntfs, slack = store.models(profile)
        return solve_for_profile(self.PAYLOAD, profile, ntfs, slack, 4 * MIB)

    def refusal(self, *calibration: Record) -> Uncalibrated:
        with self.assertRaises(Uncalibrated) as caught:
            self.solve(EXFAT_32K, *calibration)
        self.assertEqual(caught.exception.profile, EXFAT_32K)
        return caught.exception

    def test_a_profile_with_nothing_measured_is_refused(self):
        """The NTFS defaults are not a cautious guess about exFAT."""
        self.assertEqual(self.refusal().missing, (SCOPE_METADATA, SCOPE_SLACK))

    def test_the_refusal_names_what_is_missing(self):
        self.assertEqual(self.refusal(*self.POINTS).missing, (SCOPE_SLACK,))

    def test_one_file_count_does_not_calibrate_the_copy_slack(self):
        """With one n the per-file slack would be the NTFS default again."""
        self.assertEqual(
            self.refusal(*self.POINTS, self.SAMPLES[0]).missing, (SCOPE_SLACK,)
        )

    def test_a_calibrated_profile_calculates_on_its_own_measurements(self):
        exfat_solution = self.solve(EXFAT_32K, *self.POINTS, *self.SAMPLES)
        exfat_ntfs, _ = build_models([*self.POINTS, *self.SAMPLES], EXFAT_32K)
        self.assertEqual(
            exfat_solution.metadata_bytes,
            exfat_ntfs.overhead(exfat_solution.volume_bytes),
        )
        ntfs, slack = Store(
            path=self.path, calibration=[*self.POINTS, *self.SAMPLES]
        ).models()
        ntfs_solution = solve_for_profile(
            replace(self.PAYLOAD, cluster_bytes=DEFAULT_CLUSTER_BYTES),
            DEFAULT_PROFILE, ntfs, slack, 4 * MIB,
        )
        self.assertNotEqual(
            exfat_solution.metadata_bytes, ntfs_solution.metadata_bytes
        )

    def test_a_flat_slope_does_not_calibrate_the_copy_slack(self):
        """Two n, the same slack: the per-file slack stays the NTFS default."""
        flat = (
            self.SAMPLES[0],
            replace(self.SAMPLES[1], left_bytes=self.SAMPLES[0].left_bytes),
        )
        self.assertTrue(build_models(flat, EXFAT_32K)[1].per_file_calibrated)
        self.assertEqual(
            self.refusal(*self.POINTS, *flat).missing, (SCOPE_SLACK,)
        )

    def test_the_payload_is_rounded_to_the_profile_cluster(self):
        """4 KiB clusters solved on 32 KiB would lose 28 KiB per file."""
        store = Store(path=self.path, calibration=[*self.POINTS, *self.SAMPLES])
        ntfs, slack = store.models(EXFAT_32K)
        with self.assertRaises(ValueError):
            solve_for_profile(
                replace(self.PAYLOAD, cluster_bytes=4096), EXFAT_32K,
                ntfs, slack, 4 * MIB,
            )

    def test_the_default_profile_stands_on_the_factory_data(self):
        """And its answer is the solver's own: the profile only picks models."""
        payload = replace(self.PAYLOAD, cluster_bytes=DEFAULT_CLUSTER_BYTES)
        ntfs, slack = Store(path=self.path).models()
        self.assertEqual(missing_calibration(ntfs, slack), ())
        self.assertEqual(
            solve_for_profile(payload, DEFAULT_PROFILE, ntfs, slack, 4 * MIB),
            solve_container_mib(payload, ntfs=ntfs, slack=slack,
                                safety_bytes=4 * MIB),
        )


class NoFilesystemTests(unittest.TestCase):
    """VeraCrypt's "None": the data plus the headers plus the safety floor."""

    def solve(self, payload: Payload, safety_bytes: int = MIN_SAFETY_BYTES):
        return solve_for_profile(
            payload, NO_FILESYSTEM, MetadataModel(), CopySlackModel(), safety_bytes
        )

    def test_the_container_is_the_data_plus_the_headers(self):
        """1000 MiB + 256 KiB + 1 MiB is 1001.25 MiB, rounded up."""
        solution = self.solve(Payload(1000 * MIB, 1000 * MIB, 1))
        self.assertEqual(solution.container_mib, 1002)
        self.assertEqual(solution.metadata_bytes, 0)
        self.assertEqual(solution.copy_slack, 0)
        self.assertEqual(solution.vc_header, VC_HEADERS_BYTES)

    def test_the_data_counts_by_its_logical_size(self):
        """No filesystem, no clusters: ten thousand bytes are not 40 MiB."""
        payload = Payload.for_files([1] * 10_000)
        self.assertGreater(payload.alloc_bytes, 39 * MIB)
        self.assertEqual(self.solve(payload).container_mib, 2)

    def test_the_margin_does_not_go_below_the_floor(self):
        self.assertEqual(
            self.solve(Payload(MIB, MIB, 1), 0).safety_bytes, MIN_SAFETY_BYTES
        )

    def test_any_cluster_is_still_no_filesystem(self):
        payload = Payload(1000 * MIB, 1000 * MIB, 1)
        solution = solve_for_profile(
            payload, VolumeProfile("None", 4096), MetadataModel(),
            CopySlackModel(), MIN_SAFETY_BYTES,
        )
        self.assertEqual(solution.container_mib, 1002)

    def test_it_needs_no_calibration(self):
        self.assertEqual(
            missing_calibration(MetadataModel(), CopySlackModel()),
            (SCOPE_METADATA, SCOPE_SLACK),
        )
        self.assertEqual(self.solve(Payload(1, 1, 1)).container_mib, 2)

    def test_the_breakdown_adds_up_to_the_container(self):
        solution = self.solve(Payload(123_456_789, 123_456_789, 1), 3 * MIB)
        self.assertEqual(solution.safety_bytes, 3 * MIB)
        self.assertEqual(
            solution.vc_header + solution.payload_alloc
            + solution.predicted_left_bytes,
            solution.container_bytes,
        )
        self.assertGreaterEqual(solution.predicted_left_bytes, 3 * MIB)


if __name__ == "__main__":
    unittest.main()
