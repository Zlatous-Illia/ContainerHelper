# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language

Comments, docstrings, docs (`*.md`) and commit messages are **English**;
identifiers are English. Terms come from `GLOSSARY.md` — one word per idea,
and *copy slack* is never *safety margin*.

The UI is still Russian, and its strings are moving out of the code into
per-language catalogs (`containerhelper/locale/<code>.json`), module by
module. A new UI string goes in as a key: `tr("records.col.metadata")`, with
the text in `en.json` and `ru.json` both; a string not yet moved stays a
Russian literal and is not translated in place. English prose quotes a
Russian label only where the exact wording matters, in «guillemets».

`tests/test_i18n.py` fails on a key that is missing from a catalog or unused,
on a key glued together (a key chosen by condition comes from a dict of
literals), and on a `tr()` that runs at import.

`tests/test_source_language.py` fails on Cyrillic in comments, docstrings or
docs outside «…» and backticks.

## Commands

```bash
# Tests — the only automated check
.venv/Scripts/python.exe -m unittest discover -s . -p "test_*.py"

# One module / class / test
.venv/Scripts/python.exe -m unittest tests.test_model -q
.venv/Scripts/python.exe -m unittest tests.test_ui_shell.CoverageTests -q
.venv/Scripts/python.exe -m unittest tests.test_model.SolverTests.test_matches_the_core_solver

# Run the application
.venv/Scripts/python.exe -m containerhelper
.venv/Scripts/python.exe -m containerhelper --data <path>   # a different data folder

# Restore the environment (Python 3.12, the only dependency is PySide6)
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

GUI tests set `QT_QPA_PLATFORM=offscreen` themselves before importing PySide6,
open no windows and redirect settings to a temporary directory. The gate needs
no separate environment setup.

There is no linter, formatter or type checker — the dialect is kept by review
and by imitating the surrounding code.

## What this program is

It calculates which container size (`Container init`, MiB) to enter in
VeraCrypt so that a given set of data fits in it, and accumulates the
measurements that refine this calculation. The application does **not copy**
files — free space is read from the volume regardless of what fills it.

`SPEC.md` at the root is the living specification and the main source of
truth. It explains not only what was done but also why alternatives were
rejected; it is edited **in the same change** that changes the behavior, and
no separate proposal document is needed.

`init prompt.txt` is the original assignment, of which about a third remains.
The "Out of scope" section in `SPEC.md` lists what was cut on purpose (copying
of user files, export/import, extended statistics, reverse calculation of the
percentage). Charts stood there too; the ban was lifted, but only for
calibration quantities and the breakdown of the current calculation (see SPEC "Charts"). Don't bring the rest back on seeing it in
`init prompt.txt`.

## Architecture

In layers, bottom up. Every module below except `ui/` knows nothing about Qt
and can be tested without it.

**`model.py`** — arithmetic. `MetadataModel` (NTFS metadata as a function of
the volume size), `CopySlackModel` (the space that the arrival of files takes
beyond their cluster-rounded size), `SafetyModel` (the safety margin),
`solve_container_mib` (an iterative solution — the volume size depends on the
container size and vice versa).

**`records.py`** — the store (`Store`, atomic write, a single `.bak`), JSON
schema version 6, plausibility checks (`validate` → `Issue` with the scope
`metadata`/`slack`) and turning records into calibration points
(`metadata_points`, `slack_samples`, `metadata_cross_check`) of one volume
profile (`VolumeProfile`: filesystem plus cluster size). There are two
files: `Records.json` holds the copy records, the neighboring
`Calibration.json` holds the empty-volume measurements. Measurements describe the machine (the Windows
build and the VeraCrypt version), records describe the data; `Store` owns both
and, on opening an old single-file store, moves the measurements into place
by itself.

**`sizes.py`** — walking the source data (`scan_paths`, several files and
folders at once, nested paths are dropped) and reading mounted volumes through
`shutil.disk_usage` + `GetDiskFreeSpaceW`/`GetVolumeInformationW`.

**`fileset.py`** — file sets for measuring copy slack: their composition
(`FileSet`, `Group`), their arithmetic and generation right on the mounted
volume (`generate`). The sets have different file counts on purpose:
`CopySlackModel` computes the slope only with two or more distinct `n`.

**`factory.py`** — factory data from `containerhelper/data/`, read-only,
through `importlib.resources` (in a one-file build a path leads nowhere on
disk). Two files: twenty-six empty-volume measurements (one for each row of
the recommended sizes table) and seven copy-slack measurements. The latter
also give seven NTFS points on "non-round" volumes — each one's empty volume
was measured before the files were written. Four points — 768, 1536, 3072 and
6144 MiB — were carried into the factory data from own measurements: carrying
copies rather than moves, otherwise on the very machine where the numbers were
taken the segment would become "foreign" and get the factory margin
FACTORY_MARGIN_BYTES.

**`veracrypt.py`** — finding the installation (the standard Program Files,
portable names with an architecture suffix, a folder chosen by hand), the
version from the exe's resources, and three operations: create, mount,
unmount. No Qt; `subprocess` and volume reading are substituted through
fields.

**`collect.py`** — the procedure of automatic collection: the plan of steps,
the self-check, one step with cleanup in `finally`, the estimate of the space
required and of the step weight in bytes. A step comes in two kinds: an empty
volume (an NTFS point) and a volume with a file set (a copy-slack
measurement). Also without Qt.

**`elevation.py`** — administrator rights and the elevated restart through
`ShellExecuteW`.

**`plot.py`** — chart arithmetic: round ticks on the 1-2-5 ladder, mapping
values to pixels (`Span`, `Frame`), finding the point under the cursor. No Qt.
Float is allowed here and does not break the rule "only integers in the
calculation path": the calculation path is the container size, while here it
is pixels, and no number from here goes back into the model.

**`charts.py`** — building a `Chart` from measurements and models: nine
charts. Also without Qt: what to draw is data, and what has to be checked is
which points ended up on the chart and what the tooltip says.

**`i18n.py`** — UI text by key: `tr`, `tr_n` (plural forms by the rule
of the language), `set_language`. English is the reference and the fallback.
The first launch speaks the Windows UI language if it is shipped, English
otherwise (`system_language`); the suite pins Russian in `tests/__init__.py`.
No Qt: the layers below `ui/` build text too. Text is built when shown, not
when the object is created — a module-level constant holds the key, not the
text, or it would stay in the language of the import.

**`ui/`** — four tabs in working order: Calculation → Records → Model →
Calibration. `app.py` owns the models and hands them to the tabs through
callbacks; the tabs know nothing about each other and talk through signals via
the window. `ui/table.py` is the shared table behavior (three-state sorting,
manual width, height grip, header tooltips), and along with it the behavior of
input fields: validators (`digits_only`, `plain_text`), width fitting and
wrapped labels (`wrapped`).

Record edit windows are modeless: `RecordsTab` keeps a registry of the open
ones (`_editors` by `id(record)`, `_points` by container size for new
calibration points, `_creators` for the rest of the new records) and puts an
edit back by finding the record **by identity** (`Store.index_of`). The file
and folder picker lives for one showing, and everything it must outlive lies
in `PickerState` (`ui/path_picker.py`) and is saved by the main window.

Charts live in separate modeless windows: `ui/chart.py` is the canvas on
`QPainter` (drawing, mouse, crosshair, legend, export to PNG/SVG/PDF),
`ui/chart_window.py` is the window with one or several charts plus the windows
of detached ones (`DetachedChart`). The buttons on the tabs only ask for a
window with the `chartRequested` signal; `app.py` opens and updates the
windows. Our own drawing rather than a library — by measurement: matplotlib
would cost +34 MiB to the portable build and +548 ms at startup, pyqtgraph
+23 MiB and +428 ms, QtCharts +3 MiB, but its axis labels have no "Auto" mode
and its colors bypass the window palette. Details and the table of
measurements are in the "Charts" section of SPEC.

### Rules that break silently

- **Only integers in the calculation path.** Float is allowed only as a model
  coefficient and is immediately rounded up through `ceil_div`. A float that
  leaks into a byte counter is a defect, not a style nitpick.
- **Computed values are not stored.** `metadata_bytes`, `tail_bytes`,
  `copy_slack_measured` are properties, not JSON fields. It was precisely the
  duplication of computed fields that corrupted the original hand-written
  records (in them NTFS is written as `36 573` instead of `36 573 184`).
- **Qt signals carrying volume sizes must be 64-bit**
  (`Signal("qint64", bool)`). Qt's `int` is a C++ `int` of four bytes, and
  everything from 4 GiB up overflows silently: the value is truncated, matches
  no measurement, and the button just does nothing.
- **`setHorizontalHeaderLabels` erases the header tooltips.** After it, always
  `table.set_header_tooltips(...)` — including when the units change.
- **Column widths are fitted once**, on the first fill. Doing it on every
  update means wiping out what was stretched by hand. The exception is
  `fit_widget_columns`: widgets in cells (the Re-measure and Use factory
  buttons) do not exist for `resizeColumnsToContents` at all, and the column
  holding them came out at 97 px where 284 were needed. It is called on every
  update and **only widens**.
- **A wrapped label has to be given its height by hand.** The minimum height
  of a `QLabel` does not depend on its width, and under a scroll area the
  layout squeezes it to one line and less — the second line disappears
  entirely. The height has to be computed from the guaranteed minimum width of
  the content, not from the current one.
- **UI state is stored by name, not by number.** The active tab is stored as
  its title (`TAB_CALC` and its siblings), not its index: after a single
  reordering of the tabs the program would open on the wrong page, and there
  would be nothing to notice — it did open successfully. For the same reason
  the tests look a tab up by its title.
- **Logic does not raise modal windows.** Showing errors and confirmations
  goes through substitutable attributes (`RecordsTab.report_error`,
  `RecordsTab.confirm`), otherwise a test has nothing to close the dialog
  with.
- **A record in an open window is found by identity, not by number.** The
  windows are modeless, and while one is open the list has time to change: a
  number taken at opening already points to someone else's row. Equality won't
  do either — `Record` compares by value, and the edit would go into whichever
  of two identical records came first. Hence `Store.index_of` and the registry
  by `id(record)`; one record has strictly one window.
- **A dialog's size is stored as numbers, not with `saveGeometry`.**
  `restoreGeometry` compares the width of the screen it was saved on with the
  current one and, if they differ by more than a quarter, silently does
  nothing — and QDialog then fits the window to its content. It looks like
  "the size resets on every launch". The size has to be set in `showEvent`:
  in the constructor the same fitting overrides it.
- **We write the «Выбрано» ("Selected") line ourselves, with signals
  blocked.** The dialog puts only files there (a folder, to it, is a way
  deeper), and answers every edit of the line with autocompletion and
  **rearranges the selection**. Hence both the lost folders and the broken
  second inversion in a row. The truth is in the list (`selected_paths()`);
  we enable the «Выбрать» ("Select") button ourselves; we look at the line
  only if a person typed into it (`textEdited`).
- **The picker's starting folder is the one shown, not the one selected.**
  Taken from the selected path, it went one level deeper on every showing:
  select folder 2 in folder 1, and the picker opened inside folder 2. It is
  asked of the list itself (`model.filePath(view.rootIndex())`): at the
  "My Computer" level `directory()` returns the process's working directory, and
  `directoryEntered` is not emitted on a programmatic change of folder at all.
- **A tooltip does not retell what is visible next to it.** Still less is it
  hung on a dialog or a whole group: Qt hands it to every child that has none
  of its own, and the picker dialog's tooltip popped up over every file in the
  list, repeating the label beneath it.
- **A numeric field rejects letters — with a validator, not by parsing.** The
  validator and `parse_bytes` share one set of allowed characters
  (`formatting.IGNORED_IN_INPUT`): if they diverged, the field would accept
  what the parsing does not understand. Previously `parse_bytes` silently
  returned `None`, the field kept the garbage, and Container init turned into
  a dash.
- **The metadata model's X axis is the volume size, not the capacity.**
  `volume_bytes = container − VC_HEADERS_BYTES` is computed; `mounted_bytes` is
  measured and smaller by the filesystem tail, which counts as metadata. Every
  key that matches a measurement to a size — superseding, `factory_volumes`,
  the coverage table, the Use factory signal — goes by `volume_bytes`: a key by
  capacity on one side and by volume on the other matches nothing, and the
  button silently does nothing. `mounted_bytes` stays a guard ("was the volume
  measured at all") and feeds the tail check.
- **Own supersedes factory**, not "the larger of the two" as in
  `MetadataModel._dedupe`: factory data was taken on another machine. A
  disabled own measurement (`disabled`) is not deleted — the numbers stay in
  the file.
  Which factory measurement gets superseded is decided by the quantity
  the measurement describes: for a point, the volume size; for a copy-slack measurement,
  the file count. Not to be confused with `put_calibration`: there an own measurement
  supersedes an own one, and the key for copy slack is different — the file
  set (see below).
- **Every such key holds within the volume profile** (`profile_of`: the
  filesystem, with FAT32 as FAT and an unread one as NTFS, plus the cluster,
  with 0 as 4 KiB on NTFS only). The volume size and the file count are the same on every
  filesystem, so a key without the profile lets an exFAT measurement
  supersede, disable or cover an NTFS one — and an exFAT copy-slack
  measurement pull the NTFS per-file slack down, the dangerous side. Until the
  profile can be chosen, everything calculates for `DEFAULT_PROFILE`
  (NTFS, 4 KiB) through default arguments; a new call site that forgets the
  profile silently gets that one.
- **The NTFS curve is not convex, and at the bottom it is a staircase.** SPEC
  long said "up to 8 GiB the curve is convex, the chord runs above it and
  cannot underestimate"; a measurement on a 1610 MiB volume landed 202 672 B
  above the chord and refuted that, and the point at 1536 MiB, taken later,
  confirmed it independently and worse: 233 472 B. `interpolation_bound`
  returned zero then, and the underestimate was covered by `MIN_SAFETY_BYTES`
  — the safety floor. It must not be touched: it is the only thing that fired
  there. The slopes of adjacent segments below 8 GiB go 0.215 %, 0.128 %,
  0.128 %, 0.324 % — `$LogFile` changes in steps at discrete thresholds, and a
  step can hide between any two points, however dense the grid is made.
- **`Calibration.json` holds two kinds of measurements, and a derived
  attribute tells them apart.** A calibration point is `is_calibration_point`,
  that is, a record with no data and no left space; everything else there is a
  copy-slack measurement. There is no separate field, by the same rule:
  computed values are not stored. Hence three places where a common pass over
  the list lies silently: `put_calibration` supersedes the previous
  measurement **of the same kind** (by `mounted_bytes` for a point, by
  `slack_key` for copy slack), while `_set_point_disabled` and
  `_disable_all_points` touch only points — a copy-slack measurement may have
  no factory counterpart for the same `n` at all, and "disabled" would mean a
  lost calibration.
- **The key of a copy-slack measurement is the file set, not the file count**
  (`records.slack_key`). The two sets with `n = 1` were made different in size
  on purpose, to compare them with each other; a key by `n` made them mutually
  exclusive, and on the first real run the 4 GiB measurement silently ate the
  64 MiB measurement along with its NTFS point. The same key decides "already
  measured" in the collection dialog.
- **The container for a file set must not land on `RECOMMENDED_MIB`.**
  `slack_step` adds a megabyte if it hits a row of the coverage table:
  otherwise the Use factory button in that row would disable the copy-slack
  measurement as well.
- **The safety fit is seeded with a constant, not with the field**
  (`fit_safety`). The field holds the previous answer, and seeded with it the
  advice took turns: 582 and 581 MiB for one and the same input. The fit
  repeats until an advice repeats and takes the largest one met — the only
  choice whose volume is advised no more than itself.
- **A profile other than NTFS 4 KiB calculates on its own measurements or
  refuses** (`solve_for_profile` → `Uncalibrated`). The models' defaults are
  NTFS 4 KiB numbers; on exFAT they would be a confident wrong answer.
- **The calculation's prediction is stored, not recomputed.** `predicted_mib`
  and `predicted_safety_mib` are the only exception to "computed values are not
  stored", and a deliberate one: the models change with every measurement, and
  recomputing after the fact would answer "what would I say today". The safety
  margin is stored separately, otherwise an underestimate covered by it is
  indistinguishable from an exact hit.
- **The golden solver table is reprinted only on purpose.**
  `tests/test_solver_golden.py` freezes the answer of `solve_container_mib`
  over a grid of payload size and file count, taken before the volume-profile
  change, and the quickest way to make it green again is to reprint it
  (`python -m tests.test_solver_golden --print`) — after which it guards
  nothing. What that change may legitimately move, and by how much, is written
  in the file itself; anything else is a regression, and the reason for a
  reprint goes into the commit message.
- **A file in a set is no shorter than a kilobyte.** NTFS keeps a file shorter
  than ~700 B right in its MFT record and allocates no cluster for it, and
  `Σ ceil(size / cluster)` overstates the space taken — the measured copy
  slack goes negative, and the record is rejected. For the same reason the
  file names are long: short ones would understate the growth of the directory
  index, and an underestimate is the only dangerous side here.
- **Residuals on the chart are computed by a leave-one-out check.** The
  piecewise-linear model passes exactly through its measurements: ordinary
  residuals would come out zero everywhere, and the chart would be a straight
  line at zero. Each point is predicted by a model built without it
  (`metadata_cross_check`). The sign matters: up means underestimate. The edge
  measurements are put into a separate series and hidden: without them the
  model extrapolates, and −433 MiB at the edge squashes everything else to
  zero. The legend brings them back, and the caption says so.
- **The axis tick step is the nearest one in the 1-2-5 series, not the next
  one up.** Rounding up on a 137 MiB range sends it from 20 straight to 50:
  instead of seven ticks, two remain.
- **A logarithmic axis in bytes is base two, one in counts is base ten.** Sizes
  here are multiples of a power of two, and decades would fall between them.
  A file count is never a power of two, and the series 1, 4, 16 reads worse
  than 1, 10, 100.
- **The shared X axis links only X.** The NTFS curve spans megabytes, its
  residuals kilobytes; a shared Y axis would collapse the residuals into a
  line. And `apply_x` does not send its own signal back — otherwise two charts
  would loop. For the same reason only the vertical line of the crosshair goes
  to the neighbor: the horizontal one would speak of a value the neighbor does
  not have.
- **The right button pans the chart, and Qt must not raise its own menu on
  it** (`PreventContextMenu`), otherwise the menu pops up on every pan. The
  menu is on a left double-click and on the middle button, full view on a
  right double-click.
- **The crosshair must not be in a saved image.** Screen and file are drawn by
  the same `_render`, and the `live` flag separates them: in a file the
  crosshair would mean something was measured there, when it is just the place
  where the cursor happened to be.
- **A data update does not reset the view** (`set_chart(keep_view=True)`):
  the zoom, the hidden series and the selected point. It resets only when not
  a single point is left in the frame; how many went out of the frame is
  written in the corner of the plot area. Staying silent is not an option
  here: "nothing changed" after a new measurement looks like a bug.
- **The chart window's layout is rebuilt whole** (`_rebuild_layout`), not
  patched in place: detaching, returning, reordering and switching the layout
  change the composition in the same way. Column: the charts right in the
  splitter; grid: rows of two, an odd last one full width. Chart heights are
  remembered only for the column: in the grid the splitter holds rows.
- **Each layout has its own window size**, just as a chart has its own height
  in the shared window and its own size in a separate one. The grid needs
  width, the column needs height.
- **Sizes are read from how the current splitter was built** (`_built_grid`,
  `_built_order`), not from what is already planned: otherwise the heights are
  assigned to charts that have already been reordered, and swapping places
  changes nothing. They are read before every rebuild: `splitterMoved` comes
  only from the mouse.
- **The grid's columns are shared by all rows.** The rows are independent
  splitters, and without broadcasting the widths a boundary moved in one row
  leaves the others as they were: the grid stops being a grid.
- **A button's `clicked` arrives with a boolean argument.** A lambda without a
  dummy first parameter receives it in its first name — the button is pressed
  and does nothing, silently.
- **The chart order is checked for its members, not its length.** The saved
  order may hold numbers from a former chart count, and the window would
  silently lose one of the charts.
- **A detached chart stays in the `views` list.** Updates, the unit and the
  shared axis go through the list, not by who lives in which window; it is the
  widget itself that moves, along with its zoom.
- **The Y axis label is measured against the widget's height, not the plot
  area's**, and wraps onto a second line. In a window with three charts the
  plot area is half as tall as its own label: «Измерено минус модель, B»
  ("Measured minus model, B") is 288 pixels with a plot area of 186. Space in
  the left margin is reserved by the line spacing, not by the font height.
- **Rotated text grows to the right of the rotation point.** After
  `rotate(-90)` the `y` coordinate turns into screen `x`: a rectangle with
  `-spacing` ends up left of the padding, and the second line of the label
  does not land in the widget at all. This is exactly what looks from outside
  like "the axis label is cut off".
- **The title band is measured from the widget's top edge.** With
  `PADDING - height` it starts above zero, cuts off the tops of the letters,
  and this is taken for the splitter between charts creeping in from above.
- **A logarithmic count axis can do fractional ticks too.** They went through
  `fmt_bytes(int(...))`, and "0,01" turned into "0": values below one never
  occurred there until the share of the volume taken by metadata.
- **The breakdown bar needs its own minimum height.** An explicit
  `setMinimumSize` beats `minimumSizeHint`, and the common 220-pixel minimum
  does not let it shrink to its own height; zero stretch in the splitter is the
  other half of the same rule.
- **A detached chart remembers two heights at once**: its share in the shared
  window (by chart number — the splitter's list gets shorter when one leaves)
  and the geometry of its own window. The shared window's height after the
  return cannot be computed by addition: the layout minimum stops it from
  shrinking, and nothing stops it from growing back.
- **The breakdown bar must add up to the container to the byte.** The sixth
  component is the rounding to whole MiB; without it the sum of the parts does not
  equal the whole, and the bar lies about the proportion. There is a test for
  this.
- **The chart window is rebuilt on a signal, not on show.** Otherwise after a
  new measurement it silently shows yesterday's picture, and there is nothing
  to tell it apart by.

### Safety property

`solve_container_mib` has no right to return a container smaller than needed.
Any change to `model.py` must leave `tests/test_model.py::SolverTests` green;
they are tied to three real measurements in `tests/reference.py`. Do not
weaken them to make a change pass.

Underestimate is the only dangerous side of the model. An overestimate costs
space, an underestimate costs data that did not fit.

## What the tests don't reach

The suite runs headless on synthetic data. These need a person, VeraCrypt and
a mounted volume: the VeraCrypt headers 262 144 B and the one-cluster NTFS
tail (measured only at 4 KiB; 327 680 B `container − capacity` is expected at
64 KiB), reading a real volume,
whether the prediction hits reality, the layout and readability of the window.

The copy-slack collection was run on a live machine twice: the first time
halfway (four file sets failed at unmounting), after the fixes in full, thirty
steps without a single failure. Results and conclusions are in the "What the
first copy-slack run showed" section of `SPEC.md`. In short: the NTFS metadata
was confirmed to the byte, the per-file slack is measured and stable (1363 B
per file on the series 500…10 000), and on the mixed set the model
underestimated by a megabyte, and it was saved not by the safety margin
calculation but by the safety floor.

What is still missing: measurements on another Windows build or another
VeraCrypt version. Everything known about copy slack and metadata is known
about one machine — this is what the factory margin `FACTORY_MARGIN_BYTES`
lives for.

## Automating calibration collection through the VeraCrypt CLI

Implemented: `veracrypt.py` → `collect.py` → `elevation.py` → the
`ui/collect_dialog.py` dialog, the Collect automatically… button on the
Calibration tab. The full description is the "Automatic collection" section of
`SPEC.md`; checked against the VeraCrypt 1.26.24 documentation
(`docs/html/en/Command Line Usage.html`).

What breaks silently if touched:

- **The exit code means nothing.** `/silent` is documented as "If there is any
  error, the operation will fail silently". The result is checked only by the
  facts: a file of the right size, a drive letter that appeared (by polling
  `mounted_drives()`), the letter gone after unmounting.
- **Size in exact bytes.** The `G` suffix rounds, but the target is
  `container_mib × 1048576` to the byte, otherwise the measurement lands off
  its row.
- **`/nosizecheck` is mandatory**, otherwise a terabyte dynamic container will
  not be created where there is no free terabyte. `/hash sha512` at mounting
  removes trying all the PRFs. `VeraCrypt Format.exe` has no `/pim` at all.
- **Unmounting is retried, not waited out, and `/force` only in cleanup.**
  VeraCrypt does not release a volume that has just been filled with files
  right away: the refusal comes immediately with a nonzero code, and half a
  minute later the volume unmounts without objection — this is how four
  measurements out of seven failed on the first real run. There is no point
  waiting a minute after a refusal; the command has to be repeated. `/force`
  before measuring the left space is forbidden: it may discard the cache, and
  the lost write will look like extra free space, that is, it will
  **underestimate** the measured copy slack. In cleanup it is allowed, but as
  the last attempt: VeraCrypt silently swallows an unknown switch, and a
  cleanup that started with force would not work at all on an old version.
- **The unmount switch depends on the version.** `/unmount` appeared in
  1.26.20; before it there was only `/dismount` — it still works, it is just
  declared deprecated. A version that could not be read counts as old.
  Getting this wrong is quiet: with `/silent` VeraCrypt says nothing on an
  unknown switch, the volume stays mounted, and the terabyte file stays on
  disk. Below 1.24 collection does not start at all: there is neither
  `/nosizecheck` nor `/quick` there.
- **Cleanup in `finally` plus a search for orphaned containers** by the name
  `containerhelper-calibration-*.hc`. Without it terabyte files pile up
  silently.
- **Self-check first.** 1 GiB as a dynamic container with quick format and as
  a normal one with full format. If they differ by more than a megabyte, stop:
  dynamic containers cannot be trusted on this machine. That the two measure
  indistinguishably was checked on overlapping sizes (12 GiB differed by
  8 KiB, 80 GiB by 3 KiB): the host sees the sparseness, and the filesystem
  inside the volume knows nothing about it.
- **Administrator rights** are needed because of `/filesystem NTFS`: without
  them UAC would ask for every container of the run — twenty-six sizes, the
  self-check and the file sets. The restart goes through `ShellExecuteW` with
  `runas` and a mandatory `--data <current folder>` — otherwise portability
  ends. After the restart the window closes: two copies in one data folder
  would write over each other.
- **The steps run in a separate thread.** Cancelling takes effect between
  steps and inside the writing of a file set — the half-written file set is
  thrown away with the container anyway. But breaking off a running VeraCrypt
  in the middle means leaving a mounted volume and a terabyte file, so
  creation is carried through to the end.
- **Space is computed by the same model that the collection calibrates.** A
  dynamic container takes up disk only with its written clusters: an empty
  terabyte volume costs 136 MiB, not a terabyte. The uncalibrated model
  overestimates this thirteenfold — an error on the safe side: a step will be
  skipped needlessly, but not started where it would run out of room. Free
  space is queried both before a step and while writing: an outside process
  may eat the disk, and writing into a dynamic container on a full disk
  breaks the volume.
- **Progress is measured in bytes, not steps.** An empty terabyte is measured
  in seconds, a four-gigabyte set is written over minutes — a bar by step
  count would be off many times over. The progress signal is throttled to ten
  times a second: on ten thousand files it would otherwise clog the event
  queue before the window gets to paint.
- **Left space is read on a freshly mounted volume.** The model predicts Left
  space — what VeraCrypt will show the person — so the copy-slack step
  unmounts the volume and mounts it again before reading.

The run took place: 31 August 2026, VeraCrypt 1.26.24, twenty-two sizes in
3 minutes 10 seconds, all twenty-two matched the manual measurements **to the
byte**. The manual ones were taken on the same machine and the same version,
so this is a check of the automation, not of the numbers carrying over between
machines.

The tests still run the whole path on substituted `subprocess` and volume
reading: they check the commands and their order, but not VeraCrypt itself.
