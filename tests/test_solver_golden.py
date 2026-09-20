"""Frozen output of the solver on a grid of payload size and file count.

Stage A of the volume-profile work touches the core: the header constant
splits into the VeraCrypt headers and the filesystem tail, the metadata model
takes a profile, and the X axis of the calibration points changes from the
measured volume size to the computed one. On NTFS with a 4 KiB cluster none of
that may move the answer by a byte, and this file is what says so: the tables
below were taken before the first of those changes.

Two model sets, because they fail differently. FACTORY is the path the
Calculation tab walks on the first recalculation after startup — the models an
empty store builds from the factory data, the safety margin advised for that
very size, then the solve with it. The first one, not any one: the tab seeds
its probe with the spin box's current value, so a later recalculation can start
from a different margin. DEFAULT is the uncalibrated affine model
`19 MiB + 0.17 % of the volume`, where a changed definition of the volume size
shows up directly.

The metadata is frozen beside the container size on purpose: a drift of a few
bytes in the model hides inside the rounding to whole mebibytes. A drift in
VC_HEADER_BYTES itself is not caught here and does not need to be — the
reference records pin it to the byte
(`test_model.DerivedValueTests.test_veracrypt_header_is_constant_across_records`).
The copy slack moves with neither, and is frozen for its own sake: it pins the
factory slack calibration, not the solver.

What Stage A may do to these tables was measured beforehand on a simulation of
the planned change — the header keeps the 262 144 B of VeraCrypt headers, the
4096 B tail moves into the metadata, so both axes of every calibration point
grow by the tail. The real change may well land differently; these are what to
compare it against, not a promise:

* FACTORY — the metadata column grows by exactly 4096 B, on every row. That is
  the tail changing which column it is counted in, not the model moving. The
  container size, the safety margin and the copy slack hold everywhere except
  one row: at 1587 MiB in ten thousand files the advised margin is 4 194 820 B
  today, 516 B above a mebibyte boundary, and the shifted query point brings it
  back to exactly 4 MiB — the margin drops from 5 MiB to 4 and the container
  with it. The megabyte is lost to the rounding of the advice, not to the
  metadata.
* DEFAULT — the metadata grows by 7 B, `ceil(0.0017 × 4096)`, because the same
  affine model is now asked about a volume 4096 B larger; on one row the
  rounding gives 6 B. This is the divergence the plan requires to be either
  compensated or recorded here with its reason. And on a terabyte of payload in
  ten thousand files the 4096 B taken out of the header crosses a mebibyte
  boundary, and the container again comes out 1 MiB smaller.

Both of those last drops are underestimates against today's answer, and both
are covered by the safety margin rather than by the model — which is what makes
them worth a second look at Stage A rather than a shrug.

Anything beyond that is a regression, not the migration.

The tables are generated, not written by hand. Regenerate them only
deliberately, with the reason in the commit message:

    .venv/Scripts/python.exe -m tests.test_solver_golden --print
"""

import unittest
from pathlib import Path

from containerhelper.model import (
    DEFAULT_CLUSTER_BYTES,
    DEFAULT_SAFETY_BYTES,
    MIB,
    CopySlackModel,
    NtfsModel,
    Payload,
    round_up,
    solve_container_mib,
)
from containerhelper.records import Store

#: Payload sizes in MiB: from a single cluster-sized file to a terabyte, with
#: the recommended sizes table's range in between. 1513 and 1587 are there for
#: their containers: they land the volume near 1536 and 1610 MiB, the two
#: measurements that refuted the convexity of the curve, where the staircase of
#: $LogFile makes the model weakest.
SIZES_MIB = (1, 64, 512, 1024, 1513, 1587, 4096, 8192, 16384, 65536, 262144, 1048576)

#: File counts. One file is the manual records' case, ten thousand is where
#: the per-file slack outgrows everything else.
COUNTS = (1, 100, 10_000)

#: size MiB, file count → container MiB, safety MiB, metadata B, copy slack B.
FACTORY = (
    (1, 1, 23, 7, 14_816_366, 10_590),
    (1, 100, 23, 7, 14_816_366, 145_824),
    (1, 10_000, 74, 7, 14_907_278, 13_669_224),
    (64, 1, 86, 7, 14_928_669, 10_590),
    (64, 100, 86, 7, 14_928_669, 145_824),
    (64, 10_000, 111, 5, 14_934_016, 13_669_224),
    (512, 1, 534, 5, 17_223_095, 10_590),
    (512, 100, 534, 5, 17_223_095, 145_824),
    (512, 10_000, 582, 5, 17_293_312, 13_669_224),
    (1_024, 1, 1_047, 5, 17_930_928, 10_590),
    (1_024, 100, 1_047, 5, 17_930_928, 145_824),
    (1_024, 10_000, 1_091, 5, 18_030_192, 13_669_224),
    (1_513, 1, 1_536, 4, 19_034_112, 10_590),
    (1_513, 100, 1_537, 5, 19_035_496, 145_824),
    (1_513, 10_000, 1_560, 5, 19_067_323, 13_669_224),
    (1_587, 1, 1_610, 4, 19_136_512, 10_590),
    (1_587, 100, 1_611, 5, 19_137_850, 145_824),
    (1_587, 10_000, 1_639, 5, 19_175_294, 13_669_224),
    (4_096, 1, 4_125, 5, 24_610_609, 10_590),
    (4_096, 100, 4_125, 5, 24_610_609, 145_824),
    (4_096, 10_000, 4_144, 5, 24_635_968, 13_669_224),
    (8_192, 1, 8_231, 5, 34_363_584, 10_590),
    (8_192, 100, 8_231, 5, 34_363_584, 145_824),
    (8_192, 10_000, 8_255, 5, 34_420_416, 13_669_224),
    (16_384, 1, 16_441, 5, 53_759_296, 10_590),
    (16_384, 100, 16_442, 5, 53_761_152, 145_824),
    (16_384, 10_000, 16_476, 5, 53_824_256, 13_669_224),
    (65_536, 1, 65_637, 5, 99_490_976, 10_590),
    (65_536, 100, 65_637, 5, 99_490_976, 145_824),
    (65_536, 10_000, 65_661, 5, 99_491_744, 13_669_224),
    (262_144, 1, 262_251, 4, 106_701_215, 10_590),
    (262_144, 100, 262_252, 5, 106_701_257, 145_824),
    (262_144, 10_000, 262_268, 4, 106_701_932, 13_669_224),
    (1_048_576, 1, 1_048_720, 7, 142_768_820, 10_590),
    (1_048_576, 100, 1_048_720, 7, 142_768_820, 145_824),
    (1_048_576, 10_000, 1_048_751, 7, 142_824_080, 13_669_224),
)

#: size MiB, file count → container MiB, metadata B, copy slack B.
#: No safety column: without calibration there is nothing to advise from, and
#: the default 4 MiB is what goes in.
DEFAULT = (
    (1, 1, 25, 19_967_056, 198_144),
    (1, 100, 25, 19_967_056, 350_208),
    (1, 10_000, 78, 20_061_533, 15_556_608),
    (64, 1, 88, 20_079_359, 198_144),
    (64, 100, 88, 20_079_359, 350_208),
    (64, 10_000, 117, 20_131_054, 15_556_608),
    (512, 1, 537, 20_879_737, 198_144),
    (512, 100, 537, 20_879_737, 350_208),
    (512, 10_000, 586, 20_967_083, 15_556_608),
    (1_024, 1, 1_050, 21_794_200, 198_144),
    (1_024, 100, 1_050, 21_794_200, 350_208),
    (1_024, 10_000, 1_095, 21_874_416, 15_556_608),
    (1_513, 1, 1_540, 22_667_664, 198_144),
    (1_513, 100, 1_540, 22_667_664, 350_208),
    (1_513, 10_000, 1_565, 22_712_228, 15_556_608),
    (1_587, 1, 1_614, 22_799_575, 198_144),
    (1_587, 100, 1_614, 22_799_575, 350_208),
    (1_587, 10_000, 1_643, 22_851_270, 15_556_608),
    (4_096, 1, 4_127, 27_279_196, 198_144),
    (4_096, 100, 4_127, 27_279_196, 350_208),
    (4_096, 10_000, 4_147, 27_314_848, 15_556_608),
    (8_192, 1, 8_230, 34_593_119, 198_144),
    (8_192, 100, 8_230, 34_593_119, 350_208),
    (8_192, 10_000, 8_256, 34_639_466, 15_556_608),
    (16_384, 1, 16_436, 49_220_964, 198_144),
    (16_384, 100, 16_436, 49_220_964, 350_208),
    (16_384, 10_000, 16_473, 49_286_919, 15_556_608),
    (65_536, 1, 65_672, 136_988_033, 198_144),
    (65_536, 100, 65_672, 136_988_033, 350_208),
    (65_536, 10_000, 65_697, 137_032_598, 15_556_608),
    (262_144, 1, 262_614, 488_052_746, 198_144),
    (262_144, 100, 262_615, 488_054_528, 350_208),
    (262_144, 10_000, 262_634, 488_088_398, 15_556_608),
    (1_048_576, 1, 1_050_386, 1_892_318_727, 198_144),
    (1_048_576, 100, 1_050_386, 1_892_318_727, 350_208),
    (1_048_576, 10_000, 1_050_418, 1_892_375_770, 15_556_608),
)


def payload_of(size_mib: int, file_count: int) -> Payload:
    """The grid's payload: equal files, the remainder dropped.

    Equal files rather than a realistic mix, because the table has to be
    reproducible from two numbers alone. The remainder of the division is
    dropped for the same reason.
    """
    each = size_mib * MIB // file_count
    return Payload(
        logical_bytes=each * file_count,
        alloc_bytes=round_up(each, DEFAULT_CLUSTER_BYTES) * file_count,
        file_count=file_count,
        cluster_bytes=DEFAULT_CLUSTER_BYTES,
    )


def factory_rows() -> list[tuple[int, ...]]:
    """The Calculation tab's path on an untouched store.

    The store is empty and its files are never read: `calibration_records`
    fills it from the factory data by itself, which is exactly the state a
    fresh copy of the program starts in. The safety margin is advised the way
    `CalcTab._advise_safety` does it — solve once with the default, advise for
    that volume size, solve again.

    The path is deliberately one that cannot exist, and the emptiness is
    asserted rather than assumed: `Store` does not read anything in its
    constructor today, and if it ever starts to, the table must fail loudly
    instead of quietly taking in whoever's records lie in the current folder.
    """
    store = Store(path=Path(__file__).with_name("no-such-store.json"))
    assert not store.records and not store.calibration
    ntfs, slack = store.models()
    safety = store.safety()
    rows = []
    for size_mib in SIZES_MIB:
        for count in COUNTS:
            payload = payload_of(size_mib, count)
            probe = solve_container_mib(
                payload, ntfs=ntfs, slack=slack, safety_bytes=DEFAULT_SAFETY_BYTES
            )
            advice = safety.advise(probe.volume_bytes, payload.file_count)
            solution = solve_container_mib(
                payload,
                ntfs=ntfs,
                slack=slack,
                safety_bytes=advice.total_mib * MIB,
            )
            rows.append(
                (
                    size_mib,
                    count,
                    solution.container_mib,
                    advice.total_mib,
                    solution.ntfs_bytes,
                    solution.copy_slack,
                )
            )
    return rows


def default_rows() -> list[tuple[int, ...]]:
    """The same grid with no calibration at all."""
    rows = []
    for size_mib in SIZES_MIB:
        for count in COUNTS:
            payload = payload_of(size_mib, count)
            solution = solve_container_mib(
                payload,
                ntfs=NtfsModel(),
                slack=CopySlackModel(),
                safety_bytes=DEFAULT_SAFETY_BYTES,
            )
            rows.append(
                (
                    size_mib,
                    count,
                    solution.container_mib,
                    solution.ntfs_bytes,
                    solution.copy_slack,
                )
            )
    return rows


class GoldenSolverTests(unittest.TestCase):
    def test_factory_models_give_the_frozen_answers(self):
        for row, expected in zip(factory_rows(), FACTORY, strict=True):
            with self.subTest(size_mib=expected[0], file_count=expected[1]):
                self.assertEqual(row, expected)

    def test_uncalibrated_models_give_the_frozen_answers(self):
        for row, expected in zip(default_rows(), DEFAULT, strict=True):
            with self.subTest(size_mib=expected[0], file_count=expected[1]):
                self.assertEqual(row, expected)

    def test_the_grid_is_covered_whole(self):
        """A shortened sweep would pass row by row and prove nothing."""
        self.assertEqual(len(FACTORY), len(SIZES_MIB) * len(COUNTS))
        self.assertEqual(len(DEFAULT), len(SIZES_MIB) * len(COUNTS))
        self.assertEqual(len(factory_rows()), len(FACTORY))
        self.assertEqual(len(default_rows()), len(DEFAULT))


def _print_tables() -> None:
    """Print both tables ready to be pasted above."""
    for name, rows in (("FACTORY", factory_rows()), ("DEFAULT", default_rows())):
        print(f"{name} = (")
        for row in rows:
            print("    (" + ", ".join(f"{value:_}" for value in row) + "),")
        print(")")
        print()


if __name__ == "__main__":
    import sys

    if "--print" in sys.argv:
        _print_tables()
    else:
        unittest.main()
