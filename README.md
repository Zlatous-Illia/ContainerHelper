# ContainerHelper

Calculates which container size (`Container init`, MiB) to enter in VeraCrypt
so that a given data set fits into it, and accumulates the measurements that
refine this calculation.

The application does **not copy** files. It calculates the size and reads the
free space from an already mounted volume, whatever that volume is filled with.

Windows, Python 3.12, PySide6. PySide6 is the only external dependency.

The interface is currently in Russian only, so the Russian labels of controls
are given in «» next to their English names.

## Why this is needed

VeraCrypt asks for the container size, but the usable space inside is always
smaller. The difference is eaten by three things, and VeraCrypt names none of
them in advance:

| Component | Behaviour | Example for 700 MiB of data |
|---|---|---|
| VeraCrypt header | constant 266 240 B | 0.25 MiB |
| NTFS metadata | depends on the **volume** size, not on its contents | 16.7 MiB |
| Copy slack | an MFT record per file + growth of directory indexes | 0.01 MiB |
| Cluster tail | each file is rounded up to a whole cluster | part of the data |
| Safety margin | allowance for the error of the models | 4 MiB |

It is easy to miss in either direction: 700 MiB of data needs a 721 MiB
container, and with 400 small files the choice of cluster size changes the
used space from 1.2 to 25 MiB. The program calculates this in advance, not
after the copy has broken off halfway.

## How it calculates

The volume size depends on the container size, and the metadata depends on the
volume size, so the solution is iterative (`model.solve_container_mib`,
converges in two or three iterations).

NTFS metadata is modelled by **piecewise-linear interpolation** over measured
points, not by a single straight line: `$LogFile` barely grows with the volume
and hits a ceiling of 64 MiB, and only `$Bitmap` grows linearly. Outside the
measured range the slope is the baseline one, not a fitted one: a local slope
carried far beyond its own span misses by an order of magnitude.

**Safety property:** the calculation must never return a container smaller
than needed. An overestimate costs space, an underestimate costs data that does
not fit. The corresponding tests (`tests/test_model.py::SolverTests`) are tied
to three real measurements and must not be weakened.

All internal calculations are in integers. A float is allowed only as a model
coefficient and is rounded up at once; a float that leaks into a byte counter
is a defect, not a style nitpick.

## Installation and running

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

.venv\Scripts\python.exe -m containerhelper
.venv\Scripts\python.exe -m containerhelper --data <path>   # a different data folder
```

Tests are the only automated check; there is no linter and no type checking:

```bash
.venv\Scripts\python.exe -m unittest discover -s . -p "test_*.py"
```

889 tests, almost half of them GUI tests. They set `QT_QPA_PLATFORM=offscreen`
themselves before importing PySide6, open no windows and redirect the settings
to a temporary directory; no separate environment setup is needed.

## Where the data lives

The program is portable: all its state is in the `data` folder next to the
executable, and the registry is not touched at all.

```
data/
  Records.json        copy records
  Calibration.json    empty-volume and copy-slack measurements: this machine's calibration
  settings.ini        window, columns, units, file and folder picker, chart layout
  *.bak               one backup per file
```

The folder is looked up in order: the `--data <path>` argument → the folder
next to the program → the choice remembered earlier. If none of them fits, the
program **asks** instead of silently moving to a foreign directory: a silent
move breaks portability without anyone noticing.

## Tabs

Four, in working order: **Calculation** («Расчёт») → **Records** («Записи») →
**Model** («Модель») → **Calibration** («Калибровка»).

- **Calculation.** Choose files and folders in one dialog (a container holds
  both, usually together), drag them with the mouse from Explorer, or type the
  size by hand. The result is `Container init` in MiB and its breakdown into
  components.

  The file and folder picker remembers its view, its size and the folder it
  shows; with Remember folder («Запоминать папку») off, it opens at My Computer
  («Компьютер»), the list of drives, and not in the program's folder: the data
  lives anywhere but next to the program. Numeric fields accept nothing but
  digits and digit-group separators: a letter there is a mistyped key, not a
  value that failed to parse.
- **Records.** Real copy operations: which container, what went into it, how much
  space was left. They calibrate copy slack and check the prediction.

  Edit windows are modeless, and there can be several of them: a record is
  filled in while looking at the Calculation tab, which is the source of the
  size and of the file count. Still, there is only one window per record: two
  windows on one row are a race won by whoever presses Save («Сохранить») last.
- **Model.** What both models rest on and how far they miss on their own
  measurements, each one predicted by a model built **without it**. There is
  nothing to edit here; this is diagnostics.
- **Calibration.** Coverage of sizes by empty-volume measurements. No data is
  needed for such a measurement: the metadata depends only on the volume size.

## Calibration

The package ships factory data: **26 empty-volume measurements** from 512 MiB
to 1 TiB (one per row of the recommended-sizes table) and **7 copy-slack
measurements**. A new copy gives meaningful results from the first launch.

An own measurement always supersedes the factory one at the same volume size,
not "the larger of the two": the factory value was taken on another machine,
and the metadata size is decided not by NTFS in general but by the specific
formatting code, that is, by the Windows build and the VeraCrypt version. While
a segment rests on factory points only, a factory margin of 4 MiB is added to
the safety margin.

### Automatic collection

The Collect automatically… button («Снять автоматически…») creates containers
of the missing sizes through the VeraCrypt CLI, measures them and deletes them.
By hand that is some twenty containers and a whole evening; automatically it
takes about three minutes for the whole series.

What you need to know:

- **Administrator rights are required.** Otherwise, because of
  `/filesystem NTFS`, UAC would ask for every container. The program restarts
  itself via `runas`, always passing the current data folder, and closes the
  old window: two copies in one folder would write over each other.
- **VeraCrypt 1.24 or later**: earlier versions have neither `/nosizecheck` nor
  `/quick`. The unmount switch depends on the version: `/unmount` appeared in
  1.26.20, before it there was only `/dismount`. A version that could not be
  read is treated as old.
- **The exit code means nothing.** `/silent` is documented as "operation will
  fail silently", so the result is checked only by the facts: a file of the
  right size, a drive letter that came up, a letter that disappeared after
  unmounting.
- **Unmounting is retried, not waited out.** VeraCrypt does not let go of a
  volume that has just been filled with files right away.
- **Space is estimated with the same model that the collection calibrates.** A
  dynamic container takes up disk space only for the clusters written to it:
  an empty terabyte volume costs 136 MiB. An uncalibrated model overestimates
  this thirteen times over. The error is on the safe side: a step will be
  skipped for nothing, but never started where there is not enough room for it.
- **Cleanup in `finally`, plus a search for orphaned**
  `containerhelper-calibration-*.hc` files. Without it, terabyte files pile up
  silently.
- **Progress is measured in bytes, not in steps**, and the clock runs on its
  own, once a second. Progress comes from the step, and during container
  creation, full format and mounting there is none at all: the time froze for
  minutes exactly when the only sign that the program was alive was needed. Each
  phase has its own clock: the overall one tells how long the collection has
  been running, the phase one how long the program has been stuck in one place.

A run on a live machine has taken place: 22 sizes in 3 minutes 10 seconds, all
of them matched the manual measurements **to the byte**. The copy-slack
collection was run separately: thirty steps, the metadata was confirmed to the
byte, and the per-file slack is measured and stable (about 1360 B per file on
the 500…10 000 series).

## Charts

Nine charts in four modeless windows: the NTFS metadata curve, the share of the
volume taken by the metadata, the model's miss under the leave-one-out check and
the slopes of the segments; copy slack against the file count and its miss; the
prediction miss across records; the breakdown of the current container as a
bar, and the used space against the cluster size.

They are drawn by our own widget on `QPainter`, without a charting library. The
decision was made by measurement, not on principle: four single-file portable
builds were made and compared — matplotlib would cost +33.7 MiB and +548 ms at
startup, pyqtgraph +22.8 MiB and +428 ms, QtCharts +3.0 MiB. Our own widget
cost **+103 KiB**, and on top of that its labels follow the unit chosen in the
window (B/KiB/MiB/GiB), and its colours come from the window's palette. Export
to PNG, SVG and PDF is drawn by the same code as the screen.

Mouse: drag a box with the left button to zoom in, the wheel zooms, hold the
right button to pan, double right-click or Esc for the full view. The menu
opens on a double left-click or the middle button. Clicking the legend hides a
series, clicking a point breaks it down. A crosshair with values on the axes
follows the cursor; on charts with a shared X axis the neighbours repeat it by
value, each drawing it in its own scale.

The window lays the charts out **in a column or in a grid** of two per row:
vertical space is more expensive, and four charts in a column ask for nearly a
thousand pixels of height, in a grid half that. Charts are reordered with the
arrows in their corner, and any of them can be detached into a separate window
and put back. The layout, the order, the sizes and what is detached are
remembered for each window.

Updating the data resets neither the zoom, nor the hidden series, nor the
selected point: a window is opened to examine a region, and collection goes in
steps, so the picture would jump to the full view at every step. The zoom is
reset only if not a single point is left in the frame; how many of them went
out of the frame is written in the corner.

## Limitations

- **Windows only and NTFS only.** Free space and cluster size are read through
  `GetDiskFreeSpaceW`, administrator rights go through `ShellExecuteW`.
  Clusters exist in any filesystem, but the metadata has been measured only on
  NTFS.
- **All known numbers were taken on one machine**: one Windows build, one
  VeraCrypt version. There are no measurements from another machine at all;
  that is what the 4 MiB factory margin on factory segments is for.
- **The metadata curve is not convex, and at the bottom it is a staircase.**
  `$LogFile` changes in steps at discrete thresholds, and a step can hide
  between any two points, however dense the grid. The underestimate here is
  covered by the safety floor.
- **The prediction is checked after the fact.** What the calculation promised is
  stored in the record and not recalculated: the models change with every
  measurement, and recalculating after the fact would answer "what would I say
  today", not "what did I say then".
- **The tests do not reach reality.** The whole VeraCrypt path runs on a substituted
  `subprocess` and substituted volume reading: the commands and their order are
  checked, but not VeraCrypt itself. The readability and the layout of the
  window cannot be checked by anything either.

## Out of scope

Listed explicitly so that it does not come back during further work: copying
the user's files by the application; themes and keyboard shortcuts; extended
statistics (medians, box-plot, CDF, correlations, slices by type);
export/import and filtering of records; enlarging a container with VeraCrypt
Expander; reverse calculation of the overhead percentage, since percentage is not
used as a concept at all.

## Structure

`SPEC.md` is the living specification and the main source of truth. It
explains not only what has been done but also why the alternatives were
rejected, and it is edited **in the same change** that changes the behaviour.
`CLAUDE.md` is a condensed digest of the same rules for those who edit the
code.

In layers, bottom up. The lower ones know nothing about Qt and are tested
without it.

| Module | What it does |
|---|---|
| `model.py` | arithmetic: NTFS, copy slack and safety margin models, solving for the size |
| `records.py` | JSON storage (schema 5), plausibility checks, calibration points |
| `sizes.py` | walking the source data and reading mounted volumes |
| `fileset.py` | file sets for measuring copy slack and generating them on a volume |
| `factory.py` | factory data from `containerhelper/data/`, read-only |
| `veracrypt.py` | finding the installation, version, create/mount/unmount |
| `collect.py` | order of automatic collection: plan, self-check, step, cleanup |
| `elevation.py` | administrator rights and the elevated restart |
| `plot.py` | chart arithmetic: axes, ticks, pixels, hit testing |
| `charts.py` | building charts from measurements and models |
| `ui/` | tabs, dialogs, tables, the chart canvas and its window |

Why everything lives in the `containerhelper/` package and not in the root:
`python -m containerhelper` works only for a package; the factory data is read
through `importlib.resources` as the `containerhelper.data` resource, because
in a single-file build a path does not lead to the disk; the `data/` folder
next to the program is the user's state and has to be told apart from the
code; names like `model.py` or `table.py` in the root would collide with other
people's modules on `sys.path`.

### What breaks silently

Rules whose violation does not crash the program but quietly corrupts the
numbers. The full list is in `SPEC.md` and in the comments next to the relevant
code; here are the ones people trip over first:

- **Computed values are not stored.** `ntfs_bytes`, `vc_header`,
  `copy_slack_measured` are properties, not JSON fields. Duplicating computed
  fields is exactly what spoiled the original handwritten records.
- **Qt signals carrying volume sizes must be 64-bit** (`Signal("qint64", bool)`).
  Qt's `int` is a four-byte C++ `int`, and everything from 4 GiB up overflows
  silently.
- **`setHorizontalHeaderLabels` erases the header tooltips**: always follow it
  with `set_header_tooltips(...)`.
- **UI state is stored by name, not by number.** The active tab and the geometry
  of chart windows go by title: a single reordering would scramble the list of
  indices, and there would be no way to notice.
- **Logic does not raise modal windows.** Showing errors and confirmations goes
  through substitutable attributes; otherwise a test has no way to close the
  dialog.
- **Residuals on charts are computed by the leave-one-out check.** A
  piecewise-linear model passes exactly through its own measurements: ordinary
  residuals would come out zero everywhere.
- **Rotated text grows to the right of the rotation point.** After
  `rotate(-90)` the `y` coordinate turns into the screen `x`: a rectangle set
  "upwards" ends up left of the padding, and the second line of the axis label
  does not get into the widget at all. From outside this looks like "the label
  is cut off", and the cause is looked for in the wrong place.
- **A button's `clicked` arrives with a boolean argument.** A lambda without a
  dummy first parameter receives it into its first name: the button is pressed
  and does nothing, silently, because there is simply no such value.

## License

Not chosen.
