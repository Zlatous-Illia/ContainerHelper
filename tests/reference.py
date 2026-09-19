"""Three records taken by hand from real containers.

The values come from the volume properties in Explorer. Only the measured
fields are stored — the VeraCrypt header, the NTFS metadata and the copy slack
are derived from them.

Cache 2 is deliberately kept as is: its left_bytes is physically impossible
(the used space comes out smaller than the file itself), and it is used to
check that validation catches this but still takes the point for the NTFS
model from it.
"""

from containerhelper.records import Record

CACHE_1 = Record(
    id="Cache 1",
    created="2026-08-29T00:00:01",
    container_mib=11130,
    mounted_bytes=11_670_384_640,
    empty_free_bytes=11_630_067_712,
    file_bytes=11_553_254_233,
    file_count=1,
    left_bytes=76_668_928,
)

CACHE_2 = Record(
    id="Cache 2",
    created="2026-08-29T00:00:02",
    container_mib=10475,
    mounted_bytes=10_983_567_360,
    empty_free_bytes=10_946_994_176,
    file_bytes=10_941_734_967,
    file_count=1,
    left_bytes=15_847_424,
)

CACHE_4 = Record(
    id="Cache 4",
    created="2026-08-29T00:00:04",
    container_mib=8050,
    mounted_bytes=8_440_770_560,
    empty_free_bytes=8_406_695_936,
    file_bytes=8_374_112_625,
    file_count=1,
    left_bytes=32_464_896,
)

ALL = [CACHE_1, CACHE_2, CACHE_4]

#: NTFS metadata, derived as mounted - empty_free.
EXPECTED_NTFS = {
    "Cache 1": 40_316_928,
    "Cache 2": 36_573_184,
    "Cache 4": 34_074_624,
}

#: Copy slack, derived as (empty_free - left) - alloc(file).
#: Both records hold one file. Cache 2 is missing: there the value is negative.
EXPECTED_SLACK = {
    "Cache 1": 143_360,
    "Cache 4": 114_688,
}

#: The smallest container size that would have been enough: the one actually
#: set minus the measured left space, rounded down to whole MiB.
TRUE_MINIMUM_MIB = {
    "Cache 1": 11057,
    "Cache 4": 8020,
}
