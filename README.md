# convert_cines

Recursively converts video files (default: `.cine`) to H.265 MP4 using ffmpeg. Supports brightness, contrast, and gamma adjustment and per-file trimming, with per-file rules based on filename patterns (from the CLI, a YAML file, or a CSV file), plus quality checking, resumable batch runs, and raw TIFF frame extraction.

## Table of Contents

- [Install](#install)
- [Requirements](#requirements)
- [convert_cines.py](#convert_cinespy)
  - [Flag reference](#flag-reference)
  - [Basic examples](#basic-examples)
  - [Image enhancement](#image-enhancement)
  - [Encoding options](#encoding-options)
  - [Test mode](#test-mode)
  - [Dry run](#dry-run)
  - [File matching enhancement rules](#file-matching-enhancement-rules)
  - [Trimming](#trimming)
  - [Output path behaviour](#output-path-behaviour)
  - [Progress tracking and resuming](#progress-tracking-and-resuming)
  - [Quality checks](#quality-checks)
  - [Removing verified CINE files](#removing-verified-cine-files)
  - [TIFF frame extraction](#tiff-frame-extraction)
- [find_matched_cine_mp4.py](#find_matched_cine_mp4py)
  - [Usage examples](#usage-examples)
  - [Metadata preservation](#metadata-preservation)
  - [Progress tracking and resuming](#progress-tracking-and-resuming-1)

---

## Install

```bash
git clone https://github.com/tytell/convert_cines.git
cd convert_cines
uv sync
```

[`uv`](https://docs.astral.sh/uv/) creates a `.venv` and installs the dependencies pinned in `uv.lock`. The project pins Python 3.13 (`.python-version`), which `uv` will download and manage automatically — no manual Python install needed.

`ffmpeg` is a separate system dependency and must be installed and on `PATH` (e.g. `brew install ffmpeg` on macOS, `apt install ffmpeg` on Debian/Ubuntu).

Once set up, run either script with `uv run`:

```bash
uv run convert_cines.py --help
uv run find_matched_cine_mp4.py --help
```

Both scripts declare their own dependencies inline via a PEP 723 script header (`convert_cines.py` needs `pyyaml`; `find_matched_cine_mp4.py` needs `numpy` and `pycine`), so `uv run convert_cines.py` / `uv run find_matched_cine_mp4.py` install what they need automatically on first use, even without `uv sync`.

## Requirements

- Python 3.12+, managed by `uv` (`.python-version` pins 3.13)
- `ffmpeg` on PATH
- `pyyaml` — used by `convert_cines.py` for `--config`; installed automatically by `uv run`
- `numpy`, `pycine` — used by `find_matched_cine_mp4.py` for the Spearman correlation check and CINE metadata extraction; installed automatically by `uv run`

---

## convert_cines.py

```
uv run convert_cines.py source_dir [options]
```

### Flag reference

Flags are grouped below by the feature area they belong to, matching the sections that follow.

#### General

| Flag | Default | Description |
|---|---|---|
| `source_dir` | _(required unless `--continue` supplies it)_ | Root directory to search |
| `--ext` | `.cine` | File extension to find |
| `--output-dir` | _(none)_ | Output root directory (mirrors source structure). If omitted, MP4s are written next to source files |
| `--suffix` | `''` | Suffix appended to output filenames before the extension |
| `--overwrite` | off | Overwrite existing output files |
| `-v`, `--verbose` | off | Print detailed processing info |

#### Image enhancement

| Flag | Default | Description |
|---|---|---|
| `--max-intensity FLOAT` | `1.0` | Brightness via `curves` filter, 0.0–1.0 (lower = brighter) |
| `--contrast FLOAT` | `1.0` | Contrast via `eq` filter |
| `--gamma FLOAT` | `1.0` | Gamma via `eq` filter |

#### Encoding options

| Flag | Default | Description |
|---|---|---|
| `--crf N` | `28` | H.265 quality (0–51; lower = better quality, larger file) |
| `--preset NAME` | `slow` | x265 encoding speed preset (`ultrafast` … `veryslow`) |
| `--fps FLOAT` | _(source rate)_ | Output frame rate |

#### Test mode

| Flag | Default | Description |
|---|---|---|
| `--test-count N` | _(none)_ | Process N files spread evenly across the full set |
| `--test-files FILE ...` | _(none)_ | Process specific files (overrides `--test-count`) |
| `--test-frames N` | _(none)_ | Encode only the first N frames per file |

#### Dry run

| Flag | Default | Description |
|---|---|---|
| `-n`, `--dry-run` | off | Print ffmpeg commands without running them |

#### File matching enhancement rules and trimming

| Flag | Default | Description |
|---|---|---|
| `--rule PATTERN:param=value,...` | _(none)_ | Per-file enhancement/trim rule (repeatable) |
| `--config PATH` | _(none)_ | YAML or CSV file with per-file overrides, dispatched on extension (`.csv` vs anything else) |
| `--start-frame N` | _(none)_ | Global trim start, 1-indexed frame number |
| `--end-frame N` | _(none)_ | Global trim end (inclusive): a number, `end`, or `end - N` |
| `--duration-frames N` | _(none)_ | Global trim duration in frames |
| `--start-sec T` | _(none)_ | Global trim start, in seconds |
| `--end-sec T` | _(none)_ | Global trim end: a number, `end`, or `end - T` |
| `--duration-sec T` | _(none)_ | Global trim duration in seconds |

#### Progress tracking and resuming

| Flag | Default | Description |
|---|---|---|
| `--progress-file PATH` | `<source_dir>/conversion_progress.csv` | Path for the progress CSV |
| `--no-progress` | off | Disable progress tracking entirely (also implied by `--dry-run`) |
| `--continue` | off | Resume a previous run using its stored parameters; `source_dir` may be omitted |
| `--restart` | off | Clear the progress file and start fresh |

#### Quality checks

| Flag | Default | Description |
|---|---|---|
| `--test-psnr` | off | Run inline Y-channel PSNR check after each conversion (auto-enabled in test mode) |
| `--psnr-frames N` | `5` | Frames sampled for the inline PSNR check |
| `--psnr-threshold T` | `30.0` | Minimum PSNR in dB for the inline check to pass; also used as the threshold for `--check` |
| `--check` | off | Run a thorough R-channel PSNR check (extracts grayscale PNGs from source & output); also runs for already-converted files |
| `--check-frames N` | `5` | Frames sampled for `--check` |
| `--check-dir DIR` | _(temp, cleaned up)_ | Save extracted check PNGs under DIR, mirroring the source tree |
| `--keep-frames` | off | Save extracted check-frame PNGs next to the output MP4 (ignored if `--check-dir` is set) |

#### Removing verified CINE files

| Flag | Default | Description |
|---|---|---|
| `--remove-cine` | off | After `--check`, write a removal script listing CINE files that passed |
| `--remove-script PATH` | `remove_cines.sh`/`.bat` next to `source_dir`'s parent | Base path for the generated removal scripts |

#### TIFF frame extraction

| Flag | Default | Description |
|---|---|---|
| `--tiff-dir DIR` | _(none)_ | Output root for extracted TIFFs; enables TIFF extraction, mirrors source tree |
| `--tiff-count N` | all frames | N evenly-distributed frames, or `'all'`. Mutually exclusive with `--tiff-every` |
| `--tiff-every M` | _(none)_ | Extract one frame (or pair) every M source frames. Mutually exclusive with `--tiff-count` |
| `--tiff-pair-sep N` | _(none)_ | Also extract a frame N frames after each anchor, producing pairs |

### Basic examples

```bash
# Convert all .cine files under current directory (output alongside source files)
uv run convert_cines.py .

# Convert to a separate output directory (mirrors source folder structure)
uv run convert_cines.py rawdata/videos --output-dir rawdata/videos/compressed

# Overwrite files that have already been converted
uv run convert_cines.py rawdata/videos --output-dir rawdata/videos/compressed --overwrite

# Convert files with a different file extension
# (for example, to convert H264 mp4s to H265)
uv run convert_cines.py rawdata/videos --ext .mp4
```

### Image enhancement

Three parameters control image enhancement. All default to no adjustment.

| Flag | Default | Effect |
|---|---|---|
| `--max-intensity FLOAT` | `1.0` | Brightness via `curves` filter. Range 0.0–1.0; lower values brighten the image by mapping that input level to full output brightness. |
| `--contrast FLOAT` | `1.0` | Contrast via `eq` filter. Values above 1.0 increase contrast. |
| `--gamma FLOAT` | `1.0` | Gamma via `eq` filter. Values below 1.0 brighten midtones; above 1.0 darken them. |

```bash
# Brighten dark footage
uv run convert_cines.py . --max-intensity 0.4

# Adjust contrast and gamma
uv run convert_cines.py . --contrast 1.3 --gamma 0.8

# Combine all three
uv run convert_cines.py . --max-intensity 0.5 --contrast 1.2 --gamma 0.85
```

### Encoding options

```bash
# Higher quality (larger files)
uv run convert_cines.py . --crf 22 --preset slow

# Set output frame rate (e.g. convert 1000fps high-speed footage to 30fps playback)
uv run convert_cines.py . --fps 30
```

| Flag | Default | Description |
|---|---|---|
| `--crf N` | `28` | H.265 quality (0–51; lower = better quality, larger file) |
| `--preset NAME` | `slow` | x265 encoding speed preset (`ultrafast` … `veryslow`) |
| `--fps FLOAT` | _(source rate)_ | Output frame rate |
| `--overwrite` | off | Overwrite existing output files (default: skip if output exists) |

### Test mode

Test mode lets you check enhancement settings on a small subset of files before committing to a full batch run. Test mode also auto-enables the inline PSNR check (see [Quality checks](#quality-checks)).

```bash
# Encode the first file found (first 50 frames only)
uv run convert_cines.py . --test-frames 50 --output-dir preview

# Encode 3 files spread evenly across the full set
uv run convert_cines.py . --test-count 3 --output-dir preview

# Encode specific files
uv run convert_cines.py . --test-files run1/shot3.cine run2/shot7.cine --output-dir preview
```

### Dry run

Print the ffmpeg commands that would be run, without executing them:

```bash
uv run convert_cines.py . --output-dir rawdata/videos/compressed -n
```

Progress tracking is automatically disabled during a dry run (no CSV is written).

### File matching enhancement rules

When different files in a batch need different enhancement settings, rules can map filename patterns to specific parameter values. Rules are checked in order; the first matching rule wins. Files that match no rule use the global `--max-intensity`, `--contrast`, and `--gamma` values.

The same rules (and the same `--rule`/`--config` mechanism) also carry per-file trim settings — see [Trimming](#trimming) below.

Patterns use shell-style wildcards (`*`, `?`) and are matched against the **path relative to `source_dir`**.

#### From the command line

Use `--rule "PATTERN:param=value,..."` (repeatable). Command line rules take priority over config file rules.

```bash
# One rule
uv run convert_cines.py . --rule "*night*:max_intensity=0.3"

# Multiple rules — checked in the order given
uv run convert_cines.py . \
  --rule "calibration/*:max_intensity=0.8,contrast=0.9" \
  --rule "*dark*:max_intensity=0.25,gamma=0.75" \
  --rule "*trial03*:max_intensity=1.0,contrast=0.7"
```

#### From a YAML config file

```bash
uv run convert_cines.py . --config enhance.yaml
```

The config file contains a `rules:` list. Each entry requires a `pattern` key plus any combination of `max_intensity`, `contrast`, `gamma`, and the trim fields described in [Trimming](#trimming).

```yaml
# enhance.yaml

rules:
  # Files shot in infrared — very dark, needs significant brightening
  - pattern: "*night*"
    max_intensity: 0.25
    gamma: 0.75

  # Calibration runs — slightly reduce intensity, lower contrast
  - pattern: "calibration/*"
    max_intensity: 0.8
    contrast: 0.9

  # Specific experiment subfolder with overlit conditions
  - pattern: "2026-03-*/*"
    max_intensity: 1.0
    contrast: 0.6
    gamma: 1.2
```

#### Combining CLI rules and a config file

CLI rules are checked before config file rules. Use this to override specific entries from a shared config on a given run:

```bash
uv run convert_cines.py . --config shared.yaml --rule "*night*:max_intensity=0.15"
```

#### Verbose output

Add `-v` to print which enhancement values are applied to each file (useful when rules are active):

```bash
uv run convert_cines.py . --config enhance.yaml -v -n
# [1/12] run1/night_001.cine → run1/night_001.mp4
#   enhancement: max_intensity=0.25 contrast=1.0 gamma=0.75
```

### Trimming

Individual files can be trimmed to a start/end (or start/duration) range, using the same `--rule`/`--config` mechanism as enhancement, plus six global `--start-*`/`--end-*`/`--duration-*` flags that apply to any file no rule overrides.

#### Fields and units

| Field | Frame form (1-indexed, inclusive) | Seconds form |
|---|---|---|
| Start | `start_frame` | `start_sec` |
| End | `end_frame` | `end_sec` |
| Duration | `duration_frames` | `duration_sec` |

- The unit is just a suffix on the field name — no separate units setting, and the same names work as a `--rule`/CLI value, a YAML key, or a CSV column header.
- `start_frame` is 1-indexed (`1` = the first frame). `end_frame` is inclusive (`end_frame: 100` keeps frame 100).
- For a given file, set at most one of each pair (`start_frame`/`start_sec`, `end_frame`/`end_sec`), and at most one of `end_*`/`duration_*` — both define the clip's length, so setting both is an error.
- `end_frame`/`end_sec` additionally accept:
  - `end` — the last frame/timestamp of the source file;
  - `end - x` — x frames/seconds before the last one, e.g. `end_frame: "end - 30"` drops the last 30 frames regardless of the file's actual length.

Trimming is always exact and never tied to keyframes: any field resolved in frame units triggers an ffmpeg `select`+`setpts` filter addressed by exact frame index (immune to fps rounding); a trim resolved purely in seconds uses `-ss`/`-t` placed **after** `-i` (ffmpeg's "accurate" output seek, which decodes and checks every frame instead of snapping to the nearest keyframe).

#### From the command line

```bash
# Same trim for every file: drop the first 2 seconds and the last 30 frames
uv run convert_cines.py . --start-sec 2.0 --end-frame "end - 30"

# Per-file trim via --rule
uv run convert_cines.py . --rule "*trial03*:start_frame=1,duration_frames=500"
```

#### From a YAML config file

```yaml
# trim.yaml
rules:
  - pattern: "*night*"
    max_intensity: 0.25       # enhancement and trim keys can mix in one rule
    start_sec: 1.0
    end_sec: "end - 0.5"

  - pattern: "calibration/*"
    start_frame: 1
    duration_frames: 500
```

#### From a CSV config file

`--config` also accepts a `.csv` file (dispatched by extension): one row per file/pattern, with a `pattern` or `filename` column (either name works) plus any of the enhancement/trim fields above as columns. A column that's missing entirely falls back to the CLI default for every row; a blank cell in a present column does the same for that row. Any column name that isn't a recognized field is a hard error, to catch typos before any file is touched.

```csv
pattern,start_frame,end_frame,max_intensity
*trial01*,1,500,
*trial02*,50,end - 10,0.6
*trial03*,,,0.8
```

```bash
uv run convert_cines.py . --config trim.csv
```

#### Verbose output

`-v` also prints the resolved trim window for each file:

```bash
uv run convert_cines.py . --config trim.yaml -v -n
# [1/12] run1/night_001.cine → run1/night_001.mp4
#   enhancement: max_intensity=0.25 contrast=1.0 gamma=1.0
#   trim: time:1.000000:2.500000
```

### Output path behaviour

| Scenario | Output location |
|---|---|
| No `--output-dir` | MP4 written next to source file, same directory |
| `--output-dir /out` | Source directory tree mirrored under `/out` |

Example with mirrored output:
```
source:  rawdata/run1/sub/shot1.cine
output:  rawdata/compressed/run1/sub/shot1.mp4
```

`--suffix` inserts text before the extension, e.g. `--suffix _preview` writes `shot1_preview.mp4`.

### Progress tracking and resuming

Progress tracking is **on by default**. Every run writes a human-readable CSV (`<source_dir>/conversion_progress.csv` unless overridden) recording each file's status, encoding parameters, and check results. The file is written atomically after every status change, so an interrupted run — Ctrl-C, a crash, an ffmpeg error — never leaves a corrupt progress file, and can always be resumed.

Each file progresses through statuses as it's processed: `queued` → `converted` → `psnr_passed`/`psnr_failed` → `check_passed`/`check_failed`. Only `check_passed`/`check_failed` are treated as fully done and skipped outright on resume; a file left at `converted` will still get its checks run, and one left at `psnr_passed`/`psnr_failed` will still run `--check` if requested.

If a file's encoding parameters (`crf`, `preset`, `fps`, the resolved enhancement filter, or the resolved trim window) differ from what's stored, it's automatically re-queued and force-reconverted, even without `--overwrite` — so changing a `--rule` (including a trim rule, or an `end - x` rule whose resolved value shifted because the source file itself changed) only re-processes the files it affects.

```bash
# Normal run — progress tracked automatically
uv run convert_cines.py rawdata/videos --output-dir compressed --check

# ...run gets interrupted...

# Resume where it left off, reusing all stored parameters (source_dir can be omitted)
uv run convert_cines.py --continue

# Resume a run tracked by a specific progress file
uv run convert_cines.py --continue --progress-file rawdata/videos/conversion_progress.csv

# Start over from scratch, ignoring prior progress
uv run convert_cines.py rawdata/videos --output-dir compressed --restart

# Disable progress tracking entirely (no CSV written)
uv run convert_cines.py rawdata/videos --no-progress
```

`--continue` restores stored conversion parameters (source/output dirs, `crf`, `preset`, `fps`, enhancement settings, global trim flags, rules, PSNR/check thresholds) from the progress file. Run-mode flags — `--check`, `--verbose`, `--remove-cine`, etc. — are **not** stored and must be passed again on each run.

| Flag | Default | Description |
|---|---|---|
| `--progress-file PATH` | `<source_dir>/conversion_progress.csv` | Path for the progress CSV. Created automatically |
| `--no-progress` | off | Disable progress tracking entirely (no CSV file) |
| `--continue` | off | Resume a previous run using all parameters stored in the progress file; `source_dir` may be omitted |
| `--restart` | off | Clear the progress file and start fresh; re-converts all files regardless of prior status |

### Quality checks

Two independent checks are available to verify a conversion didn't lose or corrupt content.

**Inline PSNR check** (`--test-psnr`, auto-enabled in test mode) — runs immediately after each conversion. Samples a few frames from the filtered source and the compressed output and compares them on the Y (luma) channel using ffmpeg's `psnr` filter. Fast, since no temp files are written.

**Thorough check** (`--check`) — extracts grayscale R-channel PNGs from both the source and the output and compares those, which avoids YUV↔RGB conversion artifacts. Slower, but more accurate, and also runs against files that were skipped because they were already converted in a prior run. Skipped when `--test-frames` is set. When `--check` is active, the inline PSNR check is automatically suppressed unless `--test-psnr` is also passed explicitly — the thorough check subsumes it.

Both checks sample source and output at *proportional* positions in the output's own duration. For a trimmed file this is automatically offset into the corresponding window of the source, so a trimmed clip is still compared against the right part of the source rather than against, say, 50% through the entire untrimmed recording.

```bash
# Force inline PSNR check on a normal (non-test) run
uv run convert_cines.py . --output-dir compressed --test-psnr

# Thorough R-channel check after conversion (and for already-converted files)
uv run convert_cines.py . --output-dir compressed --check

# Save extracted check frames for visual inspection, mirroring the source tree
uv run convert_cines.py . --output-dir compressed --check --check-dir /tmp/check_frames

# ...or save them next to each output file instead
uv run convert_cines.py . --output-dir compressed --check --keep-frames
```

| Flag | Default | Description |
|---|---|---|
| `--test-psnr` | off | Run inline PSNR check after each conversion (also auto-enabled in any test mode) |
| `--psnr-frames N` | `5` | Frames to sample for the inline check |
| `--psnr-threshold T` | `30.0` | Minimum acceptable PSNR in dB — used by both the inline check and `--check` |
| `--check` | off | Run the thorough R-channel PSNR check |
| `--check-frames N` | `5` | Frames to sample for `--check` |
| `--check-dir DIR` | _(temp, cleaned up)_ | Save extracted PNGs under DIR, mirroring the source tree |
| `--keep-frames` | off | Save extracted check frames next to the output MP4 (ignored if `--check-dir` is set) |

### Removing verified CINE files

`--remove-cine` writes a shell (`.sh`) and batch (`.bat`) script listing every CINE file whose thorough `--check` passed, for you to review and run manually — it never deletes anything itself.

For safety, the **first CINE file encountered in each source subdirectory** (sorted by filename) is always kept out of the removal list, even if it passed `--check`, so one original per directory remains as a spot-check reference.

Because pass/fail status is read from the progress log, `--remove-cine` also picks up files that passed `--check` in an earlier run — you can run `--check` once, then add `--remove-cine` on a later `--continue` run without re-checking anything.

```bash
uv run convert_cines.py rawdata/videos --output-dir compressed --check --remove-cine

# review the generated script, then run it:
bash remove_cines.sh      # macOS/Linux
remove_cines.bat          # Windows
```

| Flag | Default | Description |
|---|---|---|
| `--remove-cine` | off | After `--check`, write a shell/bat script listing CINE files that passed, for manual review and deletion |
| `--remove-script PATH` | `remove_cines.sh`/`.bat` next to `source_dir`'s parent directory | Base path for the generated removal scripts |

### TIFF frame extraction

`--tiff-dir` extracts individual frames as raw, uncompressed 16-bit grayscale TIFFs — no enhancement filters are ever applied, and no quality check is run on TIFF output. Each source file gets its own subdirectory (named after the source stem) under `--tiff-dir`, mirroring the source directory tree. Extraction runs after conversion (or for already-converted/skipped files) and is skipped if the output subdirectory already contains TIFFs, unless `--overwrite` is set.

Note that trim rules (see [Trimming](#trimming)) have no effect on TIFF extraction — it always extracts from the full, untrimmed source file.

Three selection modes, controlled by `--tiff-count` / `--tiff-every` (mutually exclusive):

```bash
# Extract every frame as TIFF (the default when neither flag below is given)
uv run convert_cines.py . --tiff-dir tiffs

# 10 evenly spaced frames per file
uv run convert_cines.py . --tiff-dir tiffs --tiff-count 10

# Every 100th frame
uv run convert_cines.py . --tiff-dir tiffs --tiff-every 100
```

Add `--tiff-pair-sep N` to also extract a second frame N source frames after each anchor, producing consecutive pairs in the output (e.g. for PIV-style frame-pair analysis):

```bash
# Every 100th frame, plus its partner 3 frames later
uv run convert_cines.py . --tiff-dir tiffs --tiff-every 100 --tiff-pair-sep 3

# 10 evenly spaced pairs, each pair separated by 3 frames
uv run convert_cines.py . --tiff-dir tiffs --tiff-count 10 --tiff-pair-sep 3
```

Output layout:
```
tiff_dir/
  rel/path/to/
    shot1/
      shot1_0001.tiff
      shot1_0002.tiff
      ...
```

| Flag | Default | Description |
|---|---|---|
| `--tiff-dir DIR` | _(none — disabled)_ | Output root for extracted TIFFs; enables TIFF extraction; mirrors the source tree, one subdirectory per source file |
| `--tiff-count N` | all frames | Integer for N evenly-distributed frames, or the literal `'all'`. Mutually exclusive with `--tiff-every` |
| `--tiff-every M` | _(none)_ | Extract one frame (or pair) every M source frames. Mutually exclusive with `--tiff-count` |
| `--tiff-pair-sep N` | _(none)_ | Also extract a frame N source frames after each anchor. Requires `--tiff-every` or `--tiff-count N` (not `'all'`); if combined with `--tiff-every M`, N must be less than M |

---

## find_matched_cine_mp4.py

After a batch conversion, use `find_matched_cine_mp4.py` to find original source files that have a converted counterpart in the same directory, verify the content matches via **Spearman rank correlation**, and generate a removal script for the originals (the larger file in each passing pair). Spearman correlation is invariant to any monotonic per-pixel transform (gain, gamma, curves), so it tolerates the brightness/contrast/gamma adjustments `convert_cines.py` may have applied, while still catching genuine content mismatches. Before queuing a CINE for removal, the tool preserves its camera metadata as an XML file next to the MP4 — using the existing Phantom-generated XML sidecar if present, or extracting it from the binary header with `pycine` if not.

### Usage examples

```bash
# Find matched pairs, check correlation, write removal script
uv run find_matched_cine_mp4.py rawdata/

# Dry run: list matches without running the check
uv run find_matched_cine_mp4.py rawdata/ --dry-run

# Adjust threshold (lower tolerates more difference; default 0.99)
uv run find_matched_cine_mp4.py rawdata/ --threshold 0.95

# Save extracted frames to a separate directory (mirrors source tree)
uv run find_matched_cine_mp4.py rawdata/ --check-dir /tmp/frames

# Save extracted frames next to the video files
uv run find_matched_cine_mp4.py rawdata/ --keep-frames
```

| Flag | Default | Description |
|---|---|---|
| `source_dir` | _(required)_ | Root directory to scan |
| `--ext LIST` | `.cine,.mp4,.avi,.mov` | Comma-separated extensions to consider |
| `--threshold T` | `0.99` | Minimum Spearman rank correlation to count a pair as matching |
| `--frames N` | `5` | Number of frames to sample per pair |
| `--check-dir DIR` | _(temp, cleaned up)_ | Save extracted grayscale PNGs under DIR, mirroring source tree |
| `--keep-frames` | off | Save extracted check frames next to the video files |
| `--remove-script PATH` | `remove_originals` next to `source_dir` | Base path for `.sh` / `.bat` removal scripts |
| `--no-metadata` | off | Skip XML metadata extraction/copy for CINE files |
| `--progress-file PATH` | `<source_dir>/find_matched_progress.csv` | Path for the progress CSV |
| `--no-progress` | off | Disable progress tracking entirely (no CSV file) |
| `--continue` | off | Resume a previous run: skip pairs already passed or failed |
| `--restart` | off | Clear the progress file and start fresh |
| `-n`, `--dry-run` | off | List matches without running checks or writing scripts |
| `-v`, `--verbose` | off | Print per-frame correlation values |

### Metadata preservation

Metadata is only extracted/copied when the CINE is the file being removed from a passing pair — if the MP4 (or other surviving format) is larger and gets removed instead, no XML is written. Pass `--no-metadata` to skip this step entirely.

### Progress tracking and resuming

Like `convert_cines.py`, progress tracking is **on by default**: each run writes a CSV (`<source_dir>/find_matched_progress.csv` unless overridden) tracking every pair's status (`queued` → `passed`/`failed`/`error`), written atomically so an interrupted run can always be resumed.

```bash
# Normal run — progress tracked automatically
uv run find_matched_cine_mp4.py rawdata/

# ...run gets interrupted...

# Resume: pairs already marked passed/failed are skipped
uv run find_matched_cine_mp4.py rawdata/ --continue

# Start over from scratch, ignoring prior progress
uv run find_matched_cine_mp4.py rawdata/ --restart
```

`--continue` warns if `--threshold` has changed since the stored run, since previously-passed/failed results used the old threshold. Pairs left at `error` status (an exception during the check, e.g. a corrupt file) are retried automatically on resume, unlike `passed`/`failed` pairs.

<!--
To render this file to HTML:
pandoc README.md -o README.html -s --css=pandoc.css --embed-resources --standalone
-->
