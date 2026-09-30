"""Frozen output of the solver on a grid of payload size and file count.

Stage A of the volume-profile work touches the core: the header constant
splits into the VeraCrypt headers and the filesystem tail, the metadata model
takes a profile, and the X axis of the calibration points changes from the
measured volume capacity to the computed volume size. On NTFS with a 4 KiB
cluster none of that may move the answer by a byte, and this file is what says
so: the tables were taken before the first of those changes.

Two model sets, because they fail differently. FACTORY is the path the
Calculation tab walks on the first recalculation after startup — the models an
empty store builds from the factory data, the safety margin advised for that
very size, then the solve with it. The first one, not any one: the tab seeds
its probe with the spin box's current value, so a later recalculation can start
from a different margin. DEFAULT is the uncalibrated affine model
`19 MiB + 4 KiB + 0.17 % of the volume`, where a changed definition of the
volume size shows up directly.

The metadata is frozen beside the container size on purpose: a drift of a few
bytes in the model hides inside the rounding to whole mebibytes. A drift in
VC_HEADERS_BYTES itself is not caught here and does not need to be — the
reference records pin it to the byte
(`test_model.DerivedValueTests.test_veracrypt_header_is_constant_across_records`).
The copy slack moves with neither, and is frozen for its own sake: it pins the
factory slack calibration, not the solver.

What Stage A2 did to these tables — the header kept the 262 144 B of VeraCrypt
headers, the 4096 B filesystem tail moved into the metadata, and both axes of
every calibration point grew by the tail — and why each change is the
migration and not a regression:

* Container size, safety margin and copy slack: unchanged on every row of both
  tables.
* FACTORY metadata: exactly +4096 B on every row. That is the tail changing
  which column it is counted in, not the model moving: shifting both axes of
  every point by the same amount leaves the interpolation, its bound and the
  leave-one-out misses where they were.
* DEFAULT metadata: +4103 B, and +4102 B on two rows. 4096 B of it is the tail,
  added to the default model's base, because the 19 MiB were fitted while the
  tail sat in the header; the other 7 B are `ceil(0.0017 × 4096)`, the same
  affine model asked about a volume 4096 B larger (6 B on the two rows where
  the rounding falls the other way). Left in rather than compensated: it is an
  overestimate, the safe side, and it crossed no mebibyte boundary.

A simulation made before A2 predicted two 1 MiB drops — one where the advised
margin sat 516 B above a mebibyte boundary (1587 MiB in ten thousand files),
one in the default model on a terabyte. Neither happened. The second is what
the 4 KiB in the default base prevents; the first the real change does not
reproduce, and why the simulation gave it was not traced — the simulation is
gone, and the real answer is the one above.

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
    MetadataModel,
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
    (1, 1, 23, 7, 14_820_462, 10_590),
    (1, 100, 23, 7, 14_820_462, 145_824),
    (1, 10_000, 74, 7, 14_911_374, 13_669_224),
    (64, 1, 86, 7, 14_932_765, 10_590),
    (64, 100, 86, 7, 14_932_765, 145_824),
    (64, 10_000, 111, 5, 14_938_112, 13_669_224),
    (512, 1, 534, 5, 17_227_191, 10_590),
    (512, 100, 534, 5, 17_227_191, 145_824),
    (512, 10_000, 582, 5, 17_297_408, 13_669_224),
    (1_024, 1, 1_047, 5, 17_935_024, 10_590),
    (1_024, 100, 1_047, 5, 17_935_024, 145_824),
    (1_024, 10_000, 1_091, 5, 18_034_288, 13_669_224),
    (1_513, 1, 1_536, 4, 19_038_208, 10_590),
    (1_513, 100, 1_537, 5, 19_039_592, 145_824),
    (1_513, 10_000, 1_560, 5, 19_071_419, 13_669_224),
    (1_587, 1, 1_610, 4, 19_140_608, 10_590),
    (1_587, 100, 1_611, 5, 19_141_946, 145_824),
    (1_587, 10_000, 1_639, 5, 19_179_390, 13_669_224),
    (4_096, 1, 4_125, 5, 24_614_705, 10_590),
    (4_096, 100, 4_125, 5, 24_614_705, 145_824),
    (4_096, 10_000, 4_144, 5, 24_640_064, 13_669_224),
    (8_192, 1, 8_231, 5, 34_367_680, 10_590),
    (8_192, 100, 8_231, 5, 34_367_680, 145_824),
    (8_192, 10_000, 8_255, 5, 34_424_512, 13_669_224),
    (16_384, 1, 16_441, 5, 53_763_392, 10_590),
    (16_384, 100, 16_442, 5, 53_765_248, 145_824),
    (16_384, 10_000, 16_476, 5, 53_828_352, 13_669_224),
    (65_536, 1, 65_637, 5, 99_495_072, 10_590),
    (65_536, 100, 65_637, 5, 99_495_072, 145_824),
    (65_536, 10_000, 65_661, 5, 99_495_840, 13_669_224),
    (262_144, 1, 262_251, 4, 106_705_311, 10_590),
    (262_144, 100, 262_252, 5, 106_705_353, 145_824),
    (262_144, 10_000, 262_268, 4, 106_706_028, 13_669_224),
    (1_048_576, 1, 1_048_720, 7, 142_772_916, 10_590),
    (1_048_576, 100, 1_048_720, 7, 142_772_916, 145_824),
    (1_048_576, 10_000, 1_048_751, 7, 142_828_176, 13_669_224),
)

#: size MiB, file count → container MiB, metadata B, copy slack B.
#: No safety column: without calibration there is nothing to advise from, and
#: the default 4 MiB is what goes in.
DEFAULT = (
    (1, 1, 25, 19_971_159, 198_144),
    (1, 100, 25, 19_971_159, 350_208),
    (1, 10_000, 78, 20_065_636, 15_556_608),
    (64, 1, 88, 20_083_462, 198_144),
    (64, 100, 88, 20_083_462, 350_208),
    (64, 10_000, 117, 20_135_157, 15_556_608),
    (512, 1, 537, 20_883_840, 198_144),
    (512, 100, 537, 20_883_840, 350_208),
    (512, 10_000, 586, 20_971_186, 15_556_608),
    (1_024, 1, 1_050, 21_798_303, 198_144),
    (1_024, 100, 1_050, 21_798_303, 350_208),
    (1_024, 10_000, 1_095, 21_878_519, 15_556_608),
    (1_513, 1, 1_540, 22_671_767, 198_144),
    (1_513, 100, 1_540, 22_671_767, 350_208),
    (1_513, 10_000, 1_565, 22_716_331, 15_556_608),
    (1_587, 1, 1_614, 22_803_678, 198_144),
    (1_587, 100, 1_614, 22_803_678, 350_208),
    (1_587, 10_000, 1_643, 22_855_372, 15_556_608),
    (4_096, 1, 4_127, 27_283_299, 198_144),
    (4_096, 100, 4_127, 27_283_299, 350_208),
    (4_096, 10_000, 4_147, 27_318_951, 15_556_608),
    (8_192, 1, 8_230, 34_597_222, 198_144),
    (8_192, 100, 8_230, 34_597_222, 350_208),
    (8_192, 10_000, 8_256, 34_643_569, 15_556_608),
    (16_384, 1, 16_436, 49_225_067, 198_144),
    (16_384, 100, 16_436, 49_225_067, 350_208),
    (16_384, 10_000, 16_473, 49_291_022, 15_556_608),
    (65_536, 1, 65_672, 136_992_136, 198_144),
    (65_536, 100, 65_672, 136_992_136, 350_208),
    (65_536, 10_000, 65_697, 137_036_701, 15_556_608),
    (262_144, 1, 262_614, 488_056_849, 198_144),
    (262_144, 100, 262_615, 488_058_631, 350_208),
    (262_144, 10_000, 262_634, 488_092_500, 15_556_608),
    (1_048_576, 1, 1_050_386, 1_892_322_830, 198_144),
    (1_048_576, 100, 1_050_386, 1_892_322_830, 350_208),
    (1_048_576, 10_000, 1_050_418, 1_892_379_873, 15_556_608),
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
                    solution.metadata_bytes,
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
                ntfs=MetadataModel(),
                slack=CopySlackModel(),
                safety_bytes=DEFAULT_SAFETY_BYTES,
            )
            rows.append(
                (
                    size_mib,
                    count,
                    solution.container_mib,
                    solution.metadata_bytes,
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
