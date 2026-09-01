"""Записи измерений: схема, проверки, JSON-хранилище.

Правило схемы: в файл пишутся только измеренные величины. Всё вычислимое
(заголовок VeraCrypt, метаданные NTFS, занятое место, запас на копирование)
считается при чтении и никогда не сохраняется. В исходных ручных записях
противоречия появились именно из-за дублирования вычислимых полей.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .factory import FactorySample, factory_data
from .paths import CALIBRATION_NAME
from .sizes import SUPPORTED_FS
from .model import (
    DEFAULT_CLUSTER_BYTES,
    MIB,
    VC_HEADER_BYTES,
    CopySlackModel,
    NtfsModel,
    SafetyModel,
    ceil_div,
    round_up,
)

SCHEMA_VERSION = 5

#: Схема 1 не знала file_alloc_bytes, схема 2 — filesystem, схема 3 — ключа
#: calibration, схема 4 держала этот ключ в одном файле с записями. Все
#: читаются по-прежнему: отсутствие поля означает ровно прежнее поведение, а
#: замеры пустых томов при чтении переезжают сначала из records в calibration,
#: а оттуда — в свой файл. Пишется всегда новая версия.
SUPPORTED_SCHEMAS = (1, 2, 3, 4, 5)

#: Границы правдоподобия для метаданных NTFS. Нижняя ловит потерю разрядов
#: (в Cache 2 записано 36 573 вместо 36 573 184). Верхняя растёт вместе с
#: томом: на 100 GiB одни только $LogFile и $MFT дают под сотню мегабайт,
#: поэтому фиксированный потолок здесь давал бы ложные срабатывания.
NTFS_MIN_BYTES = MIB
NTFS_MAX_FLOOR = 128 * MIB
NTFS_MAX_SHARE = 0.02


#: Область, которую обесценивает ошибка. Запись с битым left_bytes всё ещё
#: даёт годную точку для модели NTFS, и терять её из-за этого не нужно.
SCOPE_NTFS = "ntfs"
SCOPE_SLACK = "slack"
SCOPE_BOTH = "both"


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    scope: str = SCOPE_BOTH

    def affects(self, scope: str) -> bool:
        return self.scope in (scope, SCOPE_BOTH)


@dataclass
class Record:
    id: str
    container_mib: int
    created: str = ""
    mounted_bytes: int | None = None
    empty_free_bytes: int | None = None
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES
    file_bytes: int | None = None
    file_count: int | None = None
    #: Σ ceil(size_i / cluster) × cluster, снятое обходом папки. Для одного
    #: файла выводится из file_bytes, для папки — нет: сумма логических
    #: размеров не восстанавливает поклеточное округление каждого файла.
    file_alloc_bytes: int | None = None
    left_bytes: int | None = None
    #: Файловая система тома, прочитанная при замере. Пусто — не читалась
    #: (все записи до появления проверки, и они по умолчанию считаются NTFS).
    filesystem: str = ""
    note: str = ""
    flagged: bool = False
    #: Отключённая точка калибровки: числа остаются в файле, но в расчёт идёт
    #: заводское значение. Так «сброс до заводских» ничего не теряет.
    disabled: bool = False
    #: Что пообещал расчёт, когда по нему создавали контейнер, и сколько в
    #: этом обещании было страховки. Хранятся, потому что задним числом не
    #: вычислимы: обе модели меняются от каждого нового замера, и пересчёт
    #: ответил бы «что я скажу сегодня», а не «что я сказал тогда».
    predicted_mib: int | None = None
    predicted_safety_mib: int | None = None
    #: Ключ набора файлов, если данные сгенерированы автоматическим сбором.
    #: Пусто — настоящее копирование. Отличать нужно: набор описывает машину,
    #: а не данные пользователя, и на другой машине его надо пересобрать.
    fileset: str = ""

    def __post_init__(self) -> None:
        if not self.created:
            self.created = datetime.now().isoformat(timespec="seconds")

    # --- вычислимые величины: никогда не хранятся --------------------------

    @property
    def container_bytes(self) -> int:
        return self.container_mib * MIB

    @property
    def vc_header(self) -> int | None:
        if self.mounted_bytes is None:
            return None
        return self.container_bytes - self.mounted_bytes

    @property
    def ntfs_bytes(self) -> int | None:
        if self.mounted_bytes is None or self.empty_free_bytes is None:
            return None
        return self.mounted_bytes - self.empty_free_bytes

    @property
    def consumed_bytes(self) -> int | None:
        if self.empty_free_bytes is None or self.left_bytes is None:
            return None
        return self.empty_free_bytes - self.left_bytes

    @property
    def is_calibration_point(self) -> bool:
        """Замер пустого тома: ни данных, ни остатка после копирования.

        Отдельного поля не заводится — признак выводится. Точка калибровки
        тем и определяется, что в контейнер ничего не клали.
        """
        return self.file_bytes is None and self.left_bytes is None

    @property
    def payload_alloc(self) -> int | None:
        """Сколько места занимают сами данные с учётом округления по кластерам.

        Измеренное значение имеет приоритет над выведенным: для папки из
        многих файлов round_up(Σ size_i) меньше, чем Σ round_up(size_i), и
        разница целиком уехала бы в запас на копирование.
        """
        if self.file_alloc_bytes is not None:
            return self.file_alloc_bytes
        if self.file_bytes is None:
            return None
        return round_up(self.file_bytes, self.cluster_bytes)

    @property
    def copy_slack_measured(self) -> int | None:
        consumed = self.consumed_bytes
        alloc = self.payload_alloc
        if consumed is None or alloc is None:
            return None
        return consumed - alloc

    # --- проверка прогноза постфактум --------------------------------------

    @property
    def minimum_mib(self) -> int | None:
        """Наименьший контейнер, в который эти данные всё-таки влезли бы.

        Занятое на томе — это `mounted_bytes - left_bytes`, и в него уже
        входит всё: метаданные NTFS, данные по кластерам и запас на
        копирование. Прибавить заголовок VeraCrypt и округлить вверх до MiB —
        и получится Container init, которого хватило бы впритык.

        Оценка чуть завышена: контейнер поменьше дал бы том поменьше, а на
        нём и метаданных меньше. Ошибка идёт в сторону завышения минимума, то
        есть промах выходит меньше настоящего — метрика ошибается в сторону
        тревоги, а не благодушия, и это правильная сторона.
        """
        if self.mounted_bytes is None or self.left_bytes is None:
            return None
        return ceil_div(self.mounted_bytes - self.left_bytes + VC_HEADER_BYTES, MIB)

    @property
    def miss_mib(self) -> int | None:
        """Обещано минус минимально достаточно.

        Плюс — перезаклад, ноль — попали ровно, минус — **данные не влезли
        бы**. Ради последнего случая величина и считается: занижение —
        единственная опасная сторона расчёта.
        """
        minimum = self.minimum_mib
        if self.predicted_mib is None or minimum is None:
            return None
        return self.predicted_mib - minimum

    @property
    def model_miss_mib(self) -> int | None:
        """Тот же промах за вычетом намеренного запаса.

        Без этого числа промах неоднозначен: +5 MiB одинаково выглядят и
        когда модель точна при страховке 5 MiB, и когда модель занизила на 3,
        а 8 MiB страховки это скрыли. Второе — предвестник аварии, который
        выглядит здоровым.
        """
        miss = self.miss_mib
        if miss is None:
            return None
        return miss - (self.predicted_safety_mib or 0)

    @property
    def forecast_checked(self) -> bool:
        """Есть ли что сверять: обещание записано и остаток замерен."""
        return self.miss_mib is not None

    # --- сериализация ------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "created": self.created,
            "container_mib": self.container_mib,
            "cluster_bytes": self.cluster_bytes,
        }
        for name in (
            "mounted_bytes",
            "empty_free_bytes",
            "file_bytes",
            "file_count",
            "file_alloc_bytes",
            "left_bytes",
            "predicted_mib",
            "predicted_safety_mib",
        ):
            value = getattr(self, name)
            if value is not None:
                data[name] = value
        if self.filesystem:
            data["filesystem"] = self.filesystem
        if self.fileset:
            data["fileset"] = self.fileset
        if self.note:
            data["note"] = self.note
        if self.flagged:
            data["flagged"] = True
        if self.disabled:
            data["disabled"] = True
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Record":
        return cls(
            id=str(data.get("id", "")),
            created=str(data.get("created", "")),
            container_mib=int(data["container_mib"]),
            mounted_bytes=_opt_int(data.get("mounted_bytes")),
            empty_free_bytes=_opt_int(data.get("empty_free_bytes")),
            cluster_bytes=int(data.get("cluster_bytes", DEFAULT_CLUSTER_BYTES)),
            file_bytes=_opt_int(data.get("file_bytes")),
            file_count=_opt_int(data.get("file_count")),
            file_alloc_bytes=_opt_int(data.get("file_alloc_bytes")),
            left_bytes=_opt_int(data.get("left_bytes")),
            filesystem=str(data.get("filesystem", "")),
            note=str(data.get("note", "")),
            flagged=bool(data.get("flagged", False)),
            disabled=bool(data.get("disabled", False)),
            predicted_mib=_opt_int(data.get("predicted_mib")),
            predicted_safety_mib=_opt_int(data.get("predicted_safety_mib")),
            fileset=str(data.get("fileset", "")),
        )


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def validate(record: Record) -> list[Issue]:
    """Пять проверок, каждая ловит реальный класс ошибки ручного ввода."""
    issues: list[Issue] = []

    if record.container_mib <= 0:
        issues.append(Issue("container_mib", "Размер контейнера должен быть больше нуля."))

    mounted = record.mounted_bytes
    free = record.empty_free_bytes

    if mounted is not None:
        header = record.container_bytes - mounted
        if header <= 0:
            issues.append(
                Issue(
                    "header",
                    f"Размер тома ({mounted}) не меньше размера контейнера "
                    f"({record.container_bytes}): заголовок VeraCrypt получается "
                    f"отрицательным.",
                    SCOPE_NTFS,
                )
            )
        elif header != VC_HEADER_BYTES:
            issues.append(
                Issue(
                    "header_unusual",
                    f"Заголовок VeraCrypt вышел {header} B вместо ожидаемых "
                    f"{VC_HEADER_BYTES} B. Проверьте container_mib и mounted_bytes.",
                    SCOPE_NTFS,
                )
            )

    if mounted is not None and free is not None:
        if free >= mounted:
            issues.append(
                Issue(
                    "free_ge_mounted",
                    f"Свободное место на пустом томе ({free}) не меньше его "
                    f"ёмкости ({mounted}).",
                )
            )
        else:
            ntfs = mounted - free
            ceiling = max(NTFS_MAX_FLOOR, int(mounted * NTFS_MAX_SHARE))
            if not (NTFS_MIN_BYTES <= ntfs <= ceiling):
                issues.append(
                    Issue(
                        "ntfs_range",
                        f"Метаданные NTFS вышли {ntfs} B — вне правдоподобного "
                        f"диапазона {NTFS_MIN_BYTES}..{ceiling} B. Похоже на "
                        f"потерю или лишние разряды.",
                        SCOPE_NTFS,
                    )
                )

    consumed = record.consumed_bytes
    alloc = record.payload_alloc
    if consumed is not None and alloc is not None and consumed < alloc:
        issues.append(
            Issue(
                "consumed_lt_file",
                f"Занято на томе {consumed} B, а сам файл занимает {alloc} B. "
                f"Занятое не может быть меньше файла — ошибка в одном из полей "
                f"empty_free_bytes / left_bytes / file_bytes.",
                SCOPE_SLACK,
            )
        )

    if record.filesystem and record.filesystem.upper() != SUPPORTED_FS:
        issues.append(
            Issue(
                "filesystem",
                f"Том отформатирован как {record.filesystem}, а модель "
                f"метаданных снята на {SUPPORTED_FS}. У других файловых систем "
                f"накладные расходы устроены иначе, и в калибровку такая "
                f"запись не идёт.",
                SCOPE_NTFS,
            )
        )

    alloc_measured = record.file_alloc_bytes
    if alloc_measured is not None:
        if record.file_bytes is not None and alloc_measured < record.file_bytes:
            issues.append(
                Issue(
                    "alloc_lt_logical",
                    f"Данные по кластерам ({alloc_measured}) меньше их логического "
                    f"размера ({record.file_bytes}). Округление вверх не может "
                    f"уменьшить объём.",
                    SCOPE_SLACK,
                )
            )
        if record.cluster_bytes > 0 and alloc_measured % record.cluster_bytes:
            issues.append(
                Issue(
                    "alloc_not_aligned",
                    f"Данные по кластерам ({alloc_measured}) не кратны размеру "
                    f"кластера ({record.cluster_bytes}).",
                    SCOPE_SLACK,
                )
            )

    if record.file_count is not None:
        if record.file_count < 1:
            issues.append(
                Issue(
                    "file_count",
                    "Количество файлов должно быть не меньше 1.",
                    SCOPE_SLACK,
                )
            )
        elif record.file_bytes is not None and record.file_count > record.file_bytes:
            issues.append(
                Issue(
                    "file_count_gt_bytes",
                    f"Файлов ({record.file_count}) больше, чем байт "
                    f"({record.file_bytes}).",
                    SCOPE_SLACK,
                )
            )

    return issues


def is_usable(record: Record, scope: str = SCOPE_BOTH) -> bool:
    """Годится ли запись для калибровки указанной модели.

    Проверяется не «есть ли вообще ошибки», а задевают ли они именно ту
    величину, которая берётся из записи.
    """
    if record.flagged:
        return False
    if scope == SCOPE_BOTH:
        return not validate(record)
    return not any(issue.affects(scope) for issue in validate(record))


def ntfs_points(records: Iterable[Record]) -> list[tuple[int, int]]:
    """Точки (размер тома → метаданные NTFS) для калибровки NtfsModel."""
    points = []
    for record in records:
        overhead = record.ntfs_bytes
        if overhead is not None and is_usable(record, SCOPE_NTFS):
            points.append((record.mounted_bytes, overhead))
    return points


def slack_samples(records: Iterable[Record]) -> list[tuple[int, int]]:
    """Пары (число файлов → измеренный запас) для калибровки CopySlackModel."""
    samples = []
    for record in records:
        measured = record.copy_slack_measured
        if measured is None or not is_usable(record, SCOPE_SLACK):
            continue
        samples.append((record.file_count or 1, measured))
    return samples


def build_models(
    records: Sequence[Record],
) -> tuple[NtfsModel, CopySlackModel]:
    """Собрать обе модели по накопленным записям."""
    return (
        NtfsModel(ntfs_points(records)),
        CopySlackModel.calibrate(slack_samples(records)),
    )


def build_safety(
    records: Sequence[Record],
    factory_volumes: set[int] | None = None,
) -> SafetyModel:
    """Собрать модель страховки по накопленным записям.

    Отклонения берутся из проверки исключением: только она показывает, как
    модель ведёт себя там, где точки не было. Приводит их к ширине нужного
    отрезка уже SafetyModel — сырое отклонение снято на прорехе примерно
    вдвое шире настоящей.

    За краем измеренного диапазона локальных свидетельств нет вовсе, и туда
    идёт наибольшая недооценка по всем записям — намеренно осторожно.
    """
    checks = ntfs_cross_check(records)
    deviations = [
        (check.record.mounted_bytes, check.deviation, check.record.id)
        for check in checks
        if check.record.mounted_bytes
    ]
    slack_deviations = [
        (check.record.file_count or 1, check.deviation, check.record.id)
        for check in slack_cross_check(records)
    ]
    return SafetyModel(
        ntfs=NtfsModel(ntfs_points(records)),
        ntfs_deviations=deviations,
        slack_deviations=slack_deviations,
        extrapolation_bytes=worst_shortfall(checks),
        factory_volumes=factory_volumes or set(),
    )


@dataclass(frozen=True)
class Check:
    """Насколько модель попала бы в запись, не видя её."""

    record: Record
    measured: int
    predicted: int
    calibrated: bool

    @property
    def deviation(self) -> int:
        """Положительное значение — модель занизила, то есть промахнулась вниз."""
        return self.measured - self.predicted


def ntfs_cross_check(records: Sequence[Record]) -> list[Check]:
    """Проверить модель NTFS, исключая из калибровки саму проверяемую запись.

    Без исключения проверка бессмысленна: кусочно-линейная модель проходит
    ровно через свои точки, и отклонение всегда вышло бы нулевым.
    """
    checks = []
    for index, record in enumerate(records):
        measured = record.ntfs_bytes
        if measured is None or not is_usable(record, SCOPE_NTFS):
            continue
        others = [*records[:index], *records[index + 1 :]]
        model = NtfsModel(ntfs_points(others))
        checks.append(
            Check(
                record=record,
                measured=measured,
                predicted=model.overhead(record.mounted_bytes),
                calibrated=model.calibrated,
            )
        )
    return checks


def slack_cross_check(records: Sequence[Record]) -> list[Check]:
    """То же для запаса на копирование."""
    checks = []
    for index, record in enumerate(records):
        measured = record.copy_slack_measured
        if measured is None or not is_usable(record, SCOPE_SLACK):
            continue
        others = [*records[:index], *records[index + 1 :]]
        model = CopySlackModel.calibrate(slack_samples(others))
        checks.append(
            Check(
                record=record,
                measured=measured,
                predicted=model.slack(record.file_count or 1),
                calibrated=model.calibrated,
            )
        )
    return checks


def worst_shortfall(checks: Sequence[Check]) -> int:
    """Наибольшая недооценка. Ноль означает, что модель нигде не занизила."""
    return max((check.deviation for check in checks), default=0)


def slack_key(record: Record) -> str:
    """Чем один замер запаса отличается от другого — и что кого вытесняет.

    Ключ — набор, а не число файлов. Числом файлов было нельзя: два набора с
    `n = 1` заведены нарочно разными по объёму (64 MiB и 4 GiB), чтобы
    сравнить их друг с другом и проверить, что запас от размера файлов не
    зависит. Ключ по `n` делал их взаимоисключающими — на первом настоящем
    прогоне второй молча съел первый вместе с его точкой NTFS, то есть
    правило вытеснения уничтожало ровно ту сверку, ради которой оба набора и
    существуют.

    У снятого руками замера набора нет, и он по-прежнему опознаётся числом
    файлов: два ручных замера на одном `n` — это два замера одного и того же.
    """
    return record.fileset or f"n={record.file_count}"


def factory_slack_record(sample: FactorySample) -> Record:
    """Заводской замер запаса как запись. Числа те же, что и в файле.

    Такая запись годится сразу двум моделям: пустой том у неё замерен, значит
    она ещё и точка NTFS. Так и задумано — замер снимался на настоящем томе,
    и терять его метаданные было бы расточительством.
    """
    title = sample.title or sample.fileset
    return Record(
        id=f"Заводской запас {title}".strip(),
        created="",
        container_mib=sample.container_mib,
        cluster_bytes=sample.cluster_bytes,
        mounted_bytes=sample.mounted_bytes,
        empty_free_bytes=sample.empty_free_bytes,
        file_bytes=sample.file_bytes,
        file_count=sample.file_count,
        file_alloc_bytes=sample.file_alloc_bytes,
        left_bytes=sample.left_bytes,
        fileset=sample.fileset,
    )


@dataclass(frozen=True)
class Forecast:
    """Сводка по проверке прогноза постфактум.

    Считается по записям, у которых есть и обещание расчёта, и замеренный
    остаток. Всё остальное проверять не на чем.
    """

    checked: int
    worst_miss: int
    worst_model_miss: int
    #: Записи, где обещанного контейнера не хватило бы. Пусто — расчёт ни
    #: разу не занизил, и это главное, что нужно знать.
    short: tuple[str, ...]

    @property
    def any_short(self) -> bool:
        return bool(self.short)


def forecast(records: Iterable[Record]) -> Forecast:
    """Как расчёт справился на тех записях, где его есть с чем сверить."""
    checked = [record for record in records if record.forecast_checked]
    if not checked:
        return Forecast(0, 0, 0, ())
    return Forecast(
        checked=len(checked),
        worst_miss=min(record.miss_mib for record in checked),
        worst_model_miss=min(record.model_miss_mib for record in checked),
        short=tuple(record.id for record in checked if record.miss_mib < 0),
    )


class StoreError(Exception):
    """Хранилище недоступно или повреждено."""


def calibration_file_for(records_file: str | os.PathLike[str]) -> Path:
    """Файл замеров рядом с файлом записей, с постоянным именем.

    Имя постоянное, а не производное от имени записей: замеры описывают
    машину, а не набор записей, и два файла записей в одной папке должны
    делить одну калибровку, а не разводить две копии одних и тех же чисел.
    """
    return Path(records_file).with_name(CALIBRATION_NAME)


def _read_json(path: Path) -> dict[str, Any]:
    """Прочитать файл и проверить версию схемы. Отсутствие файла — не ошибка."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StoreError(
            f"Файл {path} повреждён и не читается как JSON: {exc}. "
            f"Резервная копия: {path.with_suffix(path.suffix + '.bak')}"
        ) from exc
    except OSError as exc:
        raise StoreError(f"Не удалось прочитать {path}: {exc}") from exc

    version = raw.get("schema")
    if version not in SUPPORTED_SCHEMAS:
        supported = ", ".join(str(item) for item in SUPPORTED_SCHEMAS)
        raise StoreError(
            f"Версия схемы {version!r} не поддерживается, ожидается одна из: {supported}."
        )
    return raw


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Атомарная запись с одной резервной копией .bak."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise StoreError(f"Не удалось записать {path}: {exc}") from exc


@dataclass
class Store:
    """Два JSON-файла: записи о копировании и замеры пустых томов.

    Раздельно, потому что это разные величины с разным сроком жизни. Записи
    о копировании описывают данные и переезжают вместе с ними; замеры
    описывают машину — сборку Windows и версию VeraCrypt — и на другой
    машине неверны. В одном файле их приходилось возить вместе, и чужая
    калибровка приезжала под видом своей.

    Обоими файлами владеет одно хранилище: расчёт стоит на них вместе, и
    читать их порознь было бы негде.
    """

    path: Path
    records: list[Record] = field(default_factory=list)
    calibration: list[Record] = field(default_factory=list)
    #: Куда легли замеры. Пусто — соседний файл с постоянным именем.
    calibration_path: Path | None = None

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.calibration_path is None:
            self.calibration_path = calibration_file_for(self.path)
        else:
            self.calibration_path = Path(self.calibration_path)
        #: Замеры приехали из старого однофайлового хранилища и ещё не
        #: записаны на своё место. Пока это так, старый файл держит их копию.
        self.migrated = False

    @property
    def backup_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".bak")

    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str],
        calibration_path: str | os.PathLike[str] | None = None,
    ) -> "Store":
        path = Path(path)
        points_path = (
            Path(calibration_path)
            if calibration_path is not None
            else calibration_file_for(path)
        )

        raw = _read_json(path)
        version = int(raw.get("schema", SCHEMA_VERSION))
        records = [Record.from_json(item) for item in raw.get("records", [])]

        # Замеры из старого однофайлового хранилища. До четвёртой схемы они
        # лежали вперемешку с записями, в четвёртой — своим ключом того же
        # файла. Числа не меняются, меняется только место.
        legacy = [Record.from_json(item) for item in raw.get("calibration", [])]
        if version < 4:
            legacy.extend(record for record in records if record.is_calibration_point)
            records = [record for record in records if not record.is_calibration_point]

        points = [
            Record.from_json(item)
            for item in _read_json(points_path).get("calibration", [])
        ]
        # Свой файл замеров старше приехавшего: если пользователь уже снял
        # точку на этом томе, старая копия из файла записей её не вытесняет.
        covered = {record.mounted_bytes for record in points}
        points.extend(
            record for record in legacy if record.mounted_bytes not in covered
        )

        store = cls(
            path=path,
            records=records,
            calibration=points,
            calibration_path=points_path,
        )
        store.migrated = bool(legacy)
        return store

    def save(self) -> None:
        _write_json(
            self.path,
            {
                "schema": SCHEMA_VERSION,
                "records": [record.to_json() for record in self.records],
            },
        )
        # Пустой файл замеров не заводится: на новой машине своих точек нет
        # вовсе, и класть рядом пустышку незачем. Опустевший — переписывается,
        # иначе удалённые замеры вернулись бы при следующем чтении.
        if self.calibration or self.calibration_path.exists():
            _write_json(
                self.calibration_path,
                {
                    "schema": SCHEMA_VERSION,
                    "calibration": [record.to_json() for record in self.calibration],
                },
            )
        self.migrated = False

    def calibration_records(self) -> list[Record]:
        """Точки, участвующие в модели: свои включённые плюс заводские.

        Свой замер вытесняет заводской на том же размере тома. Не «большее из
        двух», как в NtfsModel._dedupe: заводское значение снято на чужой
        машине, и меньшее собственное вернее любого чужого.
        """
        own = [record for record in self.calibration if not record.disabled]
        covered = {record.mounted_bytes for record in own if record.mounted_bytes}
        covered.update(
            record.mounted_bytes for record in self.records if record.mounted_bytes
        )

        filled = list(own)
        for point in factory_data().points:
            if point.mounted_bytes in covered:
                continue
            filled.append(
                Record(
                    id=f"Заводская {point.container_mib} MiB",
                    created="",
                    container_mib=point.container_mib,
                    cluster_bytes=point.cluster_bytes,
                    mounted_bytes=point.mounted_bytes,
                    empty_free_bytes=point.empty_free_bytes,
                )
            )

        # Заводские замеры запаса вытесняются своими по числу файлов, а не по
        # размеру тома: запас зависит от n, и своя точка на том же n вернее
        # чужой ровно по той же причине, что и с метаданными.
        counts = {
            record.file_count
            for record in [*self.records, *own]
            if record.file_count and record.copy_slack_measured is not None
        }
        for sample in factory_data().samples:
            if sample.file_count in counts:
                continue
            filled.append(factory_slack_record(sample))
        return filled

    def all_for_model(self) -> list[Record]:
        """Всё, на чём строится модель: записи о копировании и точки."""
        return [*self.records, *self.calibration_records()]

    def calibration_points(self) -> list[Record]:
        """Свои замеры пустых томов — то, что показывает таблица покрытия."""
        return [record for record in self.calibration if record.is_calibration_point]

    def slack_measurements(self) -> list[Record]:
        """Свои замеры запаса на копирование.

        Живут в том же файле, что и точки калибровки: обе величины описывают
        машину, а не данные, и на другой машине одинаково неверны. Отдельного
        поля для различения не заведено — признак выводится, как и всё
        остальное вычислимое: у замера запаса есть и данные, и остаток,
        поэтому точкой калибровки он не считается.
        """
        return [
            record for record in self.calibration if not record.is_calibration_point
        ]

    def put_calibration(self, record: Record) -> None:
        """Положить замер, вытеснив прежний того же рода.

        Вытеснение раздельное: точка калибровки заменяет точку на том же
        томе, замер запаса — замер того же набора. Общий ключ по
        `mounted_bytes` выбивал бы точку NTFS замером запаса, снятым на том
        же размере тома, и наоборот — молча, потому что обе записи выглядят
        одинаково законно.
        """
        if record.is_calibration_point:
            keep = [
                item
                for item in self.calibration
                if not item.is_calibration_point
                or item.mounted_bytes != record.mounted_bytes
            ]
        else:
            key = slack_key(record)
            keep = [
                item
                for item in self.calibration
                if item.is_calibration_point or slack_key(item) != key
            ]
        self.calibration = [*keep, record]

    def factory_volumes(self) -> set[int]:
        """Размеры томов, покрытые только заводскими данными.

        Замеры запаса тоже дают точку NTFS — пустой том меряется до записи
        файлов, — поэтому заводские среди них считаются здесь наравне с
        точками: отрезок, оба конца которого чужие, обязан получить надбавку
        независимо от того, каким сбором чужие числа сняты.
        """
        own = {
            record.mounted_bytes
            for record in [*self.records, *self.calibration]
            if record.mounted_bytes and not record.disabled
        }
        volumes = {point.mounted_bytes for point in factory_data().points}
        volumes.update(sample.mounted_bytes for sample in factory_data().samples)
        return {volume for volume in volumes if volume not in own}

    def models(self) -> tuple[NtfsModel, CopySlackModel]:
        return build_models(self.all_for_model())

    def safety(self) -> SafetyModel:
        return build_safety(self.all_for_model(), self.factory_volumes())

    def add(self, record: Record) -> None:
        self.records.append(record)

    def replace_at(self, index: int, record: Record) -> None:
        self.records[index] = record

    def index_of(self, record: Record) -> int | None:
        """Где лежит эта самая запись. По тождеству, а не по равенству.

        Окна правки немодальны, и пока одно из них открыто, список успевает
        измениться: соседнюю запись удалили, новую добавили — номер, взятый
        при открытии, показывает уже на чужую строку. Равенство здесь тоже не
        годится: две записи с одинаковыми полями — обычное дело, `Record`
        сравнивается по значениям, и правка ушла бы в первую попавшуюся.

        None — записи в хранилище больше нет: её удалили из другого окна.
        """
        for index, item in enumerate(self.records):
            if item is record:
                return index
        return None

    def remove_at(self, index: int) -> None:
        del self.records[index]

    def measure_into(self, record: Record, total: int, free: int) -> Record:
        """Разложить один замер тома по полям записи.

        Пустой том даёт ёмкость и свободное место; тот же замер после
        копирования даёт остаток. Какое из полей заполняется, определяется
        тем, заполнено ли уже empty_free_bytes.
        """
        if record.empty_free_bytes is None:
            return replace(record, mounted_bytes=total, empty_free_bytes=free)
        return replace(record, left_bytes=free)
