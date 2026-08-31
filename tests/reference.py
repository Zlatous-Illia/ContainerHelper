"""Три записи, снятые вручную с реальных контейнеров.

Значения взяты из свойств тома в Проводнике. Хранятся только измеренные поля —
заголовок VeraCrypt, метаданные NTFS и запас на копирование выводятся из них.

Cache 2 намеренно оставлена как есть: её left_bytes физически невозможен
(занято выходит меньше самого файла), и на ней проверяется, что валидация это
ловит, но точку для модели NTFS из неё всё равно берёт.
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

#: Метаданные NTFS, выведенные как mounted - empty_free.
EXPECTED_NTFS = {
    "Cache 1": 40_316_928,
    "Cache 2": 36_573_184,
    "Cache 4": 34_074_624,
}

#: Запас на копирование, выведенный как (empty_free - left) - alloc(file).
#: Обе записи с одним файлом. Cache 2 отсутствует: там величина отрицательна.
EXPECTED_SLACK = {
    "Cache 1": 143_360,
    "Cache 4": 114_688,
}

#: Наименьший размер контейнера, которого хватило бы: фактически заданный
#: минус измеренный остаток, округлённый вниз до целых MiB.
TRUE_MINIMUM_MIB = {
    "Cache 1": 11057,
    "Cache 4": 8020,
}
