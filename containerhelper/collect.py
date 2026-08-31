"""Автоматический сбор замеров: план, один шаг, самопроверка.

Снять калибровку руками — это двадцать два контейнера: создать, отформатировать,
смонтировать, записать числа, размонтировать, удалить. Несколько часов, и
каждый шаг можно сделать неправильно молча. Здесь то же самое делает VeraCrypt
по команде, а программа читает готовый том.

Собирается два рода замеров. Пустой том даёт метаданные NTFS. Тот же том,
заполненный сгенерированным набором файлов, даёт запас на копирование — ту
самую часть модели, которую до сих пор не подтверждало ни одно измерение при
нескольких разных `n`. Второй род дороже: он пишет на том настоящие гигабайты,
и место на диске приходится считать заранее.

Без Qt: последовательность шагов и разбор их результатов — не отображение, и
проверяться должны без интерфейса. Прогресс и отмена живут в диалоге, который
крутит эти шаги по одному.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Sequence

from .fileset import PAYLOAD_DIR, FileSet, generate
from .formatting import fmt_both, size_label
from .model import (
    DEFAULT_CLUSTER_BYTES,
    DEFAULT_SAFETY_BYTES,
    MIB,
    VC_HEADER_BYTES,
    CopySlackModel,
    NtfsModel,
    SafetyModel,
    ceil_div,
    solve_container_mib,
)
from .records import Record
from .sizes import scan_paths
from .veracrypt import (
    VeraCrypt,
    VeraCryptError,
    container_name,
    orphans,
    remove_container,
)

#: На каком размере идёт самопроверка. Гигабайт выбран потому, что полное
#: форматирование гигабайта стоит секунды, а терабайта — часы.
SELF_CHECK_MIB = 1024

#: Насколько может разойтись динамический контейнер с быстрым форматированием
#: и обычный с полным, чтобы считать их равными. На перекрывающихся размерах
#: расхождение было 8 KiB на 12 GiB и 3 KiB на 80 GiB — мегабайт с запасом
#: покрывает эту рябь и всё ещё ловит настоящую разницу, ради которой
#: самопроверка и делается.
SELF_CHECK_TOLERANCE = MIB

#: Насколько контейнер под набор файлов делается больше обещанного расчётом.
#: Обещание записывается как есть — на нём и держится проверка прогноза, — но
#: создавать контейнер ровно по нему нельзя: промахнись модель вниз, набор не
#: влезет, и вместо замера выйдет неудача. Подушка не искажает ничего:
#: измеряемый запас от размера тома не зависит, а метаданные пустого тома
#: меряются на том же самом томе.
SLACK_CUSHION_MIB = 64

#: Постоянная и долевая части запаса по свободному месту хоста. Меньшего не
#: хватает: файловая система хоста тоже растёт, пока в неё пишут, а
#: динамический контейнер на кончившемся диске рвётся посреди записи.
SPACE_MARGIN_BYTES = 64 * MIB
SPACE_MARGIN_PERCENT = 5

#: Ниже этого свободного места на хосте запись прекращается. Отдельно от
#: margin: тот закладывается до начала шага, а этот срабатывает, если место
#: съел кто-то посторонний уже по ходу.
SPACE_FLOOR_BYTES = 128 * MIB

#: Во сколько байт записи обходится создание одного файла — грубая
#: равнозначность, и только для полосы прогресса.
#:
#: Нужна потому, что время шага держится не на одних байтах. Десять тысяч
#: килобайтных файлов занимают 39 MiB, а создаются заметно дольше, чем эти
#: 39 MiB пишутся: цена там в записях MFT и в индексе каталога, а не в объёме.
#: Без поправки полоса проскакивала бы такой набор мгновенно и потом стояла на
#: нём, пока он идёт.
#:
#: На требуемое место эта величина не влияет **никогда**: там считаются
#: настоящие байты, и приписывать диску лишнее значило бы зря пропускать шаги.
FILE_WEIGHT_BYTES = 64 * 1024


class OutOfSpace(OSError):
    """Место на диске кончилось или кончается. Наследник OSError намеренно.

    Шаг ловит `OSError` и без того — дисковые отказы приходят именно им, — и
    заводить для нехватки места отдельную ветку значило бы разойтись с тем,
    как эта же беда приходит от самой записи.
    """


class Stopped(Exception):
    """Отмена, замеченная посреди записи набора.

    Между шагами отмена срабатывает сама, но запись четырёх гигабайт — это
    один шаг длиной в минуты, и ждать его конца, чтобы услышать «стоп»,
    незачем: неоконченный набор всё равно выбрасывается вместе с контейнером.
    """


# --- ход одного шага -------------------------------------------------------
#
# Фазы названы словами, а не номерами: они уходят прямо в строку под
# прогрессом, и человеку надо видеть, на чём именно программа стоит третью
# минуту.

PHASE_CREATE = "создание контейнера"
PHASE_MOUNT = "монтирование"
PHASE_EMPTY = "замер пустого тома"
PHASE_WRITE = "запись файлов"
PHASE_VERIFY = "сверка содержимого"
PHASE_REMOUNT = "перемонтирование"
PHASE_LEFT = "замер остатка"
PHASE_CLEANUP = "уборка"


@dataclass(frozen=True)
class Progress:
    """Что происходит внутри шага прямо сейчас.

    Байты считаются те, что предстоит записать на том, — по ним и меряется
    прогресс. Для замера пустого тома их нет вовсе, и шаг вносит в общий счёт
    свою оценку целиком, когда кончится.
    """

    phase: str
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0

    @property
    def detail(self) -> str:
        """Подробность к названию фазы. Пусто — сказать нечего."""
        if self.phase != PHASE_WRITE or not self.files_total:
            return ""
        return f"{self.files_done} из {self.files_total}"

    @property
    def share(self) -> float:
        """Какая доля шага пройдена — тем же весом, что и весь план.

        Байты и файлы складываются вместе, потому что порознь врут оба:
        полоса по байтам стоит на наборе из мелких файлов, полоса по файлам —
        на одном большом. Вне записи доля нулевая: сколько байт VeraCrypt уже
        уложил при создании контейнера, снаружи не видно, и додумывать это
        полосой не стоит — фаза названа словом.
        """
        done = self.bytes_done + FILE_WEIGHT_BYTES * self.files_done
        total = self.bytes_total + FILE_WEIGHT_BYTES * self.files_total
        return min(done / total, 1.0) if total else 0.0


@dataclass(frozen=True)
class Step:
    """Один контейнер: создать, смонтировать, замерить, убрать за собой."""

    container_mib: int
    dynamic: bool = True
    quick: bool = True
    self_check: bool = False
    #: Набор файлов, который надо создать на томе. None — замер пустого тома,
    #: то есть точка для модели метаданных NTFS.
    fileset: FileSet | None = None
    #: Что пообещал расчёт на этом наборе и сколько в обещании было страховки.
    #: Ноль — обещания не было (замер пустого тома проверять не с чем).
    predicted_mib: int = 0
    predicted_safety_mib: int = 0

    @property
    def key(self) -> str:
        return self.fileset.key if self.fileset is not None else ""

    @property
    def title(self) -> str:
        if self.fileset is not None:
            return f"{self.fileset.title} — контейнер {size_label(self.container_mib)}"
        method = (
            "динамический, быстрое форматирование"
            if self.dynamic and self.quick
            else "обычный, полное форматирование"
        )
        return f"{size_label(self.container_mib)} — {method}"

    def payload_bytes(self, cluster_bytes: int = DEFAULT_CLUSTER_BYTES) -> int:
        return 0 if self.fileset is None else self.fileset.alloc_bytes(cluster_bytes)


@dataclass(frozen=True)
class Measurement:
    """Что дал смонтированный том.

    Поля набора заполняются только у замера запаса. У замера пустого тома их
    нет, и запись из него выходит точкой калибровки — по тому же признаку, по
    которому её узнаёт Record: ни данных, ни остатка.
    """

    container_mib: int
    mounted_bytes: int
    empty_free_bytes: int
    cluster_bytes: int
    filesystem: str
    fileset: str = ""
    fileset_title: str = ""
    file_bytes: int | None = None
    file_count: int | None = None
    file_alloc_bytes: int | None = None
    left_bytes: int | None = None
    predicted_mib: int = 0
    predicted_safety_mib: int = 0

    @property
    def ntfs_bytes(self) -> int:
        return self.mounted_bytes - self.empty_free_bytes

    @property
    def copy_slack_bytes(self) -> int | None:
        if self.left_bytes is None or self.file_alloc_bytes is None:
            return None
        return (self.empty_free_bytes - self.left_bytes) - self.file_alloc_bytes

    def as_record(self, note: str = "") -> Record:
        """Замер как запись. Вычислимое не пишется, как и везде."""
        title = (
            f"Запас {self.fileset_title}"
            if self.fileset
            else f"Калибровка {size_label(self.container_mib)}"
        )
        return Record(
            id=title,
            container_mib=self.container_mib,
            cluster_bytes=self.cluster_bytes,
            mounted_bytes=self.mounted_bytes,
            empty_free_bytes=self.empty_free_bytes,
            file_bytes=self.file_bytes,
            file_count=self.file_count,
            file_alloc_bytes=self.file_alloc_bytes,
            left_bytes=self.left_bytes,
            filesystem=self.filesystem,
            note=note,
            predicted_mib=self.predicted_mib or None,
            predicted_safety_mib=self.predicted_safety_mib or None,
            fileset=self.fileset,
        )


@dataclass(frozen=True)
class StepResult:
    step: Step
    measurement: Measurement | None = None
    error: str = ""
    #: Дальше идти нельзя: сорвалась самопроверка или её вердикт не сошёлся.
    fatal: bool = False
    #: Шаг не выполнялся: на диске не хватило места. Не неудача — его можно
    #: доснять позже, когда место освободится.
    skipped: bool = False
    required_bytes: int = 0
    free_bytes: int = 0

    @property
    def ok(self) -> bool:
        return self.measurement is not None and not self.error


# --- план ------------------------------------------------------------------


def plan(
    sizes: Sequence[int],
    covered: Sequence[int] = (),
    self_check: bool = True,
) -> list[Step]:
    """Порядок замеров пустых томов: самопроверка первым делом, затем размеры.

    Самопроверка — это тот же гигабайт дважды: динамическим с быстрым
    форматированием и обычным с полным. Совпало — остальное можно гнать
    динамическими, и терабайт не потребует терабайта свободного места.
    Разошлось — считать по динамическим на этой машине нельзя, и узнать это
    надо на секундах, а не после трёх часов работы.

    `covered` — размеры контейнеров, на которых свой замер уже есть; они
    пропускаются, чтобы «добить недостающее» не пересняло всё заново.
    """
    steps: list[Step] = []
    if self_check:
        steps.append(Step(SELF_CHECK_MIB, dynamic=True, quick=True, self_check=True))
        steps.append(Step(SELF_CHECK_MIB, dynamic=False, quick=False, self_check=True))

    done = set(covered)
    for size in sizes:
        if size in done:
            continue
        if self_check and size == SELF_CHECK_MIB:
            # Гигабайт уже снят самопроверкой, причём дважды.
            continue
        steps.append(Step(size))
    return steps


def slack_step(
    fileset: FileSet,
    ntfs: NtfsModel | None = None,
    slack: CopySlackModel | None = None,
    safety: SafetyModel | None = None,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
    forbidden: Sequence[int] = (),
) -> Step:
    """Шаг под один набор: что расчёт обещает и какой контейнер создавать.

    Обещание считается ровно так же, как на вкладке «Расчёт», включая подбор
    страховки: иначе проверка прогноза проверяла бы не ту величину, которую
    программа показывает пользователю. Совет по страховке зависит от размера
    тома, а том — от страховки, поэтому сначала решаем с умолчанием, потом
    уточняем; второй проход не нужен, страховка двигает том на единицы MiB.

    Контейнер создаётся крупнее обещанного на подушку — см.
    SLACK_CUSHION_MIB. И ещё на мегабайт, если размер совпал с одним из
    рекомендованных: замер запаса не должен садиться на строку таблицы
    покрытия, иначе кнопка «К заводскому» в той строке отключала бы его
    заодно с точкой NTFS.
    """
    ntfs = ntfs or NtfsModel()
    slack = slack or CopySlackModel()
    payload = fileset.payload(cluster_bytes)

    safety_bytes = DEFAULT_SAFETY_BYTES
    safety_mib = ceil_div(safety_bytes, MIB)
    if safety is not None:
        probe = solve_container_mib(
            payload, ntfs=ntfs, slack=slack, safety_bytes=safety_bytes
        )
        advice = safety.advise(probe.volume_bytes, payload.file_count)
        safety_bytes, safety_mib = advice.total_bytes, advice.total_mib

    predicted = solve_container_mib(
        payload, ntfs=ntfs, slack=slack, safety_bytes=safety_bytes
    )
    taken = set(forbidden)
    container_mib = predicted.container_mib + SLACK_CUSHION_MIB
    while container_mib in taken:
        container_mib += 1

    return Step(
        container_mib=container_mib,
        fileset=fileset,
        predicted_mib=predicted.container_mib,
        predicted_safety_mib=safety_mib,
    )


def slack_plan(
    filesets: Sequence[FileSet],
    ntfs: NtfsModel | None = None,
    slack: CopySlackModel | None = None,
    safety: SafetyModel | None = None,
    covered: Sequence[str] = (),
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
    forbidden: Sequence[int] = (),
) -> list[Step]:
    """Шаги замера запаса, от дешёвых по объёму к дорогим.

    Порядок по объёму, а не по списку: прожорливые наборы упираются в
    свободное место чаще всех, и если поставить их первыми, одна нехватка
    отменила бы всё, что прекрасно поместилось бы после.

    `covered` — ключи наборов, на которых свой замер уже есть. Ключами, а не
    числами файлов: два набора с `n = 1` отличаются объёмом и снимаются оба,
    иначе сверка «запас от размера файлов не зависит» становится невозможной.
    """
    done = set(covered)
    chosen = [item for item in filesets if item.key not in done]
    chosen.sort(key=lambda item: item.alloc_bytes(cluster_bytes))
    return [
        slack_step(item, ntfs, slack, safety, cluster_bytes, forbidden)
        for item in chosen
    ]


def disk_bytes(
    step: Step,
    ntfs: NtfsModel | None = None,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> int:
    """Сколько байт шаг на самом деле уложит на диск.

    Динамический контейнер ложится на диск только записанными кластерами, а
    записаны на пустом томе одни метаданные — поэтому терабайтный замер стоит
    не терабайта, а примерно 136 MiB. Предсказывает их та самая модель,
    которую сбор и калибрует: внутри покрытого замерами диапазона она точна
    до единиц мегабайт.

    Обычный контейнер с полным форматированием материализуется целиком —
    таков во всём сборе ровно один шаг, второй контейнер самопроверки.
    """
    ntfs = ntfs or NtfsModel()
    if not (step.dynamic and step.quick):
        return step.container_mib * MIB
    volume = step.container_mib * MIB - VC_HEADER_BYTES
    return VC_HEADER_BYTES + ntfs.overhead(volume) + step.payload_bytes(cluster_bytes)


def required_bytes(
    step: Step,
    ntfs: NtfsModel | None = None,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> int:
    """Сколько свободного места шаг требует, чтобы за него можно было браться.

    Уложенное на диск плюс запас: оценка метаданных не точна до байта, а
    файловая система хоста тоже прирастает, пока в неё пишут. Поправка на
    создание файлов сюда не входит — она про время, а не про место.
    """
    need = disk_bytes(step, ntfs, cluster_bytes)
    return need + max(SPACE_MARGIN_BYTES, need * SPACE_MARGIN_PERCENT // 100)


def weight_bytes(
    step: Step,
    ntfs: NtfsModel | None = None,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> int:
    """Во что шаг обходится по времени, выраженное в байтах записи.

    То же, что ляжет на диск, плюс поправка на создание каждого файла: время
    держится не на одних байтах, и без неё набор из десяти тысяч мелких
    файлов весил бы 39 MiB, а шёл бы дольше, чем полгигабайта крупных.
    """
    files = step.fileset.file_count if step.fileset is not None else 0
    return disk_bytes(step, ntfs, cluster_bytes) + FILE_WEIGHT_BYTES * files


def total_bytes(
    steps: Sequence[Step],
    ntfs: NtfsModel | None = None,
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES,
) -> int:
    """Вес всего плана — по нему и меряется общий прогресс.

    Не числом шагов: терабайтный пустой том снимается за секунды, а набор в
    четыре гигабайта пишется минутами, и полоса, ползущая равными долями,
    врала бы в разы. Запас на место сюда не входит: он про осторожность, а не
    про работу.
    """
    return sum(weight_bytes(step, ntfs, cluster_bytes) for step in steps)


def self_check_verdict(fast: Measurement, slow: Measurement) -> str:
    """Пусто — эквивалентность подтверждена. Иначе текст, объясняющий отказ."""
    difference = abs(fast.ntfs_bytes - slow.ntfs_bytes)
    if difference <= SELF_CHECK_TOLERANCE:
        return ""
    return (
        f"Самопроверка не сошлась: динамический контейнер с быстрым "
        f"форматированием дал {fast.ntfs_bytes} B метаданных, обычный с полным "
        f"— {slow.ntfs_bytes} B, разница {difference} B. На этой машине "
        f"динамические контейнеры меряются иначе, и гнать по ним остальные "
        f"размеры нельзя: числа получились бы не про те контейнеры, которые "
        f"будут созданы на самом деле."
    )


# --- один шаг --------------------------------------------------------------


def measure(
    veracrypt: VeraCrypt,
    workdir: Path,
    step: Step,
    progress: Callable[[Progress], None] | None = None,
    check: Callable[[int], None] | None = None,
) -> Measurement:
    """Создать контейнер, снять с него числа и убрать за собой.

    Уборка в finally и без исключений: смонтированный том и файл-контейнер
    обязаны сниматься даже при падении, иначе терабайтные файлы копятся молча.
    """
    path = Path(workdir) / container_name(step.container_mib, step.key)
    letter = veracrypt.free_letter()

    def say(phase: str, **extra) -> None:
        if progress is not None:
            progress(Progress(phase, **extra))

    try:
        say(PHASE_CREATE)
        veracrypt.create(
            path, step.container_mib * MIB, dynamic=step.dynamic, quick=step.quick
        )
        say(PHASE_MOUNT)
        veracrypt.mount(path, letter)
        say(PHASE_EMPTY)
        total, free = veracrypt.volumes.usage(letter)
        empty = Measurement(
            container_mib=step.container_mib,
            mounted_bytes=total,
            empty_free_bytes=free,
            cluster_bytes=veracrypt.volumes.cluster(letter) or 0,
            filesystem=veracrypt.volumes.filesystem(letter),
        )
        if step.fileset is None:
            return empty
        return _measure_slack(veracrypt, path, letter, step, empty, say, check)
    finally:
        say(PHASE_CLEANUP)
        veracrypt.unmount_quietly(letter)
        remove_container(path)


def _measure_slack(
    veracrypt: VeraCrypt,
    path: Path,
    letter: str,
    step: Step,
    empty: Measurement,
    say: Callable[..., None],
    check: Callable[[int], None] | None,
) -> Measurement:
    """Заполнить том набором и снять остаток.

    Остаток снимается на свежесмонтированном томе, а не сразу после записи.
    Модель предсказывает именно Left space — то, что VeraCrypt покажет
    человеку, когда тот смонтирует контейнер со своими данными, — и мерить
    надо ровно это состояние, со всеми дописанными метаданными.
    """
    fileset = step.fileset
    cluster = empty.cluster_bytes or DEFAULT_CLUSTER_BYTES
    payload = fileset.payload(cluster)

    # Кластер тома читается, а не предполагается, и на нестандартном размер
    # набора может оказаться совсем не тем, под который считался контейнер:
    # десять тысяч килобайтных файлов при кластере 65536 занимают не 39 MiB,
    # а 625. Лучше сказать об этом, чем оборвать запись на середине.
    if payload.alloc_bytes >= empty.empty_free_bytes:
        raise VeraCryptError(
            f"Набор «{fileset.title}» занимает {fmt_both(payload.alloc_bytes)} "
            f"при кластере {cluster} B, а на томе свободно "
            f"{fmt_both(empty.empty_free_bytes)}. Замер не начат."
        )

    root = Path(veracrypt.volumes.root(letter)) / PAYLOAD_DIR

    # Логический объём, а не кластерный: generate считает записанные байты, и
    # знаменатель обязан быть в тех же единицах. На пятистах килобайтных
    # файлах кластерный объём вдвадцатеро больше логического, и доля шага
    # упёрлась бы в пять процентов.
    written_total = fileset.logical_bytes

    def on_progress(files_done: int, bytes_done: int) -> None:
        say(
            PHASE_WRITE,
            files_done=files_done,
            files_total=payload.file_count,
            bytes_done=bytes_done,
            bytes_total=written_total,
        )

    say(PHASE_WRITE, files_total=payload.file_count, bytes_total=written_total)
    generate(root, fileset, on_progress=on_progress, check=check)

    # Сверка обходом: сгенерированное могло лечь не так, как задумано —
    # мелкий файл, уместившийся прямо в запись MFT, кластера не получает, и
    # ожидаемый объём по кластерам тогда завышен, а измеренный запас уходит в
    # минус. Ловить это надо здесь, а не разбираться потом с отрицательным
    # числом в таблице.
    say(PHASE_VERIFY)
    scan = scan_paths([root], cluster)
    if scan.file_count != payload.file_count:
        raise VeraCryptError(
            f"На томе оказалось файлов {scan.file_count}, а набор «"
            f"{fileset.title}» состоит из {payload.file_count}. Замер "
            f"негоден."
        )

    say(PHASE_REMOUNT)
    veracrypt.unmount(letter)
    veracrypt.mount(path, letter)
    say(PHASE_LEFT)
    _total, left = veracrypt.volumes.usage(letter)

    return replace(
        empty,
        fileset=fileset.key,
        fileset_title=fileset.title,
        file_bytes=scan.logical_bytes,
        file_count=scan.file_count,
        file_alloc_bytes=scan.payload.alloc_bytes,
        left_bytes=left,
        predicted_mib=step.predicted_mib,
        predicted_safety_mib=step.predicted_safety_mib,
    )


# --- весь сбор -------------------------------------------------------------


def free_space(workdir: str | Path) -> int:
    """Свободное место в рабочей папке. Недоступна — ноль, то есть «не влезет»."""
    try:
        return shutil.disk_usage(Path(workdir)).free
    except OSError:
        return 0


@dataclass
class Collector:
    """Крутит шаги по одному и следит за самопроверкой.

    По одному, а не всё разом: диалогу надо показывать прогресс и слышать
    отмену, а замер каждого размера сохраняется сразу — сбор идёт часами, и
    падение посередине не должно стоить всего, что уже снято.
    """

    veracrypt: VeraCrypt
    workdir: Path
    steps: list[Step] = field(default_factory=list)
    #: Модель метаданных: ею оценивается место, которое займёт шаг.
    ntfs: NtfsModel = field(default_factory=NtfsModel)
    cluster_bytes: int = DEFAULT_CLUSTER_BYTES
    #: Свободное место хоста. Подменяется в тестах — настоящий диск на них
    #: то полон, то пуст, и проверять по нему нечего.
    free_bytes: Callable[[], int] | None = None
    progress: Callable[[Progress], None] | None = None
    #: Прервать текущий шаг. Возвращает True — надо остановиться. Спрашивается
    #: по ходу записи набора: четыре гигабайта пишутся минутами.
    should_stop: Callable[[], bool] | None = None
    #: Что дала самопроверка. Копится до пары, потом выносится вердикт.
    _checks: list[Measurement] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir)
        if self.free_bytes is None:
            self.free_bytes = lambda: free_space(self.workdir)

    def prepare(self) -> list[Path]:
        """Убрать контейнеры, оставшиеся от прерванного сбора."""
        removed = []
        for path in orphans(self.workdir):
            if remove_container(path):
                removed.append(path)
        return removed

    def required(self, step: Step) -> int:
        return required_bytes(step, self.ntfs, self.cluster_bytes)

    def step_weight(self, step: Step) -> int:
        return weight_bytes(step, self.ntfs, self.cluster_bytes)

    def weight(self) -> int:
        """Вес всего плана в байтах — знаменатель для полосы прогресса."""
        return total_bytes(self.steps, self.ntfs, self.cluster_bytes)

    def _guard(self, _bytes_done: int) -> None:
        """Что проверяется по ходу записи: отмена и остаток места на диске."""
        if self.should_stop is not None and self.should_stop():
            raise Stopped()
        if self.free_bytes() < SPACE_FLOOR_BYTES:
            raise OutOfSpace(
                f"На диске осталось меньше {fmt_both(SPACE_FLOOR_BYTES)} — "
                f"запись остановлена, чтобы не порвать том на середине."
            )

    def run_step(self, step: Step) -> StepResult:
        need = self.required(step)
        free = self.free_bytes()
        if free < need:
            return StepResult(
                step,
                error=(
                    f"не хватает места: нужно {fmt_both(need)}, свободно "
                    f"{fmt_both(free)}"
                ),
                skipped=True,
                required_bytes=need,
                free_bytes=free,
            )

        try:
            measurement = measure(
                self.veracrypt, self.workdir, step, self.progress, self._guard
            )
        except Stopped:
            # Отмена — не неудача шага: замера просто нет, а контейнер уже
            # убран в finally.
            return StepResult(step, error="остановлено по требованию")
        except (VeraCryptError, OSError) as exc:
            # OSError тоже сюда: том может пропасть из-под ног между
            # монтированием и чтением, и это неудача шага, а не всего сбора.
            # Сорвавшаяся самопроверка останавливает всё: без неё неизвестно,
            # можно ли верить динамическим контейнерам на этой машине.
            return StepResult(step, error=str(exc), fatal=step.self_check)

        if not step.self_check:
            return StepResult(step, measurement)

        self._checks.append(measurement)
        if len(self._checks) < 2:
            return StepResult(step, measurement)
        verdict = self_check_verdict(*self._checks[:2])
        return StepResult(step, measurement, error=verdict, fatal=bool(verdict))
