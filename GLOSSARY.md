# Glossary

One word per idea, everywhere: code comments, docstrings, `SPEC.md`,
`CLAUDE.md`, `README.md` and, later, the English UI catalog. A term that
drifts between files reads as two different things, and in this project the
difference between, say, *copy slack* and *safety margin* is the whole point.

Where the code already has a name for the idea, the glossary uses it.

The Russian column is the current wording of the UI and of the old docs. It
is quoted in «guillemets»: that is also how English text quotes a Russian UI
label when the exact wording matters. Everywhere else, name a UI element by
its English term.

## The container and what it consists of

| English | Russian | Notes |
|---|---|---|
| container | «контейнер» | The file VeraCrypt creates. |
| `Container init` | «Container init» | The size in MiB typed into VeraCrypt. Kept verbatim. |
| volume | «том» | The filesystem inside a mounted container. |
| volume capacity | «ёмкость тома» | What the mounted volume reports (`mounted_bytes`). |
| empty free space | «свободное место на пустом томе» | `empty_free_bytes`. |
| VeraCrypt header | «заголовок VeraCrypt» | `VC_HEADER_BYTES`. |
| filesystem metadata; NTFS metadata | «метаданные ФС»; «метаданные NTFS» | What the filesystem keeps for itself on an empty volume. |
| payload | «полезные данные» | The data itself. *Cluster-rounded payload*: `payload_alloc`. |
| cluster tail | «кластерный хвост» | `cluster_tail`. |
| copy slack | «запас на копирование» | Space the files take beyond their cluster-rounded size. `CopySlackModel`. |
| per-file slack | «по-файловая часть запаса» | The slope of copy slack against the file count. |
| safety margin | «страховочный запас», «страховка» | `SafetyModel`. |
| safety floor | «нижний предел страховки» | `MIN_SAFETY_BYTES`. |
| factory margin | «надбавка» | `FACTORY_MARGIN_BYTES`. |
| left space | «остаток» | Free space after copying. VeraCrypt's own term, *Left space*. |
| breakdown | «разложение» | The container split into its components. *Breakdown bar*: «полоса разложения». |
| component | «слагаемое» | One part of the breakdown. |
| cluster-rounded size | «объём по кластерам» | |
| used space | «занятое место» | |
| space margin | «запас» in the free-space pre-check | `SPACE_MARGIN_BYTES`. Room left on the host disk; not copy slack. |

## Calculation and models

| English | Russian | Notes |
|---|---|---|
| calculation | «расчёт» | |
| solver | «решатель» | `solve_container_mib`. |
| default model; baseline | «модель по умолчанию»; «базовая» | Used with fewer than two points. |
| calibrated model | «калиброванная модель» | |
| piecewise-linear interpolation | «кусочно-линейная интерполяция» | |
| segment; slope; chord | «отрезок»; «наклон»; «хорда» | |
| step | «ступень» | A jump in the curve, e.g. `$LogFile` growing at a threshold. |
| extrapolation | «экстраполяция» | |
| underestimate; overestimate | «занижение»; «перезаклад» | Underestimate is the only dangerous side. |
| prediction | «обещание расчёта», «прогноз» | `predicted_mib`: what the calculation promised when the record was made. |
| miss | «промах» | Prediction minus the exact minimum. Positive is overestimate. |
| volume profile | «профиль тома» | Filesystem plus cluster size. Planned. |
| advice | «совет» | The computed safety margin (`advice`). |
| auto-selection | «автоподбор», «подбор» | Of the safety margin, and of the display unit under Auto. |
| calculation path | «расчётный путь» | Where only integers are allowed. |
| constant part; intercept | «постоянная часть»; «свободный член» | Of copy slack: `slack_base`. |
| interpolation bound | «граница интерполяции» | `interpolation_bound`. |
| anchor points | «опорные точки» | |
| knee | «излом» | The bend of the NTFS curve, e.g. at 48 GiB. |
| sag | «прогиб» | How far the curve bows away from a chord. |
| staircase; plateau; hump | «лестница»; «полка»; «горб» | Shapes of the NTFS curve. |
| upper envelope | «верхняя огибающая» | |
| gap | «прореха», «зазор» | The space between two measurements; the leave-one-out gap `h_loo`. |

## Measurements and calibration

| English | Russian | Notes |
|---|---|---|
| measurement | «замер» | |
| record | «запись» | |
| copy records | «записи о копировании» | `Records.json`. |
| empty-volume measurements | «замеры пустых томов» | `Calibration.json`. |
| calibration point | «точка калибровки» | `is_calibration_point`. |
| own measurement; factory measurement | «свой замер»; «заводской замер» | Own: taken on this machine. Factory: shipped, taken elsewhere. |
| factory data | «заводские данные» | `containerhelper/data/`. |
| disabled | «отключён» | An own measurement kept in the file but not used. |
| supersede | «вытеснять» | «своё вытесняет заводское»: own supersedes factory. |
| plausibility check | «проверка правдоподобия» | `validate`, `Issue`. |
| leave-one-out check | «проверка исключением» | Each point predicted by a model built without it. |
| residuals | «остатки» | On charts. |
| edge measurements | «крайние замеры» | The outermost points, hidden by default on the residual chart. |
| coverage; coverage table | «покрытие»; «таблица покрытия» | |
| recommended sizes | «рекомендуемые размеры» | `RECOMMENDED_MIB`. |
| file set | «набор», «набор файлов» | `FileSet`. *File set key*: `FileSet.key`. |
| copy-slack measurement | «замер запаса» | |
| measurements file | «файл замеров» | |
| manual record | «ручная запись» | Entered by hand rather than measured by the program. |
| scope | «область» | Of an `Issue`: `ntfs` or `slack`. |
| reconciliation | «сверка» | Comparing two figures that must agree, e.g. volume against copied data. |
| run | «прогон» | One pass of automatic collection on a real machine. |
| this machine | «эта машина» | |
| Windows build | «сборка Windows» | |

## Automatic collection

| English | Russian | Notes |
|---|---|---|
| automatic collection | «автоматический сбор» | `collect.py`. |
| step; step weight | «шаг»; «вес шага» | |
| self-check | «самопроверка» | |
| dynamic container | «динамический контейнер» | Sparse on the host. |
| quick format; full format | «быстрое форматирование»; «полное» | |
| mount; unmount | «смонтировать»; «размонтировать» | *Dismount* only when naming VeraCrypt's old `/dismount` switch. |
| cleanup | «уборка» | |
| orphaned container | «осиротевший контейнер» | |
| administrator rights; elevated restart | «права администратора»; «перезапуск от администратора» | |
| portable data folder | «портативная папка», «папка данных» | |
| working folder | «рабочая папка» | Where collection creates its containers. |
| normal container | «обычный контейнер» | Not dynamic. |
| remount | «перемонтирование» | |
| exit code | «код возврата» | Means nothing under `/silent`. |
| phase clock | «часы фазы» | |
| cost of a file set | «цена набора» | |

## Charts

| English | Russian | Notes |
|---|---|---|
| chart | «график» | |
| chart window | «окно графиков» | |
| detached chart | «отсоединённый график» | `DetachedChart`. |
| column layout; grid layout | «столбец»; «сетка» | |
| splitter | «разделитель» | |
| crosshair | «перекрестье» | |
| shared X axis | «общая ось X» | |
| tick | «деление» | |
| axis label | «подпись оси» | |
| legend; series | «легенда»; «серия» | |
| full view | «полный вид» | |
| pan; zoom | «панорама», «возить»; «масштаб» | |
| shared window | «общее окно» | The chart window holding all charts, as opposed to a detached one. |
| plot area; margin | «поле графика»; «поле» | |
| title band | «полоса заголовка» | |
| note | «подпись» under a chart | `chart.note`. *Caption* is fine in prose. |
| rubber band | «рамка» | Zoom selection, after `QRubberBand`. |
| backing | «подложка» | Behind the crosshair values. |
| polyline; stem | «ломаная»; «стебель» | |
| 1-2-5 ladder | «лестница 1-2-5» | Tick steps. |
| in frame; out of frame | «в кадре»; «вне кадра» | |
| coverage strip | «лента покрытия» | A removed panel; appears only in history. |

## UI mechanics

| English | Russian | Notes |
|---|---|---|
| modeless window | «немодальное окно» | |
| tooltip | «подсказка» | |
| wrapped label | «переносимая подпись» | `wrapped`. |
| height grip | «ручка высоты» | |
| three-state sorting | «сортировка в три состояния» | |
| view row | «строка вида» | The row of view options under the tabs. |
| file and folder picker | «диалог выбора файлов и папок» | `ui/path_picker.py`. Short: *picker*. |
| "Selected" line | «Выбрано» | The picker's selection field. |
| My Computer | «Компьютер» | Qt's list of drives; Qt's own English label on Windows. |
| edit window | «окно правки» | |
| check box; toggle | «галочка»; «переключатель» | |
| scroll area | «прокрутка» | |
| first fill | «первое наполнение» | When column widths are fitted, once. |
| digit-group separators | «разделители разрядов» | |
| dash | «прочерк» | Shown for a missing value. |
| Explorer | «Проводник» | |

## UI labels

The English wording planned for the UI. Until the UI is translated, the
program shows the Russian one.

### Tabs

| English | Russian |
|---|---|
| Calculation | «Расчёт» |
| Records | «Записи» |
| Model | «Модель» |
| Calibration | «Калибровка» |

### Buttons and check boxes

| English | Russian |
|---|---|
| Collect automatically… | «Снять автоматически…» |
| Re-measure | «Переснять» |
| Use factory | «К заводскому» |
| Restore own | «Вернуть своё» |
| Disable all own measurements | «Отключить все свои замеры» |
| Measure volume | «Измерить том» |
| Measure left space | «Замерить остаток» |
| Take from Calculation | «Взять с «Расчёта»» |
| Metadata chart… | «График метаданных…» |
| Miss chart… | «График промахов…» |
| Save image… | «Сохранить картинку…» |
| Copy image | «Копировать картинку» |
| Reset zoom | «Сбросить масштаб» |
| Shared X scale | «Общий масштаб по X» |
| Shared crosshair | «Общее перекрестье» |
| Select… | «Выбрать…» |
| Select (the picker's accept button) | «Выбрать» |
| Select all; Clear selection; Invert | «Выделить всё»; «Снять выделение»; «Инвертировать» |
| Measure | «Снять» |
| Edit… | «Изменить…» |
| Save; Cancel | «Сохранить»; «Отмена» |
| Only missing sizes | «Только недостающие размеры» |
| Choose folder… | «Указать папку…» |
| Add… | «Добавить…» |
| Remove | «Убрать» |
| Delete | «Удалить» |
| Copy | «Копировать» |
| Start; Stop; Close | «Начать»; «Остановить»; «Закрыть» |
| Restart as administrator | «Перезапустить от администратора» |
| Start with a self-check (1 GiB twice) | «Начать с самопроверки (1 GiB дважды)» |
| Show hidden | «Показывать скрытые» |
| Remember folder | «Запоминать папку» |
| Full-height tables | «Таблицы во всю высоту» |
| Remember tab | «Запоминать вкладку» |
| Auto | «Авто» |

### Groups and columns

| English | Russian |
|---|---|
| Input data | «Исходные данные» |
| Safety margin | «Страховочный запас» |
| Calibration status | «Состояние калибровки» |
| How to measure a point | «Как снять точку» |
| Measured values | «Измеренные величины» |
| Computed automatically, not saved | «Вычисляется автоматически, в файл не пишется» |
| What to measure | «Что снимать» |
| Where to create containers | «Где создавать контейнеры» |
| Permissions | «Права» |
| Parameters | «Параметры» |
| Name | «Имя» |
| Files | «Файлов» |
| Deviation from baseline | «Откл. от базовой» |
| Source | «Источник» |
| own measurement; factory; own, disabled; not measured | «свой замер»; «заводской»; «свой, отключён»; «нет замера» |
| Cluster size | «Размер кластера» |
| Basis; Bytes | «Основа»; «Байт» |
| None (the filesystem choice in VeraCrypt) | «нет» |
