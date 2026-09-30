# Specification: VeraCrypt container size calculator

## Goal

One application solves two tasks:

1. From the size of the source file, calculate what container size (MiB) to set in VeraCrypt.
2. Accumulate measurements of real containers, to refine the calculation where
   it has not been checked yet. When the work began only 8–12 GiB was
   calibrated. Now the program ships 26 factory empty-volume measurements from
   512 MiB to 1 TiB and 7 copy-slack measurements, but all of them come from
   one machine; measurements on another Windows build or VeraCrypt version are
   still missing.

Everything else is out of scope.

## Out of scope

Listed explicitly so that it does not come back during further work:

- copying of **user** files by the application — the free-space measurement does
  not depend on it, see the section "Measurements". The qualifier "user" came
  later: automatic collection of copy slack generates its own file set right on
  the calibration volume. That is not the same thing — there is no choice of
  sources and no moving of someone else's data, but a measuring instrument that
  needs a set known to the byte;
- themes and keyboard shortcuts;
- advanced statistics (medians, box plots, CDF, correlations, slices by type);
- reverse calculation of the overhead percentage — percentage is not used as
  a concept, and there is no chart in percent either: a percentage of what
  exactly, of the data or of the container, is a question without a good
  answer;
- export/import and filtering of records;
- the scenario of enlarging a container with VeraCrypt Expander.

"Charts of any kind" stood here for a long time as well. The ban was lifted
deliberately and not entirely: only calibration quantities and the breakdown of the current calculation are drawn, and
exactly where a number by itself explains nothing. The `$LogFile` steps are
that case: the SPEC argues about them in half a page of prose ("the 1610 MiB
point landed 202 672 B above the chord"), while on the residual chart it is
visible at a glance. Advanced statistics remain banned: that is a different
activity from showing twenty-six measurements and the polyline through them.

## Calculation model

The quantities a container is made of:

| Component | Behavior | Source |
|---|---|---|
| VeraCrypt headers | constant 262 144 B | VeraCrypt's volume format; see below |
| NTFS metadata | `ntfs_base + ntfs_rate × V`, the filesystem tail included | calibrated from records |
| Payload | `Σ ceil(size_i / cluster) × cluster` | computed |
| Copy slack | `slack_base + slack_per_file × n` | calibrated from records |
| Safety margin | chosen for the size and the file count | see "Safety margin" |

### VeraCrypt headers and the filesystem tail

The volume size is computed, not measured: `V = container_bytes − 262 144`.
The 262 144 B are VeraCrypt's own — 128 KiB at the start of the container (the
header and the hidden volume's header) and their 128 KiB backup at the end —
and they are the same whatever filesystem the volume gets.

The measured volume capacity (`mounted_bytes`) is smaller than `V` by the
**filesystem tail**. NTFS with a 4 KiB cluster keeps exactly one cluster back:
on three containers, 8050, 10475 and 11130 MiB, `container − capacity` came out
266 240 B to the byte, and twenty-two sizes of the first automatic run agreed.
The tail counts as metadata — it is space the empty volume does not give to
files — so the metadata is `V − empty free space`, not `capacity − empty free
space`.

For a long time the tail sat inside a single header constant of 266 240 B.
That held only while every volume was NTFS with a 4 KiB cluster, and it held
the wrong things together. Other filesystems measure their capacity
differently — FAT does not count its tables into it — so metadata derived from
the capacity would not compare between them, while `V` does. And on another
cluster size the "header" came out unusual, and that alone, not a deliberate
rule, kept such records out of the calibration. The split changes no answer on
NTFS with a 4 KiB cluster: both axes of every calibration point grow by the
same 4096 B, the interpolation does not move, and the frozen solver table
(`tests/test_solver_golden.py`) holds every container size and safety margin.

That the tail is one cluster for other NTFS cluster sizes as well is expected
(327 680 B `container − capacity` at 64 KiB) but not measured yet.

### NTFS metadata

**By default** (fewer than two records): `NTFS = 19 MiB + 4 KiB + 0.17 % × V`,
where `V` is the volume size. The largest underestimate on the three original
records (Cache 1, 2 and 4) is 554 330 B (0.53 MiB), on Cache 1. The 19 MiB were
fitted while the filesystem tail was counted in the header; the 4 KiB is that
tail, moved with it.

**Calibrated** (records ≥ 2): piecewise-linear interpolation over the measured
points `(V → NTFS)`, sorted by `V`. This form needs no statistics and correctly
catches the fact that the dependence is not proportional: `$LogFile` barely
grows with the volume and hits a ceiling of 64 MiB, and only `$Bitmap` grows
linearly.

**Outside the measured range** the slope is not the fitted one but the baseline
one (`ntfs_rate` from the default model), anchored to the nearest measured
point. A fitted slope outside the data is unreliable: measurements often sit
close together, and their local slope, carried many times further than its own
span, misses by an order of magnitude. On the three original records the
leave-one-out check gave 10.8 MiB of underestimate this way, against a safety
margin of 4 MiB; with the baseline slope, 2.5 MiB.

### Safety margin

Not one number for all calculations. A common "largest miss across all
records" is a useless quantity: it is taken where the model is weakest, and it
makes a 5 GiB container pay for the curve breaking around 48 GiB.

The advice is computed for the specific volume and file count and is made
of two independent parts.

**NTFS** — the larger of two is taken:

- the *interpolation bound* at this very point. Between measurements the model
  draws a straight line, and the dependence is not a straight line. Where it is
  concave — above 16 GiB, because the metadata components hit their ceilings
  one after another — the chord passes below the curve, and that gap is the
  risk. Where it is convex, the chord runs above. The bound only guesses the
  shape between two points from the neighbouring segments: it assumes that
  from above the curve stays under two straight lines, one from the left end
  of the segment with the slope of the previous segment, one from the right
  end with the slope of the next; the nearer of them minus the chord gives the
  bound. Past the last segment the slope is taken as zero — cautious and close
  to the truth, only `$Bitmap` grows there. A zero from the bound is therefore
  not a promise that the chord cannot underestimate: below 8 GiB the curve
  turned out to be a staircase, and the guess failed there twice.

  **This used to say "up to 8 GiB the curve is convex", and that turned out to
  be wrong.** At the anchor points 0.5, 1, 2 and 4 GiB the slopes do grow
  (0.128 %, 0.172 %, 0.226 %), so the convexity is visible. But the measurement
  on a 1610 MiB volume, in the middle of the 1024…2048 segment, landed
  **202 672 B above the chord**: with it, the slopes on the same stretch read
  0.205 % and 0.128 %, that is, locally concave. The interpolation bound at that
  point returned zero — "the model does not underestimate here, the chord runs
  above" — and the underestimate was covered not by the calculation but by the
  safety floor `MIN_SAFETY_BYTES`.

  Conclusion at the time: the curve is neither convex nor concave as a whole,
  it is wavy, and points an octave apart tell nothing about its shape between
  them. A zero answer from the bound is a claim about the shape that the grid
  does not support. Hence two measures: do not touch the safety floor under
  any circumstances (it is what saved the day), and make the grid denser where
  the curve still rises steeply — see `RECOMMENDED_MIB`.

  The denser grid confirmed the finding, and worse: the new point at
  1536 MiB, in the same 1024…2048 segment, lay **233 472 B above the former
  chord**. Both stay within the 1 MiB safety floor. The curve below 8 GiB is
  not wavy but a staircase: `$LogFile` changes its size in steps at discrete
  thresholds, and a step can hide between any two points, however dense the
  grid. So the safety floor stays mandatory even where the grid is dense — see
  "Implementation order", item 11;
- the *miss on the two records between which the requested size lies*, scaled to
  the width of this segment. The miss is taken from the leave-one-out check —
  only it shows how the model behaves where there was no point. But it measures
  the model without one point, that is, with a gap about twice as wide as the
  real one, and taking it as is means going back to the same common miss: it
  does not shrink with new measurements, because each new one is immediately
  left out. The error of linear interpolation grows as the square of the gap
  width, so the miss taken on a gap `h_loo` is rescaled to the real segment `h`
  by the factor `(h / h_loo)²` and additionally weighted by the position inside
  the segment (zero at the ends, one in the middle): at the measurement itself
  the model is exact.

"Records of a similar size" means exactly the ends of the segment the size
falls in. A window by size ratio pulled in second-nearest neighbours: next to
90 GiB it picked up a record at 48 GiB with a knee, and its miss drifted into a
region where, by the measurements, only the bitmap grows.

**Copy slack** — the miss on measurements with a similar file count, within a
factor of eight (`SLACK_NEIGHBOUR_RATIO`). The factory copy-slack measurements
span 1 to 10 000 files, so this covers up to 80 000. Beyond that, or with no
copy-slack measurements at all, a fallback is at work, and it too depends on
the input: for a single file the safety floor is taken (the constant part is
measured and small), for a folder the full default value, because nothing
nearby confirms the per-file slack.

Outside the measured range there is no local evidence at all, and the largest
underestimate across all records goes there — deliberately cautious.

The total is rounded up to whole MiB and never goes below 1 MiB: the free-space
measurement itself is slightly noisy.

The advice depends on the volume, and the volume on the margin, so the two
are solved together (`fit_safety`): solve with a margin, advise for that
volume, solve again with the advice, until an advice repeats. The seed is a
fixed 4 MiB, not the field. Seeded with the field — the previous answer — one
input got two answers in turn: at 547 MiB in 10 000 files the volume a 4 MiB
margin gives is advised 5 MiB and the volume a 5 MiB margin gives 4 MiB, and
Container init walked 582 ↔ 581 on every recalculation. Where the advice
alternates like that, the largest advice met is taken. Every advice met has
also been tried, so the volume it gives is advised no more than itself: the
margin is enough at the very size it produces. A cap of eight rounds guards
against a loop; over 4134 inputs from 1 MiB to 1 TiB none needed more than
three. The collection's prediction for a file set is fitted
the same way.

The Auto toggle on the Calculation tab turns auto-selection on; with it
off, the safety margin is set by hand, as before. With auto-selection on, the
field on the Model tab only shows the result.

### Copy slack

Files appearing on a volume take space beyond their cluster-rounded size: an MFT record
per file, growth of the directory indexes, housekeeping structures of the first
write.

```
copy_slack(n) = slack_base + slack_per_file * n
```

where `n` is the number of files (for a single file `n = 1`).

**Default:** `slack_base = 196608` (192 KiB), `slack_per_file = 1536`.
Both parts are cautious, but for different reasons.

The per-file slack is **measured**: on series of 500, 5 000 and 10 000 files
the rate came out at 1363 B per file and held within two bytes. It breaks down
physically — 1024 B for the MFT record plus ~339 B for the directory index
entry — and the default takes it with headroom: the second half depends on the
file name length, and the measurements were taken on 34-character names, while
in real data names are sometimes twice as long.

The constant part stayed as it was, although the synthetic measurement at
`n = 1` gave only 4096 B — one cluster. The discrepancy with the manual records
(143 360 B for Cache 1 and 114 688 B for Cache 4, both at `n = 1`) is not
explained: they were copied with Explorer, not written by a program, and what
exactly Windows adds around a real copy is unknown. The user copies with
Explorer, so the larger of the two is kept here: 192 KiB costs nothing next to
a safety margin of megabytes, and underestimate is the only dangerous side.

**Why one record is not enough.** `CopySlackModel.calibrate` takes the slope by
least squares only with two or more *distinct* `n`; with a single distinct
`n`, `slack_per_file` stays at its default value, and `slack_base` is fitted to
it. Two points are formally enough, but the dependence on `n` is not a straight
line all the way, and a slope from two points would be random.

How far from straight it is has now been measured:

| n | Slack | B per file |
|---|---|---|
| 1 | 4 096 | 4096 |
| 50 | 16 384 | 328 |
| 500 | 692 224 | 1384 |
| 5 000 | 6 819 840 | 1364 |
| 10 000 | 13 635 584 | 1364 |

The first few dozen files are almost free, after that the cost levels off at a
plateau. A plausible explanation: a freshly formatted volume already holds an
allocated `$MFT` with spare records; while the files fit there, free space does
not decrease, and as soon as the MFT has to be extended, linear growth begins.
This is an explanation, not an established fact. In practice it does not
matter: at small `n` the linear model overestimates many times over, but there
it is a matter of tens of kilobytes, and where the quantity matters, it is
exact.

Hence the automatic collection of copy slack: several deliberately different
`n` in a row, on one machine, in minutes. See "Automatic collection of copy
slack".

**Calibration:** from records where `file_bytes`, `left_bytes` and
`file_count` are filled in:

```
copy_slack_measured = (empty_free_bytes - left_bytes) - F_alloc
```

- Records with two or more distinct `n` — least squares on both
  coefficients.
- All records with the same `n` — `slack_per_file` stays at its default value,
  `slack_base` is taken as `max(measured - slack_per_file * n)` over all
  records (the upper envelope, on the safe side).
- No records — the default values.

### Solving for the container size

Iteratively; three iterations are enough — `NTFS` changes slowly:

```
F_alloc = Σ ceil(size_i / cluster) * cluster
slack   = slack_base + slack_per_file * n
C = ceil((F_alloc + 262144 + NTFS(estimate) + slack + safety) / 1048576)
repeat: V = C * 1048576 - 262144;  NTFS = model(V);  recompute C
```

Check of the default model on the existing records (the true minimum is taken
from the measured Left space):

| Record | F (B) | Calculation | Set by hand | True minimum |
|---|---|---|---|---|
| Cache 4 | 8 374 112 625 | 8024 | 8050 | ~8020 |
| Cache 2 | 10 941 734 967 | 10477 | 10475 | ~10471 |
| Cache 1 | 11 553 254 233 | 11061 | 11130 | ~11057 |

## Data

Two JSON files in one folder. The path to the records is chosen by the user and
remembered; the measurements file always lies next to it and is always called
`Calibration.json`. Before each of them is overwritten, one `.bak` backup copy
is made.

```json
// Records.json — copy records
{
  "schema": 6,
  "records": [
    {
      "id": "Cache 1",
      "created": "2026-08-29T12:00:00",
      "container_mib": 11130,
      "mounted_bytes": 11670384640,
      "empty_free_bytes": 11630067712,
      "cluster_bytes": 4096,
      "file_bytes": 11553254233,
      "file_count": 1,
      "left_bytes": 76668928,
      "filesystem": "NTFS",
      "note": ""
    }
  ]
}
```

```json
// Calibration.json — empty-volume measurements
{
  "schema": 6,
  "calibration": [
    {
      "id": "Калибровка 1 GiB",
      "created": "2026-08-29T16:39:27",
      "container_mib": 1024,
      "cluster_bytes": 4096,
      "mounted_bytes": 1073475584,
      "empty_free_bytes": 1055596544,
      "filesystem": "NTFS"
    }
  ]
}
```

**Only measured quantities are stored.** Everything else is computed on read
and never written to the file:

```
container_bytes     = container_mib * 1048576
volume_bytes        = container_bytes - 262144
tail_bytes          = volume_bytes - mounted_bytes
metadata_bytes      = volume_bytes - empty_free_bytes
consumed_bytes      = empty_free_bytes - left_bytes
payload_alloc       = file_alloc_bytes, otherwise ceil(file_bytes / cluster_bytes) * cluster_bytes
copy_slack_measured = consumed_bytes - payload_alloc
minimum_mib         = ceil((volume_bytes - left_bytes + 262144) / 1048576)
miss_mib            = predicted_mib - minimum_mib
model_miss_mib      = miss_mib - predicted_safety_mib
```

There is exactly one exception to the rule, and it is deliberate:
`predicted_mib` and `predicted_safety_mib` **are stored**, although they look
computable. They cannot be restored after the fact: both models change with
every new measurement, and a recalculation would answer "what I would say
today", not "what I said then". This is a measurement of the model at a moment
in time, not something derived from the record. More in the section "Checking
the prediction after the fact".

This rule is mandatory. In the existing manual records it was exactly the
duplication of computable fields that produced contradictions: in Cache 2 the
NTFS field is written as `36 573` instead of `36 573 184`, and the Left space is
physically impossible.

Only `container_mib`, `mounted_bytes` and `empty_free_bytes` are required for
the NTFS calibration. The fields `file_bytes`, `file_count`, `file_alloc_bytes`
and `left_bytes` are filled in after the actual copying; they give the
calibration of copy slack and a check that the calculation did not miss. A
record without them is a full point for the NTFS model.

`file_alloc_bytes` is `Σ ceil(size_i / cluster) × cluster`, taken by walking
the folder. It too is measured, not computable: it cannot be restored from the
sum of logical sizes, because `ceil(Σ size_i)` is smaller than
`Σ ceil(size_i)`, and for a folder of many files the difference is large — on
500 files of a kilobyte each it is 1.4 MiB, which would otherwise all end up in
copy slack. For a single file the field may be left empty: there the derivation
from `file_bytes` is exact.

Schema 2 added this field to schema 1, schema 3 added `filesystem`, schema 4
the top-level `calibration` key, schema 5 moved this key into a separate
file, and schema 6 writes `filesystem` always: a record whose filesystem was
not read — typed in by hand, or older than schema 3 — is written as NTFS, so
the file itself says which profile each record calibrates. The version goes
up although a schema 5 reader would parse the file: that reader would also
feed an exFAT or an 8 KiB copy record into the NTFS copy slack, and a data
folder saved by this version is not handed back to it. Old files are still read: a missing field means exactly the old behavior,
and the empty-volume measurements move first from `records` to `calibration`
(schemas before the fourth), and from there into their own file. The move is
written right away on open: while the records file holds a second copy, an edit
of a measurement would go to one file and reading to the other. A missing
`filesystem` reads as NTFS: every record from before it was read was taken on
NTFS. Schema 6 is always written.

**The files are separate because these are different quantities with different
lifetimes.** Copy records describe the data and move together with it.
Empty-volume measurements describe the machine — the Windows build and the
VeraCrypt version — and are wrong on another machine; the size of the metadata
is decided not by NTFS in general but by the specific formatting code. In one
file they had to travel together, and someone else's calibration arrived
disguised as one's own.

The name of the measurements file is fixed, not derived from the name of the
records file: two records files in one folder share one calibration, because
the machine is the same for both.

Both files lie in one folder and both belong to one `Store`: the calculation
rests on them together, and there would be nowhere to read them separately. An
empty measurements file is not created — on a new machine there are no own
points at all, and there is no reason to put a dummy next to the records; a
file that has become empty is rewritten, otherwise deleted measurements would
come back on the next read.

`filesystem` is read from the volume during measurement via
`GetVolumeInformationW`, together with the cluster size via
`GetDiskFreeSpaceW`. Neither is asked of the user, because the volume already
exists and its properties are known exactly: a typo in the cluster would
distort copy slack and would be caught by nothing.

**The volume profile** — the filesystem plus the cluster size
(`VolumeProfile`, `profile_of`) — is what a measurement calibrates. Both
models are built from one profile's measurements, and everything that decides
"the same measurement" works within a profile: superseding, disabling, a size
covered by an own point, the factory data that fills in the rest. The name is
the one VeraCrypt uses: Windows reports FAT32 (or FAT on a small volume), and
both are the profile FAT; an unread filesystem is NTFS, and an unread cluster
on NTFS is 4 KiB (on other filesystems the default cluster depends on the
volume size, and an unread one stays 0). Until the profile can be chosen the
program calculates for NTFS with 4 KiB, the only profile measured so far; the
others' measurements stay in the files, but no model, chart or table shows
them yet. Before schema 6 only the metadata was guarded, by two checks on
save that flagged a record of another filesystem or another NTFS cluster; a
copy record on exFAT or with another cluster went into the NTFS copy slack.
Mixed in, it pulls the NTFS per-file slack down: exFAT spends a fraction of
NTFS's bytes per file, and underestimate is the dangerous side. The profile
made both checks pointless and they are gone: a check keeps a record out of
every calibration, the profile only out of the others', and an exFAT
measurement is exactly what the exFAT calibration will be built from.

The volume picker names the profile when it is not NTFS with 4 KiB: the
measurement goes into that profile, and the calculation does not use it yet.
Before, it said such a measurement would not go into the calibration at all.

**A profile calculates on its own measurements or refuses**
(`solve_for_profile`). The models' defaults — `19 MiB + 4 KiB + 0.17 %` of
the volume, 192 KiB plus 1536 B per file — are NTFS with 4 KiB; on another
profile they are not a cautious guess but a guess about another filesystem
(exFAT with a 1 MiB cluster spends a whole cluster on a directory where the
default budgets 192 KiB). So a profile without two metadata points, or
without a per-file copy slack fitted from its own measurements, gets a refusal
that names what is missing (`Uncalibrated`, with the scopes `metadata` and
`slack`), not a number: a number wrong for this filesystem looks exactly like
a right one. The fit takes two different file counts and a positive slope
across them; with one count, or a flat slope, the per-file slack would be the
NTFS default again. The payload must be rounded to the profile's cluster:
rounded to 4 KiB and solved on 32 KiB it would lose up to 28 KiB per file. NTFS with 4 KiB never refuses — the factory data
calibrates it. The volume without a filesystem (`NO_FILESYSTEM`, see
"Filesystem and cluster size") needs no calibration at all. The choice of
profile is not in the window yet; the refusal is there for when it is.

**A calibration point** is not marked by a separate field; the flag is derived:
a record without `file_bytes` and without `left_bytes` is an empty-volume
measurement, nothing was put into the container.

**`Calibration.json` holds two kinds of measurements.** Besides calibration
points, the copy-slack measurements taken by automatic collection live there
too. Both describe the machine, not the data, and are equally wrong on another
machine — so they must move (more precisely, not move) together. The schema
version does not change because of this: the shape of the file is the same — a
list of records — only which of their fields are filled in changes.

They are told apart by the same derived flag: a copy-slack measurement has both
data and left space, so it does not count as a calibration point. No separate
field is added, by the general rule — what is computable is not stored.

Consequences, each of which would otherwise break silently:

- **superseding is separate.** A new point replaces the point at the same
  `volume_bytes`, a new copy-slack measurement replaces the measurement **of
  the same set** — both only within the profile: an exFAT point on a volume
  already measured on NTFS is another curve's node, not a retake. A common key on `volume_bytes` would knock out an NTFS point
  with a copy-slack measurement taken at the same volume size, and vice versa;
- **the key of a copy-slack measurement is the set, not the file count.** Two
  sets with `n = 1` were made different in size on purpose, to compare them
  with each other. A key on `n` made them mutually exclusive: on the first real
  run the measurement «Один файл 4 GiB» ("One file 4 GiB") silently ate
  «Один файл 64 MiB» ("One file 64 MiB") together with its NTFS point, so the
  supersede rule destroyed exactly the reconciliation both sets exist for. A
  measurement taken by hand has no set, and it is still identified by the file
  count: two manual measurements at the same `n` are two measurements of one
  and the same thing;
- **disabling touches only points.** The Disable all own measurements and Use
  factory buttons go over the calibration points. A copy-slack measurement may
  have no factory measurement for the same file count at all, and "disabled"
  would mean losing the copy-slack calibration without a single word about it.
  A copy-slack measurement is not disabled but deleted — with a button in its
  own row;
- **the container for a set is moved off the recommended sizes.** If the
  computed `Container init` coincides with a row of the coverage table, a
  megabyte is added to it. Otherwise the Use factory button in that row would
  disable the copy-slack measurement as well.

**`fileset`** is the key of the file set, if the data was generated by
automatic collection. Empty means real copying. The distinction is needed: a
set describes the machine, not the user's data.

Numbers in JSON are written without digit-group separators.

## Measurements

The application does not copy files. Free space is read from the volume
regardless of what it was filled with, so a single Measure volume button covers
both cases:

- pressed on an **empty** mounted volume → fills in `mounted_bytes` and
  `empty_free_bytes`;
- pressed **after copying** (with Explorer, robocopy, anything) → fills in
  `left_bytes`.

The application itself determines which field is filled in: if the record does
not have `empty_free_bytes` yet, the first measurement goes there, otherwise
into `left_bytes`. Manual input remains available for both fields.

## Checking the prediction after the fact

The program exists for the sake of one number — `Container init`. Everything
else — the models, calibration, the safety margin — serves it. This number can
be checked in only one way: write down next to the record what the calculation
promised, and compare it with what came out on a real volume.

**The minimum sufficient container** is derived from the record's
measurements. The space taken on the volume is `volume_bytes - left_bytes`,
and it already includes everything: NTFS metadata with the filesystem tail,
cluster-rounded data and copy slack. Add the VeraCrypt headers and round up to
MiB — that gives the
`Container init` that would have been just enough.

The estimate is slightly high: a smaller container would give a smaller volume,
and that volume would have less metadata, so the real minimum is a hair lower.
The error goes toward overstating the minimum, that is, the miss comes out
smaller than the real one — the metric errs toward alarm rather than
complacency, and that is the right side. On the three manual records the
quantity matches what was once calculated by hand: Cache 1 → 11 057,
Cache 4 → 8020 MiB.

**What the calculation promised cannot be computed after the fact.**
`solve_container_mib` depends on the models, and they change with every new
measurement. A recalculation today would answer "what I would say now", not
"what I said then". So the promise is stored in the field `predicted_mib`; it
is filled in automatically by the Take from Calculation button, and automatic
collection of copy slack writes it with no human involvement at all.

```
miss       = promised − minimum sufficient
model miss = miss − safety margin
```

Plus is overestimate, zero is an exact hit, **minus means the data would not
have fit**. The whole thing is computed for the sake of the last case:
underestimate is the only dangerous side of the calculation, overestimate costs
only space.

**The safety margin is stored in a separate field** `predicted_safety_mib`,
because without it the miss is ambiguous. A miss of +5 MiB looks the same in
three different stories: the model is exact and the 5 MiB were the safety
margin; the model overestimated by 5 MiB with a zero safety margin; the model
**underestimated** by 3 MiB, but 8 MiB of safety margin hid it. The third is a
precursor of failure that looks healthy. The model miss separates them and is
directly comparable with the deviations in the leave-one-out check on the Model
tab.

Both quantities are shown as the columns «Промах, MiB» ("Miss, MiB") and
«Промах модели, MiB» ("Model miss, MiB") on the Records tab and in the table of
copy-slack measurements on the Calibration tab. Underestimate is highlighted in
red: it must be visible without hovering the mouse.

**The checks on save deliberately do not cover them.** None of the models rests
on them, and a failed check flags the record and throws it out of calibration
entirely — the cost is out of proportion to the benefit. A meaningless value
will give a meaningless miss in one column, and that is all.

## Checks when a record is saved

Each check catches a real class of error:

1. `container_mib > 0`, and `mounted_bytes <= volume_bytes` — otherwise the
   filesystem tail is negative. On NTFS with 4 KiB (an unread filesystem is
   NTFS, an unread cluster there is 4 KiB) one more, for the metadata model
   only (scope `metadata`): a tail other than exactly one cluster — a typo in
   `container_mib` or `mounted_bytes` gives itself away here. On other
   profiles the tail is not checked: one cluster at other NTFS cluster sizes
   is expected, not measured, and what exFAT and FAT keep back is not known.
   A check that fired on a correct measurement would throw it out of its own
   profile's calibration.
2. `empty_free_bytes < mounted_bytes`.
3. On NTFS with 4 KiB: `volume_bytes - empty_free_bytes` from 1 MiB up to
   the larger of 128 MiB and 2 % of `volume_bytes` — a rough filter for typos
   that drop or add digits. The ceiling grows with the volume: on a terabyte
   the metadata is 136 MiB (factory point), past any fixed 128 MiB, and a
   fixed ceiling would raise false alarms. Other profiles are not checked,
   for the reason of check 1: a small exFAT volume may well spend less than
   a mebibyte.
4. If `file_bytes` and `left_bytes` are filled in:
   `empty_free_bytes - left_bytes >= ceil(file_bytes / cluster_bytes) * cluster_bytes`.
   The space taken cannot be less than the file itself. This check would have
   rejected Cache 2.
5. If `file_count` is filled in: `file_count >= 1`, and with `file_bytes > 0`,
   `file_count <= file_bytes`.
6. If `file_alloc_bytes` is filled in: it is not less than `file_bytes`.
   Rounding up cannot reduce the size.
7. If `file_alloc_bytes` is filled in: it is a multiple of `cluster_bytes`.

Another filesystem or another cluster is not a check: the volume profile
keeps such a record out of the NTFS 4 KiB models (see "The volume profile").
A record flagged by the former checks keeps its `flagged`: the flag is what
the person chose on save, not a computed value, and it is lifted by saving the
record again, when no check fires any more.

A violation is a warning with the option to save anyway, but such a record is
flagged and excluded from calibration.

### What a field rejects outright

The checks above are about the plausibility of the numbers, and they fire on
save. A letter in a byte field is not the same thing: it is a slip of the
finger, not "a value that did not parse", and it has to be caught on input.
Previously `parse_bytes` silently returned `None`, the field kept the typed
garbage, and the computed quantities turned into dashes — without a single
word about what exactly was wrong.

So every numeric field has a validator that accepts **digits and those
digit-group separators that `parse_bytes` throws away anyway**: the regular
space, the non-breaking space, the narrow space and the underscore. The
character set of the validator and of the parser is one and the same
(`formatting.IGNORED_IN_INPUT`): if they diverged, the field would accept what
the parser does not understand, or, conversely, reject what was pasted from
Explorer, and that could only be noticed by hand.

A paste goes through the same validator as a whole, and "11599081472 B" is
rejected as a whole, letter included: corrupted text does not get into the
field that way either.

The name and the note accept anything except control characters, and are
limited in length. Control characters get there by pasting from foreign text,
go into JSON escaped, and then cannot be found by eye either in the file or in
the table.

## Screens

Four tabs, no more are needed. Their order is the order of work: calculate,
record, look at the model, fill in the missing measurements.

**1. Calculation**
- **One picker button for files and folders at once**, plus manual entry of the
  size in bytes and of the file count. There is no split into "pick a file" and
  "pick a folder": it was not the user's choice but a retelling of the fact
  that the native Windows dialogs are built on two different system calls. A
  data set does not divide that way — people put both folders and separate
  files into a container, usually together.

  So the dialog is not native but our own (`QFileDialog` with
  `DontUseNativeDialog`, mode `ExistingFiles`): it has one list, and that list
  shows both files and folders. Exactly one thing is changed in it: «Выбрать»
  ("Select") on a folder does not go inside but returns the folder as a
  source; a double click still goes inside. Ctrl and Shift select several
  items, files and folders mixed.
- **In the dialog the mouse selects with a rectangle.** On its own views
  (`listView`, `treeView`) Qt sets `DragDropMode.InternalMove` with
  `dragEnabled` on, and dragging the mouse starts drag-and-drop instead of a
  selection rectangle — selecting several names with the mouse is impossible
  altogether, only Ctrl and Shift remain. Drag-and-drop is turned off for both
  views. Not for the sidebar: bookmarks are put there by dragging, and there it
  is work, not a nuisance. The views are taken by name, not by iterating over
  all `QAbstractItemView`s: the iteration would also catch the dialog's
  drop-down lists.
- **Three selection buttons** — «Выделить всё» ("Select all"),
  «Снять выделение» ("Clear selection"), «Инвертировать» ("Invert") — both in
  the dialog and in the sources table: the set is edited by selection in both
  lists, and identical behavior does not have to be memorized. Inversion is
  done with `Toggle` over the whole rectangle in one call, not by walking the
  rows: `selectRow` without Ctrl held clears the previous selection, and only
  the last row would be left of the inversion.
- **Hidden files are shown by a check box in the dialog**, not by the Explorer
  setting. The system setting does not fit here: it is about what a person
  wants to see while sorting through a disk, not about what they put into a
  container, and it changes outside the program, so the contents of the list
  would change by themselves. The check box is stored by the window in
  `settings.ini` — the dialog lives for one showing.

  It is toggled **by the same action** as the native "show hidden" item in the
  list's context menu: two independent paths would drift apart, and the check
  box would say one thing while the list showed another. The action is found
  not by its name (that is translated) but by a trait: of the actions whose
  parent is the dialog itself, exactly one is checkable. The check box listens
  to its `triggered`, not `toggled`: `toggled` arrives before the dialog
  updates the filter, and the check box, still seeing the old state, would
  toggle the action a second time — a click in the menu would do nothing.

  It has no effect on the calculation at all: inside a selected folder hidden
  files are always counted, `os.scandir` does not set them apart. The check box
  only decides whether a hidden file or folder can be picked as a top-level
  source.
- **The dialog survives its showing whole**: the view (list or detail), the
  column widths of the detail view, the sidebar, the window size, both check
  boxes and the shown folder. All of this goes into `PickerState` and is stored
  by the window in `settings.ini` — the dialog itself lives for one showing,
  and a setting left in it would not survive even Cancel. The view and the
  sidebar come from `QFileDialog.saveState()` in one piece.

  **The size is two numbers, not `saveGeometry`.** `restoreGeometry` compares
  the width of the screen the geometry was saved on with the current one and,
  if they differ by more than a quarter, **returns false without doing
  anything**. Then QDialog sees that nobody has set the size and fits the
  window to its contents — from the outside this looks like "the size resets
  on every launch". Two numbers go through no such checks.

  The size is set in `showEvent`, not in the constructor: on show QDialog fits
  the window by itself if it thinks nobody has set its size.
- **It opens where it was showing, not where something was selected.** The
  initial folder used to be taken from the first selected path, and after
  selecting folder 2 in folder 1 the next showing went inside folder 2 — one
  level deeper every time, until it ran into an empty one. The next thing to
  pick lies next to what was selected; inside it there is nothing left.

  So the folder remembered is **the one the dialog was showing**. It is asked
  of the list itself (`model.filePath(view.rootIndex())`), not of
  `directory()`: on "My Computer" that returns not the list of drives but the
  working directory of the process — remembering it would mean remembering
  someone else's place. `directoryEntered` does not fit either: a programmatic
  change of folder does not emit it at all.
- **The «Выбрано» ("Selected") line is written by us from the selection, and
  Select takes the list.** The line is held by `QFileDialog` itself, and it is
  also what the dialog answers `selectedFiles()` from. Of its own accord it
  puts only files there — a folder it considers not a choice but a way further
  in — and it answers every edit of this line with autocompletion: it selects
  in the list what is written and deselects what is not in the line.

  Hence three troubles at once: after the selection was cleared, the previous
  names stayed in the line, and Select added files that nobody had selected
  any more; Select all immediately lost all folders; a second inversion in a
  row already worked on a different set from the one shown.

  So the list became the source of truth (`selected_paths()` asks the view
  itself for the selection), and the line is written by us — whole, with
  folders, and **with signals blocked**, so as not to wake autocompletion.
  While we edit the selection ourselves, the line is silent too: otherwise the
  dialog would rearrange the selection in the middle of the operation.

  The Select button has to be enabled by us: the dialog does it on an edit of
  the line, and our line is silent.

  A name typed by hand still works in the line: the person's edit is visible by
  the `textEdited` signal, and only after it does `accept` look at the line.
  This mark cannot be done without: the folder could have been re-read, the
  selection cleared and the names left in the line — and Select would return
  them. An empty line with an empty selection does not work either:
  `selectedFiles()` then returns the folder itself.
- **The «Запоминать папку» ("Remember folder") check box**; turned off, it means
  "My Computer", the list of drives. The program's folder does not fit as the
  initial one: the data lies anywhere but next to it, and a path from there
  always begins by going up. "My Computer" is `setDirectory("")`: it has been
  checked that for an empty folder QFileDialog shows exactly the drives.

  The shown folder is remembered in any case but used only when the check box
  is on: otherwise turning it on in the middle of a session would open the
  dialog where it was closed last time **before** it was turned off.
- Three actions on the set: «Выбрать…» ("Select…") replaces it, «Добавить…»
  ("Add…") adds to it (the dialog shows only one folder at a time, and sources
  come from different places), «Убрать» ("Remove") throws out the selected
  rows. Ctrl+A and Remove clear the whole set and return to manual entry.
- **An empty set clears the fields as well.** Once the last row is removed, the
  size is erased, the file count goes back to one (the field's lower bound),
  Container init becomes a dash. Otherwise the numbers of the removed source
  stay behind, and the calculation keeps counting from what is no longer in the
  set: the label says «источник не выбран» ("no source selected"), but the
  result stays the same as before, and there is nothing to tell this from
  manual entry — the fields are the very same.
- **Drag and drop** is the same as Add…: what is dropped adds to the set, it
  does not replace it. Replacing is the button, and it shows the dialog; a drop
  asks nothing, and losing what was assembled silently is not allowed. An extra
  source is removed with one button; a lost one has to be assembled again.

  The whole tab accepts drops, not the table alone: the table hides while
  nothing is selected, and there would be nothing to aim at. The input fields
  have accepting drops turned off: `QLineEdit` accepts drops on its own and
  would paste the path as text straight into the size, while a field that
  refuses passes the event on to the tab. A drop with no file on disk behind
  it (a link from a browser, an attachment from mail — the same
  `text/uri-list`) is discarded: a source can only be a path.
- **Nested paths are discarded.** A selected folder and a file inside it would
  give that file twice, and double counting inflates the calculation silently.
  A parent is always shorter than what is nested in it, so the normalized
  string order is the nesting order. The selection order is kept: in the table
  the sources stand the way the user named them.
- **The sources table** — a row per selected item: name (full path in the
  tooltip), type, file count, number of nested folders, logical size and
  cluster-rounded size. It hides entirely while nothing is selected: with
  manual entry it would be empty noise.
- **The summary** under the table: how many sources are selected (folders and
  files), total files and nested folders, logical size, cluster-rounded size
  and cluster tail, the largest, the smallest and the average file, the number
  of empty files. It is computed in `sizes.SourceStats`, not in the UI: a sum
  over several sources is arithmetic, and it has to be tested without Qt.
  Advanced statistics (medians, distributions, slices by type) are still out of
  scope.
- For a folder two quantities are computed:
  `F_alloc = Σ ceil(size_i / cluster) * cluster` and `n`, the file count. A
  plain sum of logical sizes is wrong for folders and is not used. The cost of
  MFT records is not part of `F_alloc` — it belongs entirely to `copy_slack`,
  so as not to be counted twice. With several sources both quantities are
  summed over all of them.
- A cluster size field (4096 by default). Input fields are limited both in
  length and in width: bytes — 24 characters and a width for
  `1 125 899 906 842 624`, cluster — 7 characters and a width for `65 536`,
  Container init — 10, file count — 11. The width is measured with font
  metrics on the longest allowed value: a cluster field the width of the
  screen lies about what can be typed into it.
- Result: `Container init` in MiB, in large type, and a button that copies it
  to the clipboard.
- The breakdown by component, as a table: payload, cluster tail, VeraCrypt
  header, NTFS metadata, copy slack, safety margin, total. It shows what the
  number is made of.
- A note whether the requested size is inside the calibrated range or is
  extrapolation; separately, a note that with `n > 1` copy slack is not
  confirmed by measurements.

**2. Records**
- Copy records only. Empty-volume measurements never get here — they live in
  their own file and on their own tab, so no filter is needed.
- The summary under the table counts what the records themselves give the
  models; empty-volume measurements are not included in it, their coverage is
  shown on the Calibration tab.
- The record dialog has three actions in a row right under the name:
  «Взять с «Расчёта»» ("Take from Calculation"), «Измерить том» ("Measure
  volume"), «Замерить остаток» ("Measure left space").
- Take from Calculation carries over `container_mib`, the cluster size,
  `file_bytes`, `file_count`, `file_alloc_bytes`, and also `predicted_mib` and
  `predicted_safety_mib`. The data fields come from there because the
  application does not copy files and cannot know them by itself; the only one
  that has already counted them is the Calculation tab, where the same data set
  was selected. Container init is carried over because it is exactly
  what gets created in VeraCrypt from this calculation, and retyping it by hand
  means inviting a typo. The prediction is recorded in the same move: typing it
  in after the fact is too late — by then the model is already different.
  `container_mib` and `predicted_mib` then live separately: the first is what
  was actually created, the second is what the program advised.
- Measure volume reads the volume capacity and the empty free space.
- The volume picker takes its text from the caller: Measure volume and Measure
  left space open it the same way but measure different things, and a common
  wording would not say which volume to mount now.
- Measure left space reads `left_bytes` and reconciles the contents of the volume
  with what the calculation was based on. The reconciliation is by totals: the file
  count and `Σ ceil(size_i / cluster) × cluster`. A mismatch is a warning, not
  a ban.

  It deliberately does not reconcile file by file: copying the contents of a
  folder instead of the folder itself, and renaming along the way, are normal,
  and comparing paths would raise a false alarm on every other measurement.
  NTFS system directories (`System Volume Information`, `$RECYCLE.BIN`,
  `found.000`) are listed on a separate line: the space they take is real and
  counts as "used", but they are not payload.

  A measurement without reconciliation is meaningless: left space is tied to the
  data size and the file count taken from the Calculation tab. If the wrong
  thing was copied, copy slack comes out as garbage and silently spoils the
  calibration, and there is nothing to catch it with afterwards: check No. 4
  (`consumed >= alloc`) catches only gross errors.
- On an empty volume Measure left space refuses to work and points to Measure
  volume.
- The records table: id, container_mib, volume size, NTFS, deviation from the
  model, files, left space, **miss**, **model miss**, status. The two miss
  columns are the after-the-fact check of the prediction, see the section
  "Checking the prediction after the fact"; a negative value is highlighted in
  red because it means "the data would not have fit", and that has to be seen
  without hovering the mouse.
- Next to the miss the record dialog also shows the minimum sufficient
  container and a verdict in words: «перезаклад» ("overestimate"), «впритык»
  ("just fits"), «данные не влезли бы» ("the data would not have fit"). The
  sign decides everything, and a "−3" among the numbers does not read at once.
- No column stretches with the window width. A stretched column eats all the
  remaining space, and then the table width is locked to the window width:
  dragging the edge of one column, the user moves the neighboring one, and the
  edge of the stretched one does not move at all. The sum of the columns lives
  its own life, and if it goes past the window, a horizontal scroll bar
  appears.
- Sorting by clicking the header, three states in a cycle: ascending,
  descending, unsorted. The third is the original row order, the same as at
  startup. Qt by itself can do only the first two. Numeric columns sort by
  value, not by the displayed text, otherwise "9 000" lands after
  "10 000 000". Empty values go to the tail when sorting ascending. No sorting
  is applied on opening: the original order is the order in which the
  measurements were taken.
- Column widths are dragged with the mouse and survive a table update. They
  are fitted to the contents once, at the first fill: doing this on every
  update would mean wiping out what was set by hand.
- The table's height is dragged by the height grip under its bottom edge, see
  "Window".
- Add / edit / delete. A note field.
- The Measure volume button — see the section "Measurements".
- **Edit windows are modeless, and there can be several of them.** A record is
  edited while looking at the Calculation tab: it is the source of the size and
  the file count, and pressing Take from Calculation without it is pointless. A
  modal window covered exactly what it had been opened for.

  Still, there is one window per record: pressing Edit… again raises the one
  already open. Two windows on one row are a race won by whoever presses Save
  last, and there is nothing to notice the lost edit by. New records can be
  entered in any number: until a record is saved, they have nothing to get in
  each other's way with. A calibration point is keyed by container size: what
  its window is about is which size it measures.
- **The place of a record is found by identity at the moment of saving**, not
  remembered as an index at opening. While the window is open, the list has
  time to change: a neighboring record was deleted from another window — and
  the index already points to someone else's row. Equality does not fit here
  either: `Record` compares by value, two records with identical fields are
  common, and the edit would go into the first one found. If the record is no
  longer in the store, this is said out loud, rather than a copy being created
  after the fact.
- **Deleting a record closes its edit window**; switching the records file
  closes all windows at once. An open window holds a record of the previous
  store: there is nowhere to save it, while Save in it looks as if it works.
- **Picking the volume letter is modal to its own window, not to the program**
  (`WindowModal`). Otherwise a measurement in one record window would lock all
  the others together with the Calculation tab — exactly what we were moving
  away from.

**3. Model**
- The current NTFS calibration points and the covered range.
- The current `slack_base` and `slack_per_file`, the number of records they are
  calibrated on, and the covered range of `n`.
- Two "measured / predicted / deviation" tables — for the NTFS model and for
  copy slack. The prediction for each record is computed by a model calibrated
  **without that very record**: a piecewise-linear model passes exactly
  through its points, and without the exclusion the deviation would always be
  zero. Above the table, under the heading — the largest underestimate.
- The check runs over everything the model stands on: copy records, own
  measurements and factory data. The factory data is what holds the NTFS
  curve — twenty-six empty volumes and seven more points from the copy-slack
  measurements — and a report silent about them would report on a different
  model from the one the calculation uses.
- The safety margin is shown but not edited. It depends on the volume size and
  the file count, and only the Calculation tab knows them. A second field here
  duplicated the first and rolled itself back on auto-selection — it looked
  broken because it was.
- The largest underestimate from the leave-one-out check is shown as
  information, not as a requirement on the safety margin: it is an estimate of a model
  without one point, that is, one twice as sparse as the real one. Demanding a
  safety margin from it for all sizes at once means paying everywhere for the place
  where the grid is sparsest.

**4. Calibration**
- A table of recommended sizes from 512 MiB to 1 TiB. No upward extrapolation
  is left at all: 1 TiB is covered by a factory measurement. It used to be the
  weakest spot of the model — the baseline slope of 0.17 % against the real
  growth of one byte per 32 KiB of volume overstated the metadata at a terabyte
  12.5 times, 1704 MiB against 136.
- Four rows — 768, 1536, 3072 and 6144 MiB — were added later and split in
  half the segments with a twofold step: the only places where the grid was
  sparser than the curve changes. Their own measurements have since been
  copied into the factory data (see "Factory calibration points"), so every
  row now has a factory measurement. Above 8 GiB the segments are already denser, and
  after 64 GiB the curve is almost flat, so there is nothing to densify.
- Four row states, each written in words and tinted with a color:
  «свой замер» ("own measurement"), «заводской» ("factory"), «свой, отключён»
  ("own, disabled"), «нет замера». Color alone is a poor hope. A disabled own
  measurement takes the factory color: the factory value is what the model
  uses there.
- The "measured" mark is set by the fact of a measurement at that size, not by
  falling inside the measured range. The previous rule marked as covered 12, 24
  and 80 GiB, which nobody had measured: any size falls between 8 and 100 GiB.
- «Снять» ("Measure") opens a simplified dialog: only the empty-volume
  measurement, the data and left-space fields are hidden — nothing is put into
  the container.
- «Снять автоматически…» ("Collect automatically…") hands all the work to
  VeraCrypt — see "Automatic collection". The dialog is created by the window,
  not by the tab: collection needs both the store to put the measurements in
  and the data folder, which the program has to pass to itself on an elevated
  restart.
- «К заводскому» ("Use factory") disables the own measurement, «Вернуть своё»
  ("Restore own") enables it again, «Отключить все свои замеры» ("Disable all
  own measurements") does the same for all of them at once.
- The signal with which a row asks to toggle its own measurement carries the
  volume size and must be **64-bit** (`Signal("qint64", bool)`). Qt's `int` is
  a four-byte C++ `int`: a volume of 4 GiB or more overflows it, the truncated
  value matches no measurement, and the button silently does nothing. Only 1
  and 2 GiB worked — the only ones that fit in 32 bits.
- A separate tab, not a block on the Model tab: calibration points and copy
  records serve different purposes, and the Model tab is the longest one as it
  is.
- Below it — a second table, **«Запас на копирование» ("Copy slack")**: file
  set, file count, cluster-rounded size, measured slack, miss, model miss and a
  delete button. Here, not on the Records tab, because these measurements live
  in the measurements file and describe the same machine; copy records
  describe the data and move along with it.
- The summary above it says how many distinct file counts have been collected
  and warns plainly while there are fewer than two: with a single `n` the slope
  is not computed at all. There too is the result of the prediction check on
  these measurements — they are the only ones whose prediction was recorded by
  the program itself.
- A copy-slack measurement is deleted, not disabled: there may be no factory
  measurement for the same file count, and "disabled" would just mean hidden
  numbers. Deleting asks for confirmation.

## Calibration at large and small sizes

The key point: **a calibration point does not need a file of the matching
size.** NTFS metadata is determined by the volume size, not by what is written
to it. The procedure:

1. Create an empty container of the needed size in VeraCrypt, filesystem NTFS,
   quick format.
2. Mount it.
3. In the application press Measure volume — the capacity and the free space
   are recorded.
4. Unmount, delete the container file.

The recommended set of points is twenty-six sizes from 512 MiB to 1 TiB,
`RECOMMENDED_MIB`. Dynamic containers do not need that much space: only the
metadata lands on disk, and the terabyte point costs 136 MiB. Free space the
size of the largest one is needed only for a normal container with full
format — and in the whole collection there is one such step, the second
container of the self-check.

Copy slack is not calibrated this way — it needs records with actually copied
data and different file counts.

### Automatic collection

The same twenty-six containers by hand take several hours, and every step can
be done wrong silently. The Collect automatically… button on the Calibration
tab hands all the work to VeraCrypt: the program creates a container, mounts
it, reads the volume, unmounts it and deletes the file — one size per step.

**Finding VeraCrypt.** First the standard install locations —
`C:\Program Files\VeraCrypt` and `C:\Program Files (x86)\VeraCrypt`, taken from
`ProgramFiles`, `ProgramFiles(x86)` and `ProgramW6432`, with hard-coded
fallbacks in case the environment is empty. If nothing is found, the program
says so and asks for the folder; the choice is remembered in `settings.ini`.
The exe itself can be given too — its folder is taken. The portable build has
names with an architecture suffix (`VeraCrypt Format-x64.exe`,
`VeraCrypt-x64.exe`), the installed one has them without it; the search knows
both variants and `-arm64`. Creating and mounting are **different binaries**,
and both are needed: half an installation does not count as an installation.

**Commands** checked against the VeraCrypt 1.26.24 documentation:

```
"VeraCrypt Format.exe" /create <file> /size <exact bytes> /password <pw>
    /hash sha512 /encryption AES /filesystem NTFS
    /dynamic /quick /nosizecheck /force /silent

"VeraCrypt.exe" /quit /silent /volume <file> /letter <letter> /password <pw>
    /hash sha512

"VeraCrypt.exe" /quit /silent /unmount <letter>
```

- `VeraCrypt Format.exe` has no `/pim` at all — it exists only for mounting,
  and creation cannot be sped up with a reduced iteration count;
- `/nosizecheck` is mandatory: without it a dynamic terabyte container refuses
  to be created where there is no terabyte free;
- `/dismount` is deprecated, `/unmount` is needed;
- `/hash sha512` on mounting saves VeraCrypt from trying all PRFs;
- the size is passed as exact bytes, not with the `G` suffix: it has to hit
  `container_mib × 1048576` to the byte, otherwise the measurement lands off
  its row.

**Old VeraCrypt versions.** The switches have changed, and the version is read
from the resources of `VeraCrypt.exe` precisely for this (according to the
Release Notes shipped with 1.26.24):

| Version | What changed | What the program does |
|---|---|---|
| before 1.24 | no `/nosizecheck` and `/quick` | collection does not start, the version is named in the window |
| before 1.25.4 | `/silent` did not remove the wait window when creating an NTFS container | collection runs, the window warns |
| before 1.26.20 | `/unmount` does not exist, only `/dismount` | `/dismount` is used |
| 1.26.20 and newer | `/unmount`, `/dismount` declared deprecated | `/unmount` is used |

An unread version is treated as old: `/dismount` is understood by every
released version, including the latest, while `/unmount` is not understood by
old ones, and erring in this direction is cheaper. The error here is quiet:
with `/silent` VeraCrypt says nothing about an unknown switch, the volume stays
mounted, and the container file stays on disk.

Below 1.24 the refusal is real, not a warning: without `/nosizecheck` a
terabyte container is not created where there is no terabyte free, and without
`/quick` each of them would be fully formatted.

**Nothing relies on the exit code.** `/silent` is described as "If there is
any error, the operation will fail silently". The result is checked by the
facts: whether a file of the needed size appeared, whether the letter came up
(by polling the list of mounted drives, not by the process exit), whether it
went away after unmounting.

**Unmounting is retried, not waited out.** A volume that has just been filled
with files is not released by VeraCrypt at once: it refuses with a non-zero
code, and immediately, and half a minute later unmounts it without objection.
Waiting a minute after a refusal is pointless — the volume is not unmounting
slowly, it was not released at all. Hence: a short delay for the driver (5 s),
a pause (15 s), a new command, and so on up to four times.

`/force` — **only in cleanup and only as the last attempt**. It unmounts the
volume even when files on it are in use, which means unflushed cache contents
may be lost. Before measuring left space this must never be done: a lost write
will look like extra free space, and the measured slack will come out
**underestimated** — that is, the error goes to the only dangerous side. In
cleanup there is nothing to lose, the container is deleted right away, while
leaving a mounted volume and a terabyte file behind is not an option.

Last, not first, for the same reason the version is read to choose between
`/unmount` and `/dismount`: in silent mode VeraCrypt swallows an unknown switch
without a word. If cleanup started with force right away, on a version that
does not understand `/force` it would not work at all. First, three times the
gentle way.

**The self-check comes first.** One gigabyte is measured twice: as a dynamic
container with quick format and as a normal one with full format. If they
match, the remaining sizes go as dynamic, and a terabyte does not need a
terabyte of free space. If they differ by more than a megabyte, collection
stops: the equivalence does not hold on this machine, and that has to be
learned in seconds, not after three hours of work. The one-megabyte tolerance
is taken from the observed ripple: on overlapping sizes the difference was
8 KiB at 12 GiB and 3 KiB at 80 GiB.

**Administrator rights.** The documentation for `/filesystem NTFS`: "a UAC
prompt will be displayed unless the process is run with full administrative
privileges" — on twenty-six sizes, the self-check and the file sets that is
over thirty prompts in a row. The dialog shows the current rights and offers a
restart through `ShellExecuteW` with the `runas` verb, passing itself
`--data <current folder>` so as not to lose portability. After the restart the
window closes: two copies in one data folder would write over each other.
Rights cannot be dropped back, so the restart is offered, not done on its own.

**Cleaning up after itself.** The mounted volume and the container file are
removed in `finally` — even on a crash and on cancel. Besides, the temporary
containers are named `containerhelper-calibration-<MiB>.hc`, and before each
collection the orphaned ones left by a previous interrupted run are deleted by
name: otherwise terabyte files would pile up silently.

**What the first real run showed** (31 August 2026, Windows 10 Pro 19045,
portable VeraCrypt 1.26.24): twenty-two sizes from 512 MiB to 1 TiB were
measured in 3 minutes 10 seconds, and **all twenty-two matched the manual
measurements to the byte**. `container − capacity` came out at exactly
266 240 B at every size — the 262 144 B of VeraCrypt headers and the one-cluster
NTFS tail.

Two conclusions follow. Automatic collection gives exactly the same numbers as
a hand does — there is no more need to check it separately. And "several
hours" of manual work turn into three minutes, so re-measuring the calibration
after a Windows update has stopped being an event.

The match speaks specifically about the automation: the manual measurements
were taken on the same machine and the same VeraCrypt version. How the numbers
behave on another Windows build is still unknown — that is what the
`FACTORY_MARGIN_BYTES` factory margin lives for.

Each measurement is saved as soon as it is taken, one by one: collection runs
for minutes, and on a slow disk even longer, and a crash in the middle must not
cost everything already measured. Cancelling takes effect between steps —
cutting VeraCrypt off in the middle would mean leaving a mounted volume and a
terabyte file. The steps run in a separate thread, otherwise the window would
freeze solid and there would be nothing to press Stop with.

### Automatic collection of copy slack

The same engine can create and mount a container — what remained was to teach
it to put a file set known to the byte inside and to read the left space. This
closes the only uncalibrated part of the model, and in minutes.

**One step** is eight phases, and each is named by a word in the line under the
progress bar: creating the container, mounting, measuring the empty volume,
writing files, reconciling the contents, remounting, measuring left space,
cleanup.

Two consequences follow. The empty volume is measured **before** writing, so
one step gives both a copy-slack measurement and a full NTFS point — and on a
"non-round" volume, between the recommended sizes, where the piecewise-linear
model had so far followed the chord blindly. And left space is read **on a
freshly mounted** volume: the model predicts Left space, that is, what
VeraCrypt will show a person when they mount the container with their data —
and exactly that state is what has to be measured.

**Files are generated right on the volume, not copied.** There is nothing to
copy: nobody has a set like this lying around, and the host would have to keep
a second copy next to the container and spend twice the space. For the measured
quantity it is all the same — slack measures files appearing on the volume,
not where the bytes came from.

**The file sets** differ in file count on purpose: from a single `n` the slope
is not computed at all, and from two it is random. The size is small almost
everywhere — slack depends on the number of files, not on their size:

| File set | n | Cluster-rounded | Needed on disk |
|---|---|---|---|
| 1 × 64 MiB | 1 | 64 MiB | ≈ 144 MiB |
| 50 × 10 MiB | 50 | 500 MiB | ≈ 581 MiB |
| 500 × 1 KiB | 500 | 1.95 MiB | ≈ 82 MiB |
| 5 000 × 1 KiB | 5000 | 19.5 MiB | ≈ 100 MiB |
| 10 000 × 1 KiB | 10000 | 39 MiB | ≈ 120 MiB |
| 500 × 1 KiB + 50 × 10 MiB + 1 × 1 GiB | 551 | 1526 MiB | ≈ 1.6 GiB |
| 1 × 4 GiB | 1 | 4096 MiB | ≈ 4.2 GiB |

The two sets with `n = 1` — 64 MiB and 4 GiB — are there not by oversight:
they test the assumption itself that "slack depends only on `n`". If they
diverge, the model is wrong, and that has to be known explicitly.

What breaks silently if the composition is touched:

- **a kilobyte is the lower bound for a file.** NTFS keeps a file shorter than
  about seven hundred bytes right in its MFT record and gives it no cluster at
  all. Then `Σ ceil(size / cluster)` overstates what is used, the measured
  slack goes negative, and the record is rejected by the "used is less than the
  file" check;
- **file names are long on purpose.** An entry in the directory index is the
  bigger the longer the name; with short names the index would grow less than
  with real data, and the slack would come out underestimated;
- **the set goes into a subfolder, not into the volume root.** Data is almost
  always copied as a folder, and the root index is built differently from the
  index of an ordinary directory;
- **the volume's cluster size is read, not assumed.** Ten thousand one-kilobyte
  files with a 65536 cluster take not 39 MiB but 625; a set that will not fit
  is rejected before writing starts, not cut off in the middle;
- **the contents are reconciled by walking the volume.** The file count and the
  cluster-rounded size are taken from the real volume, not from the set's
  design: `file_alloc_bytes` must be measured, otherwise the difference goes
  straight into the measured slack.

**The prediction is kept apart from the container.** The calculation for the
set is done exactly as on the Calculation tab, including the safety margin
auto-selection, and is recorded as is — the prediction check rests on it. But
the container is created 64 MiB larger than promised: if the model missed
downward, the set would not fit, and instead of a measurement there would be a
failure. The cushion distorts nothing — the measured slack does not depend on
the volume size, and the empty-volume metadata is measured on the very same
volume.

### What the first copy-slack run showed

31 August 2026, the same machine and the same VeraCrypt 1.26.24. Twenty-three
empty-volume measurements went through; the seven file sets did not all: **three
were taken, four failed**.

**NTFS metadata was confirmed completely.** All 22 sizes matched the factory
ones to the byte, the self-check agreed to the byte (17 879 040 B both ways).
This is the second independent run, that is, the numbers reproduce. On top of
that the file sets gave points below the previous range, and there both models
overstated:

| Volume | Measured | Default model | Model from 22 points |
|---|---|---|---|
| 90 MiB | 14 934 016 | +4.91 MiB | +1.43 MiB |
| 152 MiB | 14 938 112 | +5.01 MiB | +1.54 MiB |

Between 90 and 152 MiB the metadata grew by one cluster: below half a gigabyte
the curve is almost flat. The calibration range is 90 MiB … 1 TiB.

**Copy slack turned out much smaller than assumed.** With one file it equals
**4096 B — exactly one cluster**, and this was confirmed twice, on files of
64 MiB and 4 GiB. The default value promised 197 888 B, that is, overstated 48
times. Along the way the model's assumption itself was confirmed: **slack
depends on the number of files, not on their size** — the two sets with
`n = 1` were set up precisely for this reconciliation.

| n | Measured | B per file since the previous point |
|---|---|---|
| 1 | 4 096 | — |
| 64 (manual record Cache 1) | 24 576 | 325 |
| 500 | 692 224 | 1531 |

The rate grows with `n` instead of falling. So either the dependence is
stepwise (the MFT grows in chunks), or the manual record on an 11-gigabyte
volume is not comparable with the synthetic data. The sets of 50, 551, 5000
and 10 000 could have told these apart — the very ones that failed. **So the
default values are not changed for now:** changing them from three points, two
of which are disputable, would mean fixing a guess in place disguised as a
measurement.

**The prediction check passed.** Three real copies: miss +8, +9, +2 MiB, model
miss +1, +1, 0. The model never underestimated, and its own error did not
exceed a megabyte.

**Four failures — one diagnosis.** All on unmounting, with exit code 1, in
the remount phase. The lock is temporary: a repeated attempt in cleanup, which
happened a minute later, went through — the letter was released, and not a
single orphaned container was left. Hence the rule "unmounting is retried, not
waited out": the retry that saved cleanup by accident is now done on purpose,
and in the measurement itself.

The failures do not line up with the data: 500 files went through, but 50 did
not; 4 GiB went through, but 500 MiB did not. What can be seen is that the
steps that made it were the ones before which the system had stood idle for two
minutes on other steps' failures. It looks like catch-up writing or an
antivirus going through freshly created files. The exact cause is unknown, and
an explicit cache flush before unmounting is deliberately not added: the retry
removes the consequence for certain, while a remedy against an unverified cause
would go stale together with the guess.

### Free-space pre-check

NTFS measurements need almost no space: the container is dynamic, only
written clusters land on the disk, and on an empty volume only the metadata is
written. A terabyte measurement costs **136 MiB, not a terabyte** — a check at
1 TiB with 800 GiB free passes without trouble. There is one exception in the
whole collection: the second self-check container is not dynamic and gets a
full format, so it needs an honest gigabyte. Copy-slack measurements write
real gigabytes to the volume, and those do need space in earnest.

The required space is computed by the very metadata model that the collection
calibrates:

```
dynamic + quick:         262144 + ntfs(V) + cluster-rounded data + space margin
normal or full:          whole container_bytes + space margin
space margin:            max(64 MiB, 5 %)
```

Within the range covered by measurements this is accurate to a few megabytes.
The only possible error is excess caution: the uncalibrated model
overestimates the metadata of a terabyte thirteenfold, and the step would be
skipped for nothing — but never started where it will not fit.

There are two checks, and both are needed. Before a step: will it fit
entirely. During writing: has free space dropped below 128 MiB — an unrelated
process could have eaten it, and writing into a dynamic container on a full
disk tears the volume mid-write.

**A step that ran short of space is skipped**, not counted as a failure. The
log names the required and the available space, the summary counts skipped
steps separately and says they can be taken later. The Only missing sizes mode
picks them up by itself the next time the window is opened — free up space and
open it again.

The plan order plays along with this: empty-volume measurements first, then
file sets from cheap to expensive by data size. The hungry ones hit the space
limit most often, and if they stood first, one shortage would cancel
everything that would have fit perfectly well afterwards. File sets come after
the points for another reason too: the container for a file set is computed by
the metadata model, and that model is exactly what fresh points refine.

### Progress

The bar measures **work expressed in bytes written**, not steps done. Steps
differ too much: an empty terabyte volume is measured in seconds, while a
four-gigabyte file set takes minutes to write, and a bar creeping forward in
equal shares would lie several times over.

The step weight is what will land on the disk, plus 64 KiB per file in the
file set. The correction is needed because time does not rest on bytes alone:
ten thousand one-kilobyte files take 39 MiB, yet creating them takes longer
than writing those 39 MiB — the cost there is in MFT records and in the
directory index. The value is rough and deliberately lives only in the bar:
**it is never added to the required space**, otherwise steps would be skipped
where they fit perfectly well. For the same reason the space margin is not part
of the weight — it is about caution, not about work.

The share within a step is computed with the same weight: bytes written and
files created are added together. Taken separately, both lie — a bar by bytes
stands still on a file set of small files (their logical size is four times
smaller than the cluster-rounded one: 1 KiB files in 4 KiB clusters), a bar by
files stands still on a single large file. Outside writing the share is zero:
how many bytes VeraCrypt has already laid down while creating the container is
not visible from outside, and making it up with the bar is not worth it.
Instead the phase is named in words, and during writing both the number of
finished files and the volume written are visible:

```
[3/12] запись файлов, файлов 312 из 551, 1.203 GiB из 4.000 GiB ·
фаза 1:12 · прошло 2:41 · осталось примерно 4:10
```

(In English: "[3/12] writing files, files 312 of 551, 1.203 GiB of 4.000 GiB ·
phase 1:12 · elapsed 2:41 · about 4:10 left".)

The estimate of the time left appears once more than five percent of the way
is done: before that, the speed is measured over the first seconds of
container creation and is off several times over.

Progress goes to the window no more than ten times a second. Without this, a
file set of ten thousand files would send ten thousand signals across the
thread boundary and clog the event queue before the window could draw them.

**The volume written stands next to the file count, not instead of it.** The
«Один файл 4 GiB» ("One 4 GiB file") set is a single file boundary per several
minutes of writing: the «0 из 1» ("0 of 1") counter stood still for the whole
write, and there was no way to tell a working program from a hung one. On ten
thousand one-kilobyte files, conversely, the file counter is what reads, and
the bytes creep unnoticeably. That is why `generate` now reports writing not
only at a file boundary but after every chunk written — at the same place where
cancellation and the host's free space are checked.

**The clock runs by itself, once a second, not together with progress.**
Progress comes from the step, and during container creation, full format and
mounting there is none at all — so the time froze for minutes exactly when the
only sign that the program was alive was needed. The window's timer redraws
the line regardless of whether the step has said anything at all.

**The phase has its own clock**, separate from the overall one. They show
different things: the overall clock how long the collection has been running,
the phase clock how long the program has been standing in one place. It is the
latter that answers the question "is it still working?". The phase detail
(file count, bytes) is not treated as part of the phase: during writing the detail
changes ten times a second, and the phase clock would reset along with it.

Container creation itself is not measured by progress and cannot be: the
container file gets its full size at once — VeraCrypt creates it sparse — and
`st_size` does not grow while it works. No share can be derived from this; it
is more honest to name the phase in words and show how long it has lasted.

**Cancellation** now reaches inside a step as well: writing a file set stops at
once, and the unfinished part is thrown away together with the container
anyway. But container creation, once started, is still carried through to the
end — cutting VeraCrypt off midway means leaving a mounted volume and a
terabyte file behind.

**The collection window lives inside a scroll area**, except for the button
row. The content asks for more than eleven hundred pixels of height — more
than the screen has — and without scrolling Qt squashes the labels: the row
with a file set's cost got zero height, and its text vanished entirely. The
buttons stay outside: the Stop button must be at hand without scrolling the
window.

A file set's cost stands **as a label under the check box**, not in the check
box itself: `QCheckBox` does not wrap lines, and the line
«10 000 файлов по 1 KiB — 10000 файлов, 39.06 MiB по кластерам, нужно 117.56 MiB, свой замер уже есть»
("10 000 files of 1 KiB — 10000 files, 39.06 MiB cluster-rounded, 117.56 MiB
needed, own measurement already exists") asks for 900 pixels in a window
660 wide and is cut off precisely at the end, where it speaks about the space
and about the measurement already taken. The height of such a label has to be
set by hand: a `QLabel`'s minimum height does not depend on its width, and the
layout squeezes it down to one line and below. The width used for the
calculation is not the current one but the guaranteed minimum — the content
will not get narrower than that; beyond it, horizontal scrolling takes over.

## Window

Every tab lies inside a scroll area. When the window is shorter than its
content, the elements slide under the scroll rather than being squashed into
an unreadable state: a hard 180 px for the table on the Model tab showed five
rows out of thirteen.

The minimum window width is 720 px, the minimum width of a tab's content is
680, of a table 320. Below that, form labels get cut and columns collapse into
a mess.

A table's minimum height is computed from the widget's actual metrics: the
header plus two rows plus the frame plus the horizontal scroll bar. It cannot
be picked as a number — the theme and the font size change both the header and
the row. Without this, when shrunk to the minimum, the header ran over the
first row.

The height between tables used to be divided by a splitter. Its handle sat on
the table's bottom edge, so a table had to go last in its section: with a label
placed after it, one had to drag by the grey border next to the text. That is
why on the Model tab the check result moved to the top, under the heading, and
it stays there.

There are no more splitters. A splitter divides the height that already exists
between its neighbours and cannot grow beyond it, while the height of a tab
inside a scroll area is set by the minimum of its content. So each table has
its own **height grip under its bottom edge**: it changes the table's
`minimumHeight`, the tab becomes taller than the window, and the scroll area
grows longer. A double-click on the grip restores the minimum height.

The **Full-height tables** toggle does the same for all tables at once: it sets
the height to fit all the rows of the table, capped at 40 rows. It is
recalculated when the set of records changes.

## Portable data folder

The program is designed for a folder that is carried around whole:

```
ContainerHelper\
  ContainerHelper.exe
  data\
    Records.json          copy records
    Records.json.bak
    Calibration.json      empty-volume measurements — calibration of this machine
    Calibration.json.bak
    settings.ini          window, columns, heights, sortings, units,
                          showing hidden items in the picker,
                          geometry of the chart windows,
                          path to VeraCrypt and the folder for containers
```

The registry is not touched at all: the settings lie in `settings.ini` next to
the data. Otherwise the utility silently forgets everything on another
machine, and a trace remains outside the folder.

The data directory is searched for in this order:

1. the `--data <path>` argument — it is written into a shortcut and needs no
   saved state, so it works where the program folder is not writable;
2. `data\` next to the executable, if the directory is writable. The usual
   case, no traces outside the folder;
3. the directory chosen by hand at the previous launch.

If nothing fits, the program says why and offers to choose a directory. The
choice is remembered in `%APPDATA%\ContainerHelper\location.ini`, keyed by the
path to the program folder, so that two copies do not overwrite each other's
choice. Quietly moving off to someone else's directory is not allowed: then
portability is lost silently.

The directory is checked by a test write, not by permissions: on network
drives permissions lie.

Remembered between launches: the size and position of the window, the active
tab, the column widths and the height of each table, the sort order, the
display units and the Full-height tables and Remember tab toggles.

The active tab is stored **by name, not by index**. The index changes with any
rearrangement of the tabs, and after one the program silently opens on the
wrong page — nothing reveals it, because it did open successfully. An unknown
name (including an index from earlier versions) honestly falls back to the
first tab.

The Remember tab toggle decides whether to open where the program was closed
or always on the Calculation tab; it is on by default. While it is off, the
remembered name is not overwritten: in such a session the program opened on
Calculation anyway, and writing that down would wipe the remembered one
without asking.

## Factory calibration points

Inside the package lies `containerhelper/data/factory_points.json` —
twenty-six measurements of empty containers from 512 MiB to 1 TiB, one per row
of the recommended sizes table. The point is for a new copy to calculate
meaningfully from the first launch: taking them yourself is several hours of
work.

The first twenty-two were taken by hand; four more — 768, 1536, 3072 and
6144 MiB — were carried over here later from own measurements taken by
automatic collection. Before them four rows of the table stood uncovered, and
the promise "one per row" was inaccurate. The carry-over is a copy, not a
move: the own measurement stays in `Calibration.json` and keeps superseding
the factory one, otherwise on the very machine where the numbers were taken
the segment would become "foreign" and get the factory margin
`FACTORY_MARGIN_BYTES`. In this data folder the carry-over did not change a
single calculation; in a clean one the safety margin advice for a volume of
about 2 GiB dropped from 5 to 4 MiB, because the interpolation segment became
shorter.

Up to 100 GiB these are normal containers, the rest are **dynamic with quick
format**. On overlapping sizes the two agreed: 12 GiB differed from the curve
built on normal containers by 8 KiB, 80 GiB by 3 KiB, with metadata of 42 and
95 MiB. That is how it should be: the host sees the sparseness of the
container file, while the filesystem inside the volume knows nothing about it
and is formatted the same way. Quick format differs from full format only by
zeroing the data area and scanning for bad sectors, and inside a container
there are none.

This is what makes the large points cheap: a terabyte dynamic container takes
a hundred megabytes on disk and is created in minutes.

They are read through `importlib.resources`, not by a path on disk: in a
one-file build the resource is unpacked into a temporary directory, and an
ordinary path does not lead there. Read-only.

Every factory measurement names its `filesystem` next to `cluster_bytes`: the
two make its profile, and factory data fills in only its own profile. All of
it is NTFS with 4 KiB so far; a measurement without the field reads as NTFS,
but the shipped files write it out anyway.

**Own always supersedes factory** at the same volume size of the same profile.
Not "the larger of the two", as in `MetadataModel._dedupe`: the factory value was taken on another
machine, and a smaller own value is more accurate than any foreign one.

**A factory margin of 4 MiB on top of the safety margin**, while both ends of
the segment rest on factory points. The size of the metadata is decided not by
NTFS in general but by the specific formatting code — the Windows build and
the VeraCrypt version. The `$LogFile` plateau around 48 GiB was observed on one
machine and is not a specification; underestimate is the only dangerous side
here. An own measurement nearby removes the factory margin.

**Resetting to factory deletes nothing.** The own measurement is marked
`disabled`, the numbers stay in the file, the factory value goes into the
calculation, and a button in the row brings the own one back. Separately, all
of them can be disabled at once — with a confirmation and with the number of
points in the text. There is no irreversible action at all, so there is
nothing to warn about. Copy-slack measurements are not affected by this: for
them a factory measurement for the same file count may not exist at all.

### Factory copy-slack measurements

Next to it lies `containerhelper/data/factory_slack.json` — the same thing for
copy slack. As everywhere, only measured values are stored: the slack itself
is derived from them. An own measurement supersedes a factory one **by file
count** within the profile, not by volume size: copy slack depends on `n`.

A factory copy-slack measurement is a full record, and it gives an NTFS point
on a par with the others: its empty volume was measured. Its volume therefore
also ends up in `factory_volumes()`, meaning that a segment with both ends
resting on it gets the same 4 MiB margin.

**The file is filled** from the full run of 31 August 2026: seven
measurements, `n` from 1 to 10 000, Windows 10 Pro 19045 and VeraCrypt
1.26.24. A new copy of the program gets calibrated copy slack from the first
launch — `per_file` is computed as a slope rather than taken from the default.

Along with them come seven NTFS points on "non-round" volumes (89, 116, 143,
150, 582, 1610 and 4185 MiB): the empty volume of each measurement was taken
before the files were written. Thanks to this, a new copy's metadata grid has
33 points instead of 26 (at the time of the run, 29 instead of 22: the four
carried-over points came later), and its lower bound drops from 512 to 89 MiB.

Before the first run the file shipped empty, and that was right: made-up
numbers are more dangerous here than their absence, because the model would
take them for measurements.

## Filesystem and cluster size

These are different things, and they must not be confused.

**Cluster size** is the step in which the filesystem hands out space. A
one-byte file takes a whole cluster, a 4097-byte file with a 4096 cluster
takes two. Hence `payload_alloc = Σ ceil(size_i / cluster) × cluster`.

The effect depends on the number of files, not on the data size. On one large
file the difference between 4096 and 65536 is less than one cluster. On 500
files of a kilobyte each it is 2 MiB versus 32 MiB, sixteenfold.

The parameter is not tied to the choice of filesystem: exFAT and FAT32 have
clusters too, and the arithmetic is the same. What is more, it matters more
there — by default Windows gives exFAT on a volume larger than 32 GB a 128 KiB
cluster, against 4 KiB for NTFS, and the same 500 files already take
62.5 MiB. A hard-coded 4096 would make the calculation 60 MiB too low, and the
container would not hold the data. That is why the parameter stays.

**The metadata model does not survive a change of filesystem.** `$MFT`,
`$LogFile`, `$Bitmap` are NTFS structures. exFAT's overhead is organised
differently and is much smaller, FAT32's differently in a third way; all
calibration records were taken on NTFS. VeraCrypt itself also offers "None"
(«нет») as the filesystem. With it there is no filesystem on the volume, and
no model is needed (`solve_raw_container_mib`):

```
C = ceil((F + 262144 + safety) / 1048576)
```

`F` is the logical size, not the cluster-rounded one: without a filesystem
there are no clusters, and the data lies on the volume byte for byte. The
safety margin is the floor, `MIN_SAFETY_BYTES`: there is no model whose miss
it would cover, and the floor is kept because it guards the rest of the
calculation too. A larger margin set by hand is taken, a smaller one is
raised to the floor.

That is why the filesystem is read from the volume at measurement time and
named in the dialog. A measurement on exFAT goes into the exFAT profile (see
"The volume profile") and cannot quietly spoil the NTFS model.

## Tooltips

Everything that does not explain itself by its name has a tooltip: every tab,
every column of every table, every table as a whole, every input field and
every button.

Columns are the mandatory case. The headers are short not out of concision but
for lack of space: two-word headers like "NTFS", «Откл. от базовой»
("Deviation from baseline") and «Основа» ("Basis") say neither what lies in
the column nor where it comes from. `setHorizontalHeaderLabels` creates the
header items anew and wipes the tooltips along with them, so
`table.set_header_tooltips` is called right after it — on every change of
display units too.

Table rows get tooltips where something is missing in the row itself:

- the breakdown on Calculation — where each component comes from;
- calibration — the volume size the measurement is tied to (the row shows
  Container init, while the volume serves as the key) and the name of the own
  record;
- sources — the full path, which does not fit into the cell;
- records — the plausibility checks that failed.

One-word tab names say nothing about the order of work or about how
Calibration differs from Records — that is exactly what their tooltips say.

### What a tooltip must not contain

**A tooltip does not retell what is visible next to it.** It has a cost: to
read it, one has to hover the cursor and wait — and a retelling of the visible
does not repay that cost, while it also teaches people not to read tooltips.

Removed on these grounds:

- the tooltip on the file and folder picker itself. Qt hands it to every
  descendant without one of its own, and hovering over **any file in the
  list** showed the same text that is already written as the label at the
  bottom of the window;
- the tooltip on the Input data group — for the same inheritance reason: it
  popped up over every label inside the group and repeated the line
  «Источник не выбран: перетащите сюда файлы и папки…» ("No source selected:
  drag files and folders here…") standing right there;
- the tooltip over the summary of the selection: the summary is exactly what
  the tooltip retold;
- the first sentence on the «Показывать скрытые» ("Show hidden") check box
  and on the «Только недостающие размеры» ("Only missing sizes") toggle —
  they repeated their own label;
- the tooltip on the label with the path to the records file kept only the part that the
  label itself lacks: about the neighbouring `.bak`;
- the table's height grip kept only the part about the double-click. That it
  is something to drag, the cursor shows.

## Display units

A selector in the header of the window, one for the whole application: B, KiB,
MiB, GiB, TiB and «Авто» ("Auto"). It affects only how the byte columns are
shown in three tables and the headers of those columns. It affects none of the
following:

- input and storage — always bytes, otherwise rounding to GiB on input would
  lose the very precision for which the measurements are taken;
- the `Container init` number — it is typed into VeraCrypt as is, it is always
  in MiB;
- the «Байт» ("Bytes") column in the breakdown on the Calculation tab — it is a
  column for reconciliation, it stays in bytes, and the neighbouring column
  follows the chosen unit. Choosing "B" is shown there as MiB: two identical
  columns are useless.

In Auto mode the unit is chosen for each value and is therefore written in the
cell itself, not in the header.

The choice is remembered in `QSettings` between launches.

## Charts

Nine charts in four modeless windows. Windows, not a fifth tab: the tabs follow
the order of work — Calculation, Records, Model, Calibration — and a "Charts"
tab has no place in that order. A separate window can also be stretched to the
full screen, while a tab would have to share its height with a table.

| Window | What is inside | Opened from |
|---|---|---|
| «Метаданные NTFS» ("NTFS metadata") | curve, share of the volume, leave-one-out miss, segment slopes | Model, Calibration |
| «Запас на копирование» ("Copy slack") | copy slack against the file count, leave-one-out miss | Model |
| «Промах прогноза» ("Prediction miss") | promised against required, per record | Records |
| «Текущий расчёт» ("Current calculation") | breakdown bar, used space against the cluster size | Calculation |

### What draws the charts and why not a library

Our own widget on `QPainter`. It was chosen not on principle but by
measurement: four real portable one-file builds were made and compared — the
baseline without charts and one with each of the three libraries. The row for
our own charts came later, see below.

| Build | exe size | Import |
|---|---|---|
| without charts | 45.4 MiB | — |
| **with our own charts (as done)** | **45.5 MiB (+103 KiB)** | — |
| + QtCharts | 48.4 MiB (+7 %) | +5 ms |
| + pyqtgraph (and numpy) | 68.3 MiB (+50 %) | +428 ms |
| + matplotlib | 79.2 MiB (+74 %) | +548 ms |

The second row is no longer an estimate but a build rebuilt after the work was
done: seven charts at the time (there are nine now) with zoom, tooltips, a
legend and export to PNG, SVG and PDF cost a hundred kilobytes. Vector export
came almost for free: `QtSvg` got into the build on its own (`Qt6Svg.dll`,
0.6 MiB on disk, much less when compressed), and `QPdfWriter` lives in
`QtGui`, which was there before.

Onefile unpacks its archive into a temporary folder on **every** start, so the
extra megabytes are paid for more than once. They would buy interactivity that
has no use on twenty-six points: zooming into a handful of measurements solves
a problem that does not exist. On top of that, matplotlib has the weakest
interactivity of the three inside Qt: a hover tooltip and a click on a point
are written by hand there through `mpl_connect` — as much code as in our own
widget.

That left a choice between QtCharts and our own. Our own won on three things
QtCharts does not have:

- **labels follow the unit chosen in the window** (B/KiB/MiB/GiB), the way the
  tables do. The label format of `QValueAxis` is printf, and it has no Auto
  mode at all;
- **colors come from the window palette**, so the theme is the same as in the
  rest of the program, not a "similar" one out of two ready-made ones;
- **the chart arithmetic lies one layer below Qt and is tested without it** —
  by the same rule by which the models are computed without Qt. With a library
  it moves inside the library, and then it can only be checked by eye, and
  nothing here can be checked by eye: a curve drawn wrong looks just as
  convincing as one drawn right.

Vector export is not lost but gained: `QPainter` draws into `QSvgGenerator` and
`QPdfWriter` with the same code as on screen, both modules are already in
PySide6 and both are under LGPL. QtCharts has only raster out of the box.

### Layers

- **`plot.py`** — no Qt: axes, round ticks, mapping values to pixels (`Span`,
  `Frame`), finding the point under the cursor. Float is allowed here and does
  not break the rule "only integers in the calculation path": the calculation
  path is the container size, here it is pixels, and no number from here goes
  back into the model.
- **`charts.py`** — no Qt: building a `Chart` from measurements and models.
  What to draw is data, and what needs checking is which points ended up on
  the chart and what the tooltip says.
- **`ui/chart.py`** — the canvas: drawing, mouse, legend, export.
- **`ui/chart_window.py`** — a window with one or several charts.

### Share of the volume taken by metadata

The second chart of the NTFS window, right under the curve in the default
order. The same curve, but in the units in which metadata is judged by eye:
not "how much of it there is" but "how much of the volume it will eat". Bytes
do not show this: against a volume of gigabytes the difference between 0.2 %
and 0.4 % is the thickness of a line.

It and the segment slopes are different quantities and must not be confused:
the slope says **how fast** the metadata grows over a stretch, the share says
**how much of it there is in total** at that size. A flat stretch of the slope
and a flat stretch of the share are not the same thing.

Both axes are logarithmic. The share spans three orders of magnitude — from
0.013 % on a terabyte volume to 16 % on sixty-four megabytes — and on a linear
axis everything except the smallest volumes collapses into one line near zero.
That is exactly the trouble this chart exists to fix.

It is also why the tick labels had to be fixed: fractional values on a
logarithmic **count** axis went through `fmt_bytes(int(...))`, and "0,01"
turned into "0". Values below one simply never occurred on such an axis
before.

### What fills the size: why not a chart

There used to be a separate panel, the "coverage strip": three rows of dots on
the volume axis — own measurement, factory, nothing. It was removed, and here
is why:

- **own against factory is already on the curve** — as two series of different
  colors, and the strip repeated what was drawn;
- **only one thing in it was unique** — which recommended sizes are covered by
  nothing. That is one line of information, and the table on the Calibration
  tab presents it better: it has the exact size, the metadata value, and the
  Measure button in the same row. The strip did not let you do anything;
- **the table is more honest in one place**: it shows a disabled own
  measurement as «свой, отключён» ("own, disabled"), while the strip treated it
  as factory;
- **on full coverage it said nothing**: all twenty-six sizes are covered by own
  measurements, two thirds of the strip are empty. A chart that says nothing on
  current data is a bad neighbour in a window where vertical space is
  expensive.

What was left moved into the caption under the curve:
«Не покрыто замерами: N размеров — …; там прямая идёт через пустое место»
("Not covered by measurements: N sizes — …; there the line runs through empty
space"). Zero pixels, the same message, and it disappears by itself once
everything is covered. It is counted by the same rule as «Не покрыто» ("Not
covered") in the summary on the Calibration tab: there is neither an enabled
own measurement nor a factory one.

### Column and grid

The charts in a window stand in a column layout or in a grid layout of two per
row, chosen by a toggle in the window's top row. In a column, four charts ask
for at least 980 pixels of height; in a grid, 550 at a width from 650: vertical
space is more expensive, and on a wide screen the trade pays off.

- **An odd last chart stretches across the whole row.** Half a row left empty is
  just wasted space.
- **Each layout has its own window size.** The grid needs width, the column
  needs height, and one number cannot describe both — the same rule as for a
  chart's height in the shared window and the size of its own window. Keeping
  the size unchanged on a switch will not do: in the grid at the column's
  height the charts stretch to twice their height, in the column at the grid's
  width half of them go off the bottom edge.
- **The columns are shared by all rows.** The rows are independent splitters,
  and without a link between them a column boundary moved in one row left the
  second one as it was: two rows with different boundaries are no longer a grid
  but two separate pairs, and the alignment for which the grid is switched on
  is lost. The widths are stored once per window and sent to every row; a
  double click on a vertical handle aligns the rows **to the first one**, not
  everything to the middle — the widths may have been set on purpose.
- **Chart heights are remembered only for the column layout.** In the grid the
  splitter holds rows, not charts, and "chart height" means nothing there; for
  the grid, the row heights and the shared column widths are stored.
- **A chart cannot be collapsed to zero** (`setChildrenCollapsible(False)`). It
  does not fold into its title, it just disappears, and the only way to bring
  it back is to hit a five-pixel-wide handle with the mouse.
- **The layout is rebuilt whole**, not patched in place: detaching, returning,
  reordering and changing the layout change the composition in the same way,
  and four different ways of fixing the same thing would diverge on the first
  edit. The widgets are moved, not created anew — the zoom and the hidden
  series live in them.

### Chart order

Reordered with arrows in the corner of each chart (and with the same menu
items). Which chart is more useful on top is known only to the person looking:
for some the misses matter more, for others the curve itself.

**Two arrows in the column layout and four in the grid.** One pair cannot
express movement in a grid: "up" there is a whole row back, while "left" is one
place, and a pair of arrows that walked the order swapped a chart now with its
right neighbour, now with the end of the previous row. Left and right are not
shown in the column layout at all: it has no horizontal neighbours, and a
disabled button would promise a movement that never happens.

Reordering steps **over a detached chart**: it is not in the window, and
swapping places with it would look like a press that did nothing. Returning
puts the chart back at its place in the order, not at the end.

**Charts swap places, sizes do not**: a height stays with its chart, the column
widths and row heights stay with the grid. Swapping sizes on top of swapping
places left one thing in memory and another on screen, and the very next layout
change handed the charts someone else's sizes.

For this to work, sizes are taken **from the layout that stands right now** —
by the remembered "as built" arrangement and order, not by those already
reordered. Otherwise the heights are attributed to charts already reordered,
the two swap places at once, that is, do not swap at all. For the same reason
the sizes are taken before every rebuild, not on the splitter's signal:
`splitterMoved` comes only from the mouse, and anything reordered
programmatically never reached memory.

The order is stored in `settings.ini` as a list of numbers and checked for
composition, not for length: the saved list may contain numbers from an earlier
count of charts, and the layout would silently lose one of them.

### Window size on first show

Computed from what is inside, not from the number of panels: the breakdown bar
takes its own height, not a share of the window, and the window would come out
taller than needed. In the grid it is computed by rows. The screen height is
asked too — Qt shrinks a window taller than the screen anyway, and the splitter
hands that shortage out to the panels any which way.

### Mouse controls

| Action | What it does |
|---|---|
| Left, drag a rectangle | zoom into the selection |
| Left, click | show the point under the cursor in the line at the bottom |
| Left, on the legend | hide a series or bring it back |
| Left, double click | chart menu |
| Right, held | pan along both axes |
| Right, double click | full view |
| Middle | chart menu |
| Wheel | zoom around the cursor |
| Esc | full view |

Panning moved from the middle button to the right one, and the reset from any
double click to the right one: to pan the chart and to bring it back to full
view with the same hand, without changing the grip on the mouse. Not every
mouse has a middle button, and the main action cannot live on it.

The right button's own context menu is switched off for this
(`PreventContextMenu`), otherwise it pops up over the chart on every pan. The
menu stayed where nothing can be mistaken for it: the left double click and
the middle button.

**Detaching and reordering moved out of the menu onto the chart itself** — as
buttons in the top right corner, in the title band: "↑", "↓" and «В окно»
("To window") in the column layout, plus "←" and "→" in the grid (see "Chart
order"). A detached chart keeps only «Вернуть» ("Return"). The menu has to be
found first, and this is the first thing done with a chart when there are four
in the window. The buttons do not
get into the image: `image()` draws the chart with its own `_render`, and the
widget's children do not enter it at all.

The labels are short not out of love for brevity: the buttons stand in one row
with the title and take width from it. «Отсоединить» ("Detach") together with
the arrows ate a third of the row of a chart in the grid, and the title started
hiding behind an ellipsis for no reason. The buttons' margins are cut too — by
default they are designed for a toolbar, and one arrow took forty-four pixels.
The full phrase stayed in the tooltip and in the menu, where there is as much
room as needed.

The title is centered on **what is left after the buttons**, not on the
widget: narrowing the width from both sides would give the buttons twice as
much room as they take.

### Crosshair

Two lines from the cursor to the axes and the value on each axis, on a backing.
The backing is needed: without it the label lies over the ticks and reads as
one more tick. The value is computed **with the same step as the ticks**
(`plot.value_label`), otherwise the same place on the axis is labelled two ways
— "4" at the tick, and "4,0000001" at the crosshair next to it.

The point under the cursor is circled with a ring, not filled: under the ring
you can see which series it belongs to. The tooltip with the full description
of the point stays as before.

**Only the vertical line goes to neighbouring charts with a shared axis**,
without the horizontal one. The horizontal line would mean the neighbour has
the same Y value, while it has a different quantity and a different range:
the NTFS curve is in megabytes, its residuals in kilobytes. This vertical line
is why the shared axis exists at all — the hump in the residuals and the step
in the slope stand at the same volume size, and the only way to see it is a
line through both charts.

The crosshair is not in a saved image: it is mouse state, not the chart. In a
file it would mean that something was measured there, while it is just the
place where the cursor happened to be. The screen and the file are drawn by the
same `_render`, and the `live` flag separates one from the other.

### Detaching

A chart moves into its own window by a menu item and comes back to its former
place — by the same item, which then reads «Вернуть в общее окно» ("Return to
the shared window"), by the «Вернуть» ("Return") button, or by closing the
window. What moves is **the widget itself**, not the data: the zoom and the
hidden series move with it, and nothing has to be restored.

It stays at its place in the window's chart list: updates, the display unit and
the shared axis walk the list, not whoever lives where. Returning inserts it by
its number, not at the end — the order of the charts is the order in which one
talks about them, the curve before its residuals.

**The height in the shared window and the size of its own window are two
different quantities.** In the shared window the chart shares height with its
neighbours, in its own window it takes the whole window, and one number cannot
measure both. Both are remembered: the splitter share by chart number (the
splitter's list gets shorter when someone leaves, and by that list the heights
would drift apart), the geometry of the separate window by the same number, and
it survives a return: detaching a chart a second time gives the same window,
not one fitted anew.

The shared window shrinks by exactly the height that left it, and gets back the
height it had before the **first** detach. This cannot be computed by
addition: the layout's minimum prevents the window from shrinking, while
nothing prevents it from growing back, and over three rounds of
"detach — return" the window crept a third of the screen down. If the height
was dragged by hand in between, it stays the person's — we no longer consider
it ours.

The layout has to be recomputed (`layout().activate()`) before the size is
changed: while it thinks the chart is still in the window, its minimum holds
the old height, and the window does not shrink at all.

**A double click on the splitter divides the height equally.** The splitter is
dragged with the mouse, and there is no way to hit "equal" again by hand — and
that is exactly what you want after examining one chart of three.

**Two check boxes on a detached window** — Shared X scale and Shared
crosshair, and only there: a chart standing in the shared window is linked by
definition, it is right next to the others anyway. Separate, because they link
different things: the shared scale makes the windows show the same stretch,
while the shared crosshair stays useful even on different stretches — the
vertical line is placed **by value**, and each chart draws it in its own scale.

An unchecked box unlinks the window in both directions: it neither pulls its
neighbours nor follows them. If all charts are detached, the link remains
between them, because the window's chart list did not change. Windows whose
charts have different X axes (Current calculation) have no check boxes at all:
there is nothing to link.

Detached windows hide together with the shared window and show together with
it: they live on its updates, and there is no other way to show the shared
window again — the button on the tab raises that same window. The arrangement
(who is detached, where it stands, whether a box is checked) is stored in
`settings.ini` by window name: a window spread across the screen is arranged
once, not on every start.

### Live updates

A data update **does not reset what was set by hand**: the zoom, the hidden
series and the selected point. A chart window is opened to examine a stretch,
and collection goes in steps — on each step the picture would jump to full
view, and there would be nothing left to examine.

Three caveats:

- **What was hidden by a click stays hidden; what appears comes with its own
  default.** Otherwise «край диапазона» ("range edge") would pop back up on
  every update — or, the other way round, a series hidden by hand would not
  stay hidden if the chart had it visible by default.
- **The point in the line at the bottom is updated by key, not kept as text.**
  The measurement may have changed, and the line would tell about yesterday's
  numbers without giving itself away. If the point has disappeared from the
  data, the line becomes empty.
- **If no point is left in frame, the zoom is reset.** There is nothing left to
  hold it for: it shows an empty plot area, and nothing tells that apart from
  "no measurements". As long as anything is still in frame, the zoom stays, and
  how many points went out of frame is written in the corner of the plot area
  («вне кадра: 3» ("out of frame: 3")). Staying silent is not allowed here: the
  zoom is held on purpose, and without the label "nothing changed" after a new
  measurement looks like a breakage.

### What breaks silently

- **Residuals are drawn as stems, not bare dots.** Zero there is not the edge
  of the scale but the model itself, and a stem from it to the point shows
  where the miss goes and by how much, without making the eye measure the
  distance to the line. The point stays a point: each measurement stands on its
  own, and they must not be joined with a line. A bar does not fit — it reads as
  "part of a whole" and claims the value with its whole width.
- **Residuals are computed by the leave-one-out check, not by subtracting the
  model.** A piecewise-linear model passes exactly through its measurements, and
  ordinary residuals would be zero at every point — a straight line at zero.
  Each point is predicted by a model built without it (`metadata_cross_check`),
  exactly as the safety-margin advice is computed. The sign matters: up is an
  underestimate, the only dangerous side.
- **A logarithmic axis in bytes uses base two, one for counts uses base ten.**
  All sizes here are multiples of a power of two, and decimal decades would put
  the labels off them: "1 000 000 000 B" instead of "1 GiB". A file count, on
  the other hand, is never a power of two, and the series 1, 4, 16, 64 reads
  worse than 1, 10, 100.
- **On a linear axis there is one unit for all ticks, on a logarithmic one each
  tick has its own.** On a linear axis a common unit is needed so that the eye
  does not have to convert "512 MiB" into "1 GiB" while looking for the middle.
  On a logarithmic one it is impossible: the range there is by definition wider
  than one multiple unit, and half the labels would become "0.001".
- **Ticks follow the 1-2-5 ladder, and the step taken is the nearest, not the
  next one up.** Without the ladder the labels come out like 0,0037 and 0,0074
  — the thing that gives a home-made chart away instantly. Rounding up looks
  logical and thins the axis by half: on the real 137 MiB range of the NTFS
  curve it jumps from a step of 20 straight to 50, and instead of seven ticks
  only two remain.
- **The edge measurements on the residual chart are moved into their own series
  and hidden.** Without the outermost point the model has nothing to draw a line
  between, and it extrapolates with the baseline slope; the miss there measures
  something else and comes out orders of magnitude larger. On the real
  measurements the outermost one gives −433 MiB against fractions of a megabyte
  for all the others: leave it in the common series, and the axis stretches so
  far that the whole subject of the conversation lies on the zero line. The
  series is visible in the legend and comes back with one click, and the
  caption under the chart says that they are hidden: hidden silently is the same
  as lost.
- **Room for the caption is measured, not assumed to be one line.** The
  captions here are long, and the one that explains the hidden series was cut
  off exactly mid-word — «и промах там на порядки бо» ("and the miss there is
  orders of magnitude la").
- **The Y axis label is measured against the widget height, not the plot area,
  and wraps onto a second line.** It stands beside the plot area and is not
  limited by its height, while the margins above and below are taken by the
  title and the legend. Without this most labels were cut off:
  «Метаданные NTFS, B» ("NTFS metadata, B") is 216 pixels against a plot area
  209 high, «Измерено минус модель, B» ("Measured minus model, B") — 288 against
  186, «Наклон, % от размера тома» ("Slope, % of volume size") — 300 against
  195. Room for the second line is reserved in the left margin by the **line
  spacing**, not by the font height: on two lines the label takes a couple of
  pixels more, and by those pixels it would overlap the tick labels.
- **The miss without the safety margin is drawn as dots, the overestimate as
  bars.** A bar on top of a bar reads as "part of a whole", while this is a
  separate quantity of the same miss, taken from a different place: how much
  would be left if there were no safety margin. A narrow bar on top of a wide
  one was tried — the nesting is visible that way, but both quantities merge
  into one shape, and the eye stops telling what is measured from what is
  computed. A negative value stays a lone dot below zero, and that is
  tolerable: zero on the chart is drawn as a separate line, and the caption
  under it spells out what a point below zero means.
- **The shared X axis links only X.** The three metadata charts share the
  volume axis so that the hump in the residuals stands exactly under its step in
  the slope. The Y axis of each is its own and must not be linked: the curve's
  range is in megabytes, the residuals' in kilobytes, and a shared Y would
  collapse the residuals into a line. The synchronization stays silent in reply
  (`apply_x` does not emit its own signal), otherwise the first chart would tell
  the second, the second the first, and so on until the stack overflows.
- **A bar segment thinner than a pixel cannot be hovered, and this is not
  fixable.** Widening its hit area at the expense of its neighbours would split
  what is shown from what is queried: the cursor would stand on the NTFS
  metadata while the tooltip spoke about the VeraCrypt header. Such components are
  explained by the legend, where each has its exact number.
- **The breakdown bar must add up to the container to the byte.** There are six
  components, and the sixth is the rounding to whole MiB: without it the sum of the
  parts does not equal the whole, and the bar lies about the proportion. There
  is a test for this.
- **The window is rebuilt on a signal, not on show.** A measurement was taken,
  the model moved — and the chart shows everything as before, with no way to
  tell fresh from stale by eye. The Current calculation window listens to a
  separate signal: the calculation changes on every keystroke in the size field,
  and there is no reason to redraw the NTFS curve for that.
- **The geometry of each window is stored by name, not by number** — the same
  rule as for the active tab. The arrangement of the detached charts is stored
  with it.
- **A chart title is cut with an ellipsis, not chopped off raw.** A narrow window
  ate both of its ends at once (the text is centered), and there was no telling
  what was missing. The width is reduced by the detach button on **both** sides:
  the title stands centered, and it has to be trimmed symmetrically, otherwise it
  shifts.
- **The title band is measured from the top edge of the widget, not upwards
  from the padding.** With `PADDING - height` it started two pixels above zero,
  and the tops of the letters were cut off. From outside it looked like the
  splitter between the charts creeping over from above — the cause was searched
  for in the wrong place.
- **The rectangle of a rotated label grows to the right of the rotation point.**
  After `rotate(-90)` the `y` coordinate turns into the screen `x`, and a
  rectangle with `-spacing` ends up **left** of the padding: on one line the
  label lost its edge, and the second line did not get into the widget at all.
  That was what "the axis label is cut off" really was — not the lack of space
  that had been fixed before.
- **A click on a point explains it in place, it does not highlight a table
  row.** The table lives in the main window, and a row highlighted there from
  behind another window would be nowhere to be seen. So the chart window shows
  the point's description in its own line at the bottom.

### What the tests don't reach

The same as always: how it looks. Checked: the axis arithmetic, the composition
of the series, the tooltip texts, zoom, the legend, and that all three export
formats produce a non-empty file. Also checked: that the mouse buttons do
exactly what the table above says; that the crosshair goes to the neighbour and
does not go into the file; that detaching and returning put a chart back at its
place, and both link check boxes work separately; that the grid lays out two
per row and stretches an odd last one; that reordering steps over a detached
chart and survives a restart; that a data update does not reset the zoom; that
a bar with a negative value is filled below zero; that the axis label is not
cut off.

Not checked: whether all of this reads by eye — whether the crosshair lands
where it was aimed, whether the value backing gets in the way of looking at the
point, whether a detached window ends up on top of what it was detached for,
and whether a chart has enough width in the grid on a real screen.

## Technical requirements

- Python 3.12, PySide6. The only external dependency is PySide6.
- Volume capacity and free space: `shutil.disk_usage(letter)`.
- Volume cluster size: `GetDiskFreeSpaceW` through `ctypes` (needed so as not
  to guess 4096 on containers formatted in a non-standard way).
- VeraCrypt version: `GetFileVersionInfoW` through `ctypes`. If it cannot be
  read, no harm done: the version is needed by a person in a report, not by the
  calculation.
- Administrator rights: `IsUserAnAdmin`; the re-request is `ShellExecuteW` with
  the verb `runas`.
- Running VeraCrypt: `subprocess.run` with `CREATE_NO_WINDOW`, so that a console
  window does not flash for each container of a collection: twenty-six sizes,
  the self-check and the file sets.
- Folder size: `os.scandir`, recursively.
- Free space of the working folder: `shutil.disk_usage`. Asked both before each
  step and while a file set is being written.
- File set generation: ordinary file writes in 4 MiB chunks. The content is
  random bytes from `os.urandom`, one buffer per set: the volume is fresh and
  uncompressed, but there is no reason to depend on zeros not being folded away
  on it.
- All internal computations in integers, without float. Division rounding up
  is `-(-a // b)`.
- The only place where float is allowed is the model coefficients; the result
  is immediately converted to int, rounding up.

## Error handling

- A file or folder cannot be read — a message with the path; the calculation
  is not blocked (the size can be entered by hand).
- The chosen drive letter is not mounted — a message; the measurement is not
  taken.
- The JSON is corrupted — offer to open the `.bak`, do not overwrite the
  original. The message names the file that failed to read: there are two of
  them.
- No write permission for the JSON — a message with the path; the data stays in
  memory.
- VeraCrypt not found — say where we looked and ask for the folder; collection
  does not start until the folder is given.
- A collection step failed — write the reason to the log and move on: one failed
  size is no reason to drop the rest. The exception is the self-check:
  without it there is no knowing whether dynamic containers can be trusted, and
  collection stops.
- A step lacks disk space — skip it, naming the required and the available
  amount. Not a failure: space will be freed, and the Only missing sizes mode will
  pick the step up by itself. The collection summary counts the skipped steps
  separately and says so out loud.
- Space ran out in the middle of writing a file set — stop writing for this
  step and clean up. Writing on into a dynamic container on a full disk is not
  allowed: the volume breaks midway.
- A file set does not fit on the volume (a non-standard cluster made it several
  times larger) — refuse before writing starts, with both numbers in the text.

## Implementation order

1. ✔ The calculation core with default models + unit tests on the three
   existing records. Tested without the GUI.
2. ✔ The Calculation tab.
3. ✔ The JSON store with its schema and checks, the Records tab, volume
   measurement.
4. ✔ Calibration of both models and the Model tab.

Run: `.venv\Scripts\python.exe -m containerhelper`.
Tests: `.venv\Scripts\python.exe -m unittest discover -s . -p "test_*.py"`.

5. ✔ Sorting and manual column width, display units, transfer of data from the
   Calculation tab into a record.
6. ✔ Empty-volume measurements moved into their own file `Calibration.json`
   (schema 5).
7. ✔ Automatic collection of measurements through VeraCrypt: finding the
   binaries and parsing the version, three operations checked by fact,
   administrator rights, self-check, a dialog with progress and stopping.
8. ✔ Automatic collection of copy slack: file sets, generation on the volume,
   measuring left space on a freshly mounted volume, the free-space pre-check
   with skipping and measuring the rest later, progress by bytes written with
   phases and an estimate of the remaining time. Checking the prediction after
   the fact: `predicted_mib` and `predicted_safety_mib` in the record, miss
   columns on the Records and Calibration tabs. Factory copy-slack measurements
   — a mechanism modelled on the factory points.

9. ✔ The first copy-slack collection run and the analysis of its results: the
   supersede key by file set instead of by file count, repeating the unmount
   instead of waiting, `/force` only in cleanup. See "What the first copy-slack
   run showed".
10. ✔ Full run: thirty steps, not a single failure. The per-file slack is
    measured (1363 B per file, stable over 500…10 000 files),
    `factory_slack.json` is filled with seven measurements, the default
    `slack_per_file` is raised from 1280 to 1536, the `RECOMMENDED_MIB` grid is
    made denser with four sizes at the middles of the segments with a twofold
    step.

11. ✔ Four new points measured. What they showed:

| Size | Sag of the former chord | Segment |
|---|---|---|
| 768 MiB | +8 192 B | 512→1024 |
| 1536 MiB | **+233 472 B** | 1024→2048 |
| 3072 MiB | −1 048 576 B | 2048→4096 |
| 6144 MiB | 0 | 4096→8192 |

The 1024…2048 segment was confirmed independently and turned out worse than the
measurement at 1610 MiB had shown: the chord ran 233 KB below the curve. The two
estimates agreed with each other, too — the slope between the points 1536 and
1610 came out 0.1302 % against 0.1282 % on the 1536…2048 segment, that is, a
calibration measurement and a container for a file set, taken at different
times and in different ways, lay on one straight line.

The 2048…4096 segment, on the contrary, overestimated by exactly a megabyte,
and 4096…8192 turned out straight to the byte. The curve at the bottom is a
staircase: the slopes by segment go 0.215 %, 0.128 %, 0.128 %, 0.324 %, then
0.226 % twice. That is how `$LogFile` changes its size in steps at discrete
capacity thresholds, and a step can always hide between two points — which is
why the safety floor stays mandatory even where the grid is dense.

Practical result: the NTFS safety margin at 1.5 GiB dropped from 29 425 B to
zero — the risk there is no longer estimated but measured. There are 34 points
in the model.

Still to be done by hand: there is not a single measurement on **another
Windows build or another VeraCrypt version**. Everything known about the
metadata and about copy slack is known about one machine — that is what the
factory margin `FACTORY_MARGIN_BYTES` exists for.
