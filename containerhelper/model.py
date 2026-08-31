"""Модели накладных расходов и расчёт размера контейнера VeraCrypt.

Весь модуль работает в целых байтах. Единственное место, где появляется float, —
коэффициенты моделей; результат немедленно округляется вверх до целого.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

MIB = 1024 * 1024

#: Заголовок VeraCrypt. Измерено на трёх контейнерах, совпало до байта:
#: container_bytes - mounted_bytes == 266240 для 8050, 10475 и 11130 MiB.
VC_HEADER_BYTES = 266_240

#: Модель метаданных NTFS по умолчанию: 19 MiB + 0.17 % от размера тома.
#: Максимальная недооценка на трёх имеющихся измерениях — 0.55 MiB.
DEFAULT_NTFS_BASE = 19 * MIB
DEFAULT_NTFS_RATE = 0.0017

#: Запас на копирование, когда нет вообще никаких замеров — ни своих, ни
#: заводских. Обе части намеренно осторожны, но по разным причинам.
#:
#: По-файловая часть измерена: на рядах 500, 5 000 и 10 000 файлов скорость
#: вышла 1363 B на файл и держалась в пределах двух байт. Она и раскладывается
#: физически — 1024 B на запись MFT плюс ~339 B на запись в индексе каталога.
#: Здесь взято с запасом, потому что вторая половина зависит от длины имени
#: файла: замеры сняты на именах в 34 символа, а у настоящих данных они бывают
#: и вдвое длиннее.
#:
#: Постоянная часть осталась прежней, хотя синтетический замер при n = 1 дал
#: всего 4096 B — один кластер. Расхождение с ручными записями (114 688 и
#: 143 360 B при том же n = 1) не объяснено: их копировали Проводником, а не
#: писали программой, и что именно Windows добавляет вокруг настоящего
#: копирования, неизвестно. Пользователь копирует Проводником, поэтому здесь
#: держится большее из двух: 192 KiB стоят ничего рядом со страховкой в
#: мегабайты, а занижение — единственная опасная сторона.
DEFAULT_SLACK_BASE = 192 * 1024
DEFAULT_SLACK_PER_FILE = 1536

DEFAULT_SAFETY_BYTES = 4 * MIB
DEFAULT_CLUSTER_BYTES = 4096

#: Нижняя граница предсказания метаданных NTFS. Защищает от вырожденной
#: экстраполяции по двум близким шумным точкам.
MIN_NTFS_BYTES = MIB


def ceil_div(value: int, divisor: int) -> int:
    """Деление с округлением вверх, только на целых."""
    return -(-value // divisor)


def round_up(value: int, unit: int) -> int:
    """Округление вверх до кратного unit."""
    return ceil_div(value, unit) * unit


class NtfsModel:
    """Метаданные NTFS как функция размера смонтированного тома.

    Меньше двух точек — аффинная модель по умолчанию. Две и больше —
    кусочно-линейная интерполяция по измеренным точкам, за пределами
    диапазона линейная экстраполяция по двум крайним.

    Кусочно-линейная форма выбрана потому, что зависимость не пропорциональна:
    $LogFile почти не растёт с томом и упирается в потолок 64 MiB, линейно
    растёт только $Bitmap. Одна прямая через 1 GiB и 100 GiB дала бы
    систематическую ошибку.
    """

    def __init__(
        self,
        points: Iterable[tuple[int, int]] | None = None,
        base: int = DEFAULT_NTFS_BASE,
        rate: float = DEFAULT_NTFS_RATE,
    ) -> None:
        self.base = base
        self.rate = rate
        self.points = self._dedupe(points or ())

    @staticmethod
    def _dedupe(points: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
        """Свернуть совпадающие размеры тома, оставив наибольшее значение.

        Наибольшее, а не среднее: ошибка модели должна уходить в безопасную
        сторону.
        """
        best: dict[int, int] = {}
        for volume, overhead in points:
            if volume <= 0 or overhead < 0:
                continue
            if volume not in best or overhead > best[volume]:
                best[volume] = overhead
        return sorted(best.items())

    @property
    def calibrated(self) -> bool:
        return len(self.points) >= 2

    @property
    def covered_range(self) -> tuple[int, int] | None:
        """Диапазон размеров тома, покрытый измерениями."""
        if not self.points:
            return None
        return self.points[0][0], self.points[-1][0]

    def is_extrapolation(self, volume_bytes: int) -> bool:
        span = self.covered_range
        if span is None or not self.calibrated:
            return True
        return not (span[0] <= volume_bytes <= span[1])

    def overhead(self, volume_bytes: int) -> int:
        if not self.calibrated:
            value = self.base + math.ceil(self.rate * volume_bytes)
        else:
            value = self._interpolate(volume_bytes)
        return max(value, MIN_NTFS_BYTES)

    def _interpolate(self, volume_bytes: int) -> int:
        points = self.points
        first, last = points[0], points[-1]

        if volume_bytes < first[0]:
            return self._extend(first, volume_bytes)
        if volume_bytes > last[0]:
            return self._extend(last, volume_bytes)

        index = 0
        while points[index + 1][0] < volume_bytes:
            index += 1
        (x0, y0), (x1, y1) = points[index], points[index + 1]
        return y0 + ceil_div((y1 - y0) * (volume_bytes - x0), x1 - x0)

    def _segment_slopes(self) -> list[float]:
        points = self.points
        return [
            (points[i + 1][1] - points[i][1]) / (points[i + 1][0] - points[i][0])
            for i in range(len(points) - 1)
        ]

    def interpolation_bound(self, volume_bytes: int) -> int:
        """Насколько модель может занизить именно в этой точке.

        Между двумя замерами модель ведёт прямую, а настоящая зависимость
        прямой не является. Там, где она выпукла, хорда идёт сверху и занизить
        нельзя. Там, где вогнута — а выше 16 GiB она именно такая, потому что
        составляющие метаданных по очереди упираются в свои потолки, — хорда
        проходит под кривой, и вот этот зазор и есть риск.

        Сверху кривая ограничена двумя прямыми: из левого конца с наклоном
        предыдущего отрезка и из правого с наклоном следующего. Ближайшая из
        них минус хорда и даёт границу. За последним отрезком наклон
        считается нулевым: это самое осторожное предположение, и оно же
        близко к правде — на больших томах растёт только $Bitmap.

        Вне измеренного диапазона возвращается ноль: там работает не
        интерполяция, а экстраполяция, и её погрешность оценивается иначе.
        """
        if not self.calibrated:
            return 0
        points = self.points
        if not (points[0][0] <= volume_bytes <= points[-1][0]):
            return 0

        slopes = self._segment_slopes()
        index = 0
        while points[index + 1][0] < volume_bytes:
            index += 1
        (x0, y0), (x1, y1) = points[index], points[index + 1]

        left_slope = slopes[index - 1] if index else slopes[index]
        right_slope = slopes[index + 1] if index + 1 < len(slopes) else 0.0

        ceiling = min(
            y0 + left_slope * (volume_bytes - x0),
            y1 - right_slope * (x1 - volume_bytes),
        )
        chord = y0 + (y1 - y0) * (volume_bytes - x0) / (x1 - x0)
        return max(0, math.ceil(ceiling - chord))

    def _extend(self, anchor: tuple[int, int], volume_bytes: int) -> int:
        """Продлить зависимость за пределы измеренного диапазона.

        Наклон берётся не подогнанный, а базовый — тот же, что в модели по
        умолчанию, — и привязывается к ближайшей измеренной точке. Подогнанный
        наклон за пределами данных ненадёжен: замеры часто стоят вплотную,
        и их локальный наклон, вынесенный в разы дальше собственного размаха,
        промахивается на порядок. Проверка исключением давала на этом 10.8 MiB
        недооценки при страховке в 4 MiB.

        Внутри диапазона верны измерения, снаружи — физический прирост
        (`$Bitmap` линеен по размеру тома, `$LogFile` упирается в потолок),
        сдвинутый так, чтобы совпасть с краем измеренного.
        """
        x_anchor, y_anchor = anchor
        return y_anchor + math.ceil(self.rate * (volume_bytes - x_anchor))


class CopySlackModel:
    """Место, которое занимают сами файлы сверх своего кластерного размера.

    Запись MFT на каждый файл, рост индексов каталогов, служебные структуры
    первой записи на томе.
    """

    def __init__(
        self,
        base: int = DEFAULT_SLACK_BASE,
        per_file: int = DEFAULT_SLACK_PER_FILE,
        sample_count: int = 0,
        file_counts: tuple[int, ...] = (),
    ) -> None:
        self.base = base
        self.per_file = per_file
        self.sample_count = sample_count
        self.file_counts = file_counts

    @property
    def calibrated(self) -> bool:
        return self.sample_count > 0

    @property
    def per_file_calibrated(self) -> bool:
        """Отделить по-файловую составляющую можно только при разных n."""
        return len(set(self.file_counts)) >= 2

    def slack(self, file_count: int) -> int:
        return self.base + self.per_file * max(file_count, 1)

    @classmethod
    def calibrate(cls, samples: Sequence[tuple[int, int]]) -> "CopySlackModel":
        """Подобрать коэффициенты по парам (число файлов, измеренный запас).

        Наклон берётся методом наименьших квадратов, но только если записи
        покрывают хотя бы два разных n и наклон вышел неотрицательным; иначе
        остаётся значение по умолчанию. Свободный член после этого поднимается
        до верхней огибающей, чтобы модель не занижала ни одну из записей.
        """
        usable = [(n, value) for n, value in samples if n >= 1 and value >= 0]
        if not usable:
            return cls()

        counts = [n for n, _ in usable]
        per_file = DEFAULT_SLACK_PER_FILE
        if len(set(counts)) >= 2:
            slope = cls._least_squares_slope(usable)
            if slope > 0:
                per_file = math.ceil(slope)

        base = max(value - per_file * n for n, value in usable)
        return cls(
            base=max(base, 0),
            per_file=per_file,
            sample_count=len(usable),
            file_counts=tuple(counts),
        )

    @staticmethod
    def _least_squares_slope(samples: Sequence[tuple[int, int]]) -> float:
        n_mean = sum(n for n, _ in samples) / len(samples)
        y_mean = sum(y for _, y in samples) / len(samples)
        numerator = sum((n - n_mean) * (y - y_mean) for n, y in samples)
        denominator = sum((n - n_mean) ** 2 for n, _ in samples)
        return numerator / denominator if denominator else 0.0


@dataclass(frozen=True)
class Payload:
    """То, что предстоит положить в контейнер."""

    logical_bytes: int
    alloc_bytes: int
    file_count: int
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES

    @property
    def cluster_tail(self) -> int:
        return self.alloc_bytes - self.logical_bytes

    @classmethod
    def for_file(
        cls, size_bytes: int, cluster_bytes: int = DEFAULT_CLUSTER_BYTES
    ) -> "Payload":
        return cls(
            logical_bytes=size_bytes,
            alloc_bytes=round_up(size_bytes, cluster_bytes),
            file_count=1,
            cluster_bytes=cluster_bytes,
        )

    @classmethod
    def for_files(
        cls,
        sizes: Iterable[int],
        cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
    ) -> "Payload":
        """Сумма кластерных размеров, а не логических.

        Стоимость записей MFT сюда не входит — она целиком относится к
        CopySlackModel, чтобы не задваиваться.
        """
        logical = 0
        alloc = 0
        count = 0
        for size in sizes:
            logical += size
            alloc += round_up(size, cluster_bytes)
            count += 1
        return cls(
            logical_bytes=logical,
            alloc_bytes=alloc,
            file_count=count,
            cluster_bytes=cluster_bytes,
        )


@dataclass(frozen=True)
class Solution:
    """Разложение результата по слагаемым — для таблицы на вкладке «Расчёт»."""

    container_mib: int
    container_bytes: int
    volume_bytes: int
    payload_logical: int
    payload_alloc: int
    cluster_tail: int
    vc_header: int
    ntfs_bytes: int
    copy_slack: int
    safety_bytes: int
    predicted_left_bytes: int
    ntfs_extrapolated: bool
    slack_unverified: bool


def solve_container_mib(
    payload: Payload,
    ntfs: NtfsModel | None = None,
    slack: CopySlackModel | None = None,
    safety_bytes: int = DEFAULT_SAFETY_BYTES,
    max_iterations: int = 8,
) -> Solution:
    """Найти минимальный размер контейнера в MiB, в который влезет payload.

    Метаданные NTFS зависят от размера тома, а он — от искомого результата,
    поэтому решение итеративное. Сходится за две-три итерации: NTFS меняется
    много медленнее, чем сам том.
    """
    ntfs = ntfs or NtfsModel()
    slack = slack or CopySlackModel()

    slack_bytes = slack.slack(payload.file_count)
    fixed = payload.alloc_bytes + VC_HEADER_BYTES + slack_bytes + safety_bytes

    volume_guess = payload.alloc_bytes
    container_mib = 0
    ntfs_bytes = 0

    for _ in range(max_iterations):
        ntfs_bytes = ntfs.overhead(volume_guess)
        container_mib = ceil_div(fixed + ntfs_bytes, MIB)
        next_volume = container_mib * MIB - VC_HEADER_BYTES
        if next_volume == volume_guess:
            break
        volume_guess = next_volume

    container_bytes = container_mib * MIB
    volume_bytes = container_bytes - VC_HEADER_BYTES
    ntfs_bytes = ntfs.overhead(volume_bytes)

    return Solution(
        container_mib=container_mib,
        container_bytes=container_bytes,
        volume_bytes=volume_bytes,
        payload_logical=payload.logical_bytes,
        payload_alloc=payload.alloc_bytes,
        cluster_tail=payload.cluster_tail,
        vc_header=VC_HEADER_BYTES,
        ntfs_bytes=ntfs_bytes,
        copy_slack=slack_bytes,
        safety_bytes=safety_bytes,
        predicted_left_bytes=(
            volume_bytes - ntfs_bytes - payload.alloc_bytes - slack_bytes
        ),
        ntfs_extrapolated=ntfs.is_extrapolation(volume_bytes),
        slack_unverified=payload.file_count > 1 and not slack.per_file_calibrated,
    )


#: Надбавка, пока отрезок держится только на заводских замерах. Совпадает
#: со значением по умолчанию: чужие данные стоят ровно столько же доверия,
#: сколько модель без калибровки вообще.
FACTORY_MARGIN_BYTES = 4 * MIB

#: Ниже этого страховка не опускается никогда. Замер свободного места сам по
#: себе слегка шумит, и советовать ноль было бы враньём о точности.
MIN_SAFETY_BYTES = MIB

#: Во сколько раз может отличаться число файлов. Здесь окно шире: по-файловая
#: часть меняется медленно, а замеров будет мало.
SLACK_NEIGHBOUR_RATIO = 8.0


@dataclass(frozen=True)
class SafetyAdvice:
    """Сколько страховки нужно именно этому расчёту и почему."""

    total_bytes: int
    ntfs_bytes: int
    slack_bytes: int
    ntfs_reason: str
    slack_reason: str
    #: Имена записей, на которых построен совет. Пусто — значит не на чем.
    basis: tuple[str, ...] = ()

    @property
    def total_mib(self) -> int:
        return ceil_div(self.total_bytes, MIB)


class SafetyModel:
    """Страховка под конкретный размер, а не одно число на все случаи.

    Общий «наибольший промах по всем записям» — величина бесполезная: она
    берётся с того размера, где модель слабее всего, и тащит эту слабость на
    расчёты, которым до неё нет дела. Контейнер на 5 GiB не должен платить за
    то, что кривая ломается в районе 48 GiB.

    Совет складывается из двух независимых частей:

    * NTFS — граница интерполяции ровно в этой точке и промахи на записях
      похожего размера, приведённые к ширине нужного отрезка;
    * запас на копирование — промахи на замерах с похожим числом файлов.

    Про приведение к ширине. Промах берётся из проверки исключением: только
    она показывает, как модель ведёт себя там, где точки не было. Но она
    меряет модель без одной точки, то есть с прорехой примерно вдвое шире
    настоящей, и брать её промах как есть — это и был тот самый общий
    «наибольший промах», от которого здесь уходим: он не спадает от
    добавления замеров, потому что каждый новый замер тут же выкалывают.
    Погрешность линейной интерполяции растёт как квадрат ширины прорехи, так
    что промах, снятый на прорехе `h_loo`, пересчитывается на настоящий
    отрезок `h` множителем `(h / h_loo)²`. Взять остаток полной модели было
    бы бесполезно: на своей же точке он ноль по построению.

    Там, где записей рядом нет, честный ответ — значение по умолчанию, а не
    оптимистичный ноль.
    """

    def __init__(
        self,
        ntfs: NtfsModel | None = None,
        ntfs_deviations: Sequence[tuple[int, int, str]] = (),
        slack_deviations: Sequence[tuple[int, int, str]] = (),
        extrapolation_bytes: int = DEFAULT_SAFETY_BYTES,
        floor: int = MIN_SAFETY_BYTES,
        default_bytes: int = DEFAULT_SAFETY_BYTES,
        factory_volumes: set[int] | None = None,
        factory_margin: int = FACTORY_MARGIN_BYTES,
    ) -> None:
        self.ntfs = ntfs or NtfsModel()
        self.ntfs_deviations = tuple(ntfs_deviations)
        self.slack_deviations = tuple(slack_deviations)
        #: Что закладывать за краем замеров: там локальных свидетельств нет
        #: вовсе, и осторожность — единственный доступный ответ.
        self.extrapolation_bytes = extrapolation_bytes
        self.floor = floor
        self.default_bytes = default_bytes
        #: Тома, покрытые только заводскими данными, и надбавка за них. Чужая
        #: сборка Windows могла выбрать другой размер $LogFile; занижение —
        #: единственная опасная сторона, поэтому пока оба конца отрезка
        #: заводские, к страховке добавляется постоянная величина. Свой замер
        #: рядом её снимает.
        self.factory_volumes = set(factory_volumes or ())
        self.factory_margin = factory_margin

    @staticmethod
    def _nearby(
        samples: Sequence[tuple[int, int, str]], target: int, ratio: float
    ) -> list[tuple[int, int, str]]:
        """Записи, размер которых отличается от искомого не больше чем в ratio раз."""
        if target <= 0:
            return []
        low, high = target / ratio, target * ratio
        return [item for item in samples if item[0] and low <= item[0] <= high]

    @staticmethod
    def _worst(samples: Sequence[tuple[int, int, str]]) -> int:
        """Наибольшая недооценка среди выборки. Перезаклады не в счёт."""
        return max((deviation for _, deviation, _ in samples), default=0)

    def _advise_ntfs(self, volume_bytes: int) -> tuple[int, str, tuple[str, ...]]:
        if not self.ntfs.calibrated:
            return (
                self.default_bytes,
                "модель NTFS не откалибрована — значение по умолчанию",
                (),
            )

        if self.ntfs.is_extrapolation(volume_bytes):
            return (
                max(self.extrapolation_bytes, self.default_bytes),
                "размер вне измеренного диапазона — осторожная оценка по всем "
                "записям сразу",
                (),
            )

        bound = self.ntfs.interpolation_bound(volume_bytes)
        span = self._segment_span(volume_bytes)
        neighbours = self._segment_endpoints(volume_bytes)

        # Вес обнуляет чужой промах на самих замерах и поднимает его к
        # середине прорехи: ровно там линейная интерполяция и ошибается,
        # а в узлах она точна по построению.
        weight = self._segment_weight(volume_bytes)
        scaled = 0
        for volume, deviation, _ in neighbours:
            if deviation <= 0:
                continue
            gap = self._loo_span(volume)
            if not gap:
                continue
            scaled = max(scaled, math.ceil(deviation * weight * (span / gap) ** 2))

        names = tuple(name for _, _, name in neighbours)
        value = max(bound, scaled)
        if not value:
            reason = "модель здесь не занижает: замер рядом, хорда идёт сверху"
        elif scaled > bound:
            reason = "промах на записях похожего размера, приведённый к этому отрезку"
        else:
            reason = "изгиб кривой между соседними замерами"

        if self._leans_on_factory(volume_bytes):
            value += self.factory_margin
            reason += "; отрезок держится на заводских замерах"
        return value, reason, names

    def _leans_on_factory(self, volume_bytes: int) -> bool:
        """Оба конца отрезка — заводские точки, своих замеров рядом нет."""
        if not self.factory_volumes or not self.ntfs.calibrated:
            return False
        points = self.ntfs.points
        index = 0
        while index + 1 < len(points) - 1 and points[index + 1][0] < volume_bytes:
            index += 1
        edges = (points[index][0], points[index + 1][0])
        return all(edge in self.factory_volumes for edge in edges)

    def _segment_endpoints(
        self, volume_bytes: int
    ) -> list[tuple[int, int, str]]:
        """Записи, между которыми лежит искомый размер.

        «Похожий размер» — это именно они, а не всё, что попало в окно по
        отношению размеров. Окно шириной вдвое затягивало бы соседей через
        одного: рядом с 90 GiB оказывалась запись на 48 GiB, где у кривой
        излом, и её промах уезжал в плоскую область, где по замерам растёт
        одна битовая карта.
        """
        points = self.ntfs.points
        index = 0
        while index + 1 < len(points) - 1 and points[index + 1][0] < volume_bytes:
            index += 1
        edges = {points[index][0], points[index + 1][0]}
        return [item for item in self.ntfs_deviations if item[0] in edges]

    def _segment_weight(self, volume_bytes: int) -> float:
        """Насколько глубоко искомый размер сидит внутри отрезка.

        Ноль на концах, единица посередине. На самом замере модель точна, и
        приписывать ей там чужую погрешность нечестно.
        """
        points = self.ntfs.points
        index = 0
        while index + 1 < len(points) - 1 and points[index + 1][0] < volume_bytes:
            index += 1
        x0, x1 = points[index][0], points[index + 1][0]
        if x1 == x0:
            return 0.0
        position = (volume_bytes - x0) / (x1 - x0)
        position = min(max(position, 0.0), 1.0)
        return 4 * position * (1 - position)

    def _segment_span(self, volume_bytes: int) -> int:
        """Ширина отрезка между замерами, в который попал искомый размер."""
        points = self.ntfs.points
        index = 0
        while index + 1 < len(points) - 1 and points[index + 1][0] < volume_bytes:
            index += 1
        return points[index + 1][0] - points[index][0]

    def _loo_span(self, volume_bytes: int) -> int:
        """Ширина прорехи, которая возникает при исключении этой точки.

        Она и есть та ширина, на которой измерен промах: слева и справа
        остаются соседи выколотой точки.
        """
        points = self.ntfs.points
        volumes = [volume for volume, _ in points]
        if volume_bytes not in volumes or len(volumes) < 2:
            return 0
        index = volumes.index(volume_bytes)
        low = volumes[index - 1] if index else volumes[0]
        high = volumes[index + 1] if index + 1 < len(volumes) else volumes[-1]
        return high - low

    def _fallback_slack(self, file_count: int) -> tuple[int, str, tuple[str, ...]]:
        """Сколько закладывать, когда замеров запаса нет вовсе.

        Риск здесь зависит от числа файлов, а не от их объёма. Постоянная
        часть измерена и мала — сотни килобайт, поэтому одному файлу хватает
        нижней границы. По-файловая часть не подтверждена ничем, и вот она на
        большом числе файлов может уехать далеко: там берётся полное значение
        по умолчанию.
        """
        if file_count <= 1:
            return (
                self.floor,
                "один файл: постоянная часть запаса измерена, риск мал",
                (),
            )
        return (
            self.default_bytes,
            f"по-файловая часть запаса не подтверждена замерами, а файлов "
            f"{file_count} — значение по умолчанию",
            (),
        )

    def _advise_slack(self, file_count: int) -> tuple[int, str, tuple[str, ...]]:
        if not self.slack_deviations:
            return self._fallback_slack(file_count)
        neighbours = self._nearby(
            self.slack_deviations, max(file_count, 1), SLACK_NEIGHBOUR_RATIO
        )
        if not neighbours:
            return self._fallback_slack(file_count)
        return (
            max(self._worst(neighbours), 0),
            "промах на замерах с похожим числом файлов",
            tuple(name for _, _, name in neighbours),
        )

    def advise(self, volume_bytes: int, file_count: int = 1) -> SafetyAdvice:
        ntfs_bytes, ntfs_reason, ntfs_names = self._advise_ntfs(volume_bytes)
        slack_bytes, slack_reason, slack_names = self._advise_slack(file_count)
        total = max(ntfs_bytes + slack_bytes, self.floor)
        return SafetyAdvice(
            total_bytes=round_up(total, MIB),
            ntfs_bytes=ntfs_bytes,
            slack_bytes=slack_bytes,
            ntfs_reason=ntfs_reason,
            slack_reason=slack_reason,
            basis=tuple(dict.fromkeys(ntfs_names + slack_names)),
        )
