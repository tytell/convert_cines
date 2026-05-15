# compress_cines — Design Document

## Overview

A single-file Python script that recursively finds video files and converts them to H.265 MP4 using ffmpeg. Phantom `.cine` files (10-bit grayscale in a 16-bit container) are the default target; ffmpeg handles the bit-depth conversion automatically.

## Usage

```bash
# Convert all .cine files under current directory in-place
compress_cines .

# Output to a separate directory (mirrors source structure)
compress_cines /data/experiments --output-dir /data/mp4s

# Brightness/contrast/gamma adjustments
compress_cines /data/experiments --gain 0.6 --contrast 1.2 --gamma 0.9

# Test: encode only the first 3 files, first 5 seconds each
compress_cines /data/experiments --test --test-count 3 --test-duration 5 --output-dir /tmp/preview

# Skip files already converted
compress_cines /data/experiments --output-dir /data/mp4s --skip-existing
```

## Implementation

Single file: `compress_cines.py`

### Dependencies

- Python stdlib only (`argparse`, `subprocess`, `os`, `pathlib`)
- `ffmpeg` on PATH

No third-party Python packages needed. ffmpeg is invoked via `subprocess`.

### Arguments

| Flag | Default | Description |
|---|---|---|
| `source_dir` | _(required)_ | Root directory to search |
| `--ext` | `.cine` | File extension to find |
| `--output-dir` | _(none)_ | Output root. If omitted, MP4s go next to source files. |
| `--skip-existing` | `False` | Skip if output MP4 already exists |
| `--overwrite` | `False` | Overwrite existing output |
| `--crf` | `28` | H.265 CRF quality (0–51) |
| `--preset` | `medium` | x265 preset |
| `--fps` | _(auto)_ | Output frame rate |
| `--gain` | `1.0` | Brightness gain via `curves` filter (0.0–1.0; lower = brighter) |
| `--contrast` | `1.0` | Contrast via `eq` filter |
| `--gamma` | `1.0` | Gamma via `eq` filter |
| `--test` | `False` | Test mode |
| `--test-count` | `1` | Number of files to process in test mode |
| `--test-files` | _(none)_ | Specific files to process in test mode (overrides `--test-count`) |
| `--test-duration` | _(none)_ | Seconds to encode per file in test mode |
| `--dry-run` | `False` | Print commands without running |

### Filter Chain

Gain uses the `curves` filter; contrast and gamma use the `eq` filter. Both are omitted when at defaults.

```
--gain 0.6 --contrast 1.2 --gamma 0.9
→ -vf "curves=all='0/0 0.6/1 1/1',eq=contrast=1.2:gamma=0.9"

--gain 0.6 only
→ -vf "curves=all='0/0 0.6/1 1/1'"

no enhancement
→ (no -vf argument)
```

### Output Path

```python
def output_path(src, source_root, output_dir, suffix=""):
    stem = src.stem + suffix
    if output_dir is None:
        return src.with_name(stem + ".mp4")
    rel = src.relative_to(source_root)
    return output_dir / rel.parent / (stem + ".mp4")
```

### ffmpeg Invocation

```bash
ffmpeg -i input.cine \
  [-vf "curves=all='0/0 X/1 1/1'[,eq=contrast=C:gamma=G]"] \
  -vcodec libx265 -crf 28 -preset medium \
  -pix_fmt yuvj420p -tag:v hvc1 -an \
  output.mp4
```

### Progress

Print one line per file to stdout:

```
[1/12] input.cine → output.mp4
```

ffmpeg's own stderr output provides frame-level progress (not suppressed, so it naturally shows in the terminal).

### Structure

```python
def build_vf(max_intensity, contrast, gamma): ...
def output_path(src, source_root, output_dir): ...
def find_files(source_dir, ext): ...
def build_cmd(src, dst, args, max_intensity, contrast, gamma): ...
def load_rules(args): ...           # parses --rule flags + optional YAML config
def resolve_enhancement(rel_path, rules, args): ...  # first-matching-rule lookup
def main(): ...

if __name__ == "__main__":
    main()
```

---

## Per-File Enhancement Rules

Some files in a batch may need different brightness/contrast/gamma settings (e.g. different experiments, different lighting conditions). Rules match files by wildcard pattern and override the global enhancement flags for matching files.

### Pattern Matching

Patterns are matched against the **path relative to `source_dir`** using `fnmatch` (shell-style wildcards: `*`, `?`, `[seq]`). This allows patterns like:

- `*dark*` — any file with "dark" in the name
- `run3/*` — all files under the `run3/` subdirectory
- `*/calibration_*` — files named `calibration_*` in any subdirectory

### Rule Resolution

Rules are checked **in order; first match wins**. If no rule matches, the global flags (`--max-intensity`, `--contrast`, `--gamma`) apply. Each rule only specifies the parameters it overrides; unspecified parameters fall back to the global flag values.

### YAML Config File (`--config FILE`)

```yaml
# example.yaml
rules:
  - pattern: "*night*"
    max_intensity: 0.3
    gamma: 0.8
  - pattern: "run3/*"
    max_intensity: 0.5
    contrast: 1.2
```

### CLI Rules (`--rule`, repeatable)

Format: `"PATTERN:param=value[,param=value,...]"`

```bash
convert_cines . \
  --rule "*night*:max_intensity=0.3,gamma=0.8" \
  --rule "run3/*:max_intensity=0.5,contrast=1.2"
```

CLI rules are prepended to config-file rules, so they take priority.

### Implementation

```python
from fnmatch import fnmatch

def load_rules(args):
    rules = []
    for r in (args.rule or []):
        pattern, _, params_str = r.partition(":")
        params = dict(kv.split("=") for kv in params_str.split(",") if kv)
        rules.append((pattern, {k: float(v) for k, v in params.items()}))
    if args.config:
        import yaml
        data = yaml.safe_load(open(args.config))
        for entry in data.get("rules", []):
            entry = dict(entry)
            pattern = entry.pop("pattern")
            rules.append((pattern, entry))
    return rules

def resolve_enhancement(rel_path, rules, args):
    for pattern, overrides in rules:
        if fnmatch(str(rel_path), pattern):
            return (
                overrides.get("max_intensity", args.max_intensity),
                overrides.get("contrast", args.contrast),
                overrides.get("gamma", args.gamma),
            )
    return args.max_intensity, args.contrast, args.gamma
```

`pyyaml` is imported lazily inside `load_rules`, so the script works without it when no `--config` is used.

---

## Conversion Quality Check

Two distinct quality checks are provided, with different purposes and methods.

### New file: `check_conversion.py`

All check logic lives in a separate module imported by `convert_cines.py`. No external Python dependencies — all comparison is done via ffmpeg/ffprobe.

### Shared data structures

```python
@dataclass
class FrameResult:
    index: int        # 0-based
    timestamp: float  # seconds into the video
    psnr: float       # PSNR in dB (Y-channel for inline check; R-channel for --check)
    passed: bool      # psnr >= threshold (or inf)

@dataclass
class CheckResult:
    src: Path
    dst: Path
    frames: list[FrameResult]
    passed: bool        # all frames passed
    threshold: float    # stored for reporting
    error: str | None   # set if check could not complete
```

### Shared utilities

**`_get_duration(path: Path) -> float`**
```bash
ffprobe -v error -select_streams v:0 -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 <path>
```
Parse float from stdout. Raise `RuntimeError` on non-zero exit.

**`_timestamps(duration, n_frames) -> list[float]`**
Distribute evenly: `[(i + 0.5) * duration / n_frames for i in range(n_frames)]`. Both source and output durations are queried separately and each gets its own timestamp list, keeping seek positions proportionally aligned even when `--fps` changes the output duration (e.g. a 100fps CINE converted with `--fps 10` produces an MP4 10× longer).

**`_parse_psnr_y(stderr: str) -> float`**

Regex: `r"\[Parsed_psnr_0\s+@\s+\S+\]\s+PSNR\s+y:(\S+)"`

Convert `"inf"` → `float("inf")`. Raise `RuntimeError` if no match.

---

## Inline PSNR check (automatic after each conversion)

Runs automatically after each successful `ffmpeg` conversion. Compares N sampled frames between the filtered source and the compressed output using ffmpeg's `psnr` lavfi filter. Uses the **Y (luma) channel**, which equals the R channel for grayscale sources (R=G=B → Y=R under the full-range YUV conversion). No temp files are needed since ffmpeg seeks directly in both files.

### New flags

| Flag | Default | Description |
|---|---|---|
| `--test-psnr` | `False` | Enable inline PSNR check (also auto-enabled in any test mode) |
| `--psnr-frames N` | `5` | Frames to sample per file for inline PSNR check |
| `--psnr-threshold T` | `30.0` | Minimum acceptable PSNR in dB (luma channel) |

**When it runs:** only when `test_mode` is active (any of `--test-count`, `--test-files`, `--test-frames`) or `--test-psnr` is passed explicitly. Not run on full batch conversions unless requested.

When `--test-frames` is set the output is truncated, so timestamps are computed from the **MP4 duration** (not the source duration) to stay within the encoded portion.

### Public entry point

```python
def verify_psnr(
    src: Path,
    dst: Path,
    vf: str | None,
    *,
    n_frames: int = 5,
    threshold: float = 30.0,
    verbose: bool = False,
) -> CheckResult:
```

### Frame comparison

For each timestamp `t`, a single ffmpeg command compares one frame from each file:

```bash
ffmpeg -ss <t> -i <src.cine> -ss <t> -i <dst.mp4> \
  -filter_complex "[0:v]<vf>[a]; [a][1:v]psnr" \
  -f null -
```

When `vf` is `None`, the filtergraph simplifies to `"[0:v][1:v]psnr"`.

Parse `psnr_y` from stderr. No temp files; ffmpeg seeks in both inputs directly.

### Output (inline, one line per file)

```
  psnr: PASS  (avg 38.4 dB, min 34.1 dB, 5/5 frames)
  psnr: FAIL  (avg 22.7 dB, min 19.3 dB, 3/5 frames passed, threshold 30.0 dB)
  psnr: ERROR  <message>
```

Verbose (`-v`), one line per frame:
```
    frame 1/5 t=2.10s  PSNR=41.2 dB  [PASS]
```

---

## `--check` mode (standalone thorough check)

A separate, more thorough check for already-converted files. Extracts uncompressed TIFF frames from both the source CINE and the converted MP4, then computes PSNR on the **R channel only**. This avoids YUV↔RGB conversion artifacts: when the MP4 is decoded, the YUV→RGB matrix can produce slightly different R, G, B values even for a grayscale source; using only R gives a clean single-channel comparison.

### New flags

| Flag | Default | Description |
|---|---|---|
| `--check` | `False` | Run thorough TIFF-based R-channel PSNR check |
| `--check-frames N` | `5` | Frames to sample per file |
| `--check-threshold T` | `30.0` | Minimum acceptable R-channel PSNR in dB |
| `--check-dir DIR` | _(none)_ | Save extracted TIFFs here for visual inspection (temp dir if omitted) |

Check runs after conversion AND for already-converted (skipped) files. Skipped when `--test-frames` is set.

### Public entry point

```python
def check_file(
    src: Path,
    dst: Path,
    vf: str | None,
    *,
    n_frames: int = 5,
    threshold: float = 30.0,
    check_dir: Path | None = None,
    verbose: bool = False,
) -> CheckResult:
```

### Frame extraction (directly to grayscale PNG)

Extract the R channel in a single ffmpeg pass per frame by appending `extractplanes=r,format=gray` to the filter chain:

```bash
# From source CINE (enhancement filters + grayscale normalization):
ffmpeg -y -ss <t> -i <src.cine> -vf "<vf>,format=gray" -vframes 1 src_NNN.png

# From converted MP4 (grayscale normalization only — enhancement already baked in):
ffmpeg -y -ss <t> -i <dst.mp4> -vf "format=gray" -vframes 1 dst_NNN.png
```

`format=gray` normalizes to 8-bit grayscale: for the CINE (`gray16le`), it pulls the single luma plane directly; for the MP4 (`yuvj420p`), it extracts the Y channel. This avoids the YUV→RGB conversion matrix and any inter-channel artifacts. When `vf` is `None`, both commands use just `format=gray`.

PNGs are written to `check_dir` if provided (named `<src_stem>_src_NNN.png` / `<src_stem>_dst_NNN.png`) for visual inspection, otherwise to a `tempfile.TemporaryDirectory()` cleaned up automatically.

### R-channel PSNR

```bash
ffmpeg -i src_NNN.png -i dst_NNN.png -lavfi "[0:v][1:v]psnr" -f null -
```

`psnr_y` of a single-channel grayscale image equals the R-channel PSNR. Parsed from stderr using `_parse_psnr_y`.

### Output

```
  check: PASS  (avg 38.4 dB, min 34.1 dB, 5/5 frames)
  check: FAIL  (avg 22.7 dB, min 19.3 dB, 3/5 frames passed, threshold 30.0 dB)
  check: ERROR  Output not found: /path/to/file.mp4
```

Verbose with `--check-dir` also prints TIFF paths for inspection.

---

## Integration into `convert_cines.py`

### New imports
```python
import math
from check_conversion import verify_psnr, check_file
```

### Main loop changes

`vf` is computed once from `build_vf(max_intensity, contrast, gamma)` before calling `build_cmd`.

After successful conversion:
```python
run_psnr = test_mode or args.test_psnr
if run_psnr:
    result = verify_psnr(src, dst, vf, n_frames=args.psnr_frames,
                         threshold=args.psnr_threshold, verbose=args.verbose)
    print_psnr_result(result)
    if not result.passed:
        psnr_failed += 1

if args.check and not args.test_frames:
    result = check_file(src, dst, vf, n_frames=args.check_frames,
                        threshold=args.check_threshold, check_dir=args.check_dir,
                        verbose=args.verbose)
    print_check_result(result)
    if not result.passed:
        check_failed += 1
```

For skipped files (already converted), inline PSNR is skipped (no new conversion happened); `--check` still runs.

### Summary line
```
Done: 10 succeeded, 2 skipped, 0 failed.
```
With `--test-psnr` or test mode active:
```
Done: 10 succeeded, 2 skipped, 0 failed.  PSNR: 1 failed.
```
With `--check`:
```
Done: 10 succeeded, 2 skipped, 0 failed.  Check: 0 failed.
```

### Module structure of `check_conversion.py`

```python
# Shared
def _get_duration(path) -> float
def _timestamps(duration, n_frames) -> list[float]
def _parse_psnr_y(stderr) -> float

# Inline PSNR check
def verify_psnr(src, dst, vf, *, n_frames, threshold, verbose) -> CheckResult

# --check mode
def _extract_gray_png(src, timestamp, vf, out_path) -> None
def check_file(src, dst, vf, *, n_frames, threshold, check_dir, verbose) -> CheckResult
```

---

## Progress Log and Resume

For long batch runs that may be interrupted, pass `--progress-file PATH` to create a human-readable CSV that tracks each file's status. The file is written atomically (write to `.tmp`, then `os.replace()`) after every status transition, so a crash never leaves a partial file.

### New file: `progress_log.py`

Implements `ProgressLog` and `FileRecord`. No external dependencies.

### CSV format

```csv
source,output,status,crf,preset,fps,vf,psnr_avg,psnr_min,check_avg,check_min,updated,error
/data/f1.cine,/out/f1.mp4,check_passed,28,slow,,curves=...,52.30,45.10,54.20,46.80,2026-04-09T10:23:45Z,
/data/f2.cine,/out/f2.mp4,psnr_failed,28,slow,,,22.50,19.30,,,2026-04-09T10:15:22Z,
/data/f3.cine,/out/f3.mp4,queued,28,slow,,,,,,,2026-04-09T09:00:00Z,
```

`psnr_avg`/`psnr_min` hold results from the inline PSNR check; `check_avg`/`check_min` hold results from `--check`. PSNR values of `inf` are stored as the literal string `inf`.

### Status values

| Status | Meaning |
|---|---|
| `queued` | Discovered; not yet processed |
| `converted` | ffmpeg succeeded; checks not yet run |
| `conversion_failed` | ffmpeg returned non-zero |
| `psnr_passed` | Inline PSNR check passed |
| `psnr_failed` | Inline PSNR check failed |
| `check_passed` | Thorough `--check` passed — final good state |
| `check_failed` | Thorough `--check` failed — final failed state |

### Initial registration (pre-loop)

Before the main loop, all discovered files are registered in a single pass and the log is written once. For each **new** file (not yet in the log):

- If the output already exists **and `--overwrite` is false**: status = `converted`
- Otherwise: status = `queued`

Existing records are not overwritten unless their conversion options changed (see below).

### Option-mismatch detection

The stored fields `crf`, `preset`, `fps`, and `vf` (the resolved ffmpeg `-vf` filter string for that file) are compared against the current invocation's resolved values. If they differ for an existing record:

- Status is reset to `queued`
- `psnr_*` and `check_*` result fields are cleared
- The output will be re-converted with `-y` (force overwrite), regardless of `--overwrite`
- A message is printed: `re-queued (options changed: crf 28→32)`

Per-file rules (via `--rule` or `--config`) affect only the `vf` column of the files they match, so changing a rule re-queues only those affected files.

### Resume logic

| Stored status | Action on resume |
|---|---|
| `check_passed` or `check_failed` | Skip entirely |
| `psnr_failed` | Skip conversion and PSNR; run `--check` if configured |
| `psnr_passed` | Skip conversion and PSNR; run `--check` if configured |
| `converted` | Skip conversion; run PSNR and/or `--check` as configured |
| `conversion_failed` | Retry conversion |
| `queued` | Run everything |

### Inline PSNR and `--check` interaction

When `--check` is active, the inline PSNR check is suppressed unless `--test-psnr` is explicitly passed. The thorough check subsumes the inline check. Formally:

```python
run_psnr = (test_mode or args.test_psnr) and (args.test_psnr or not args.check)
```

### New arguments

| Flag | Default | Description |
|---|---|---|
| `--progress-file PATH` | `<source_dir>/conversion_progress.csv` | Path for the progress CSV |
| `--no-progress` | `False` | Disable progress tracking entirely |
| `--continue` | `False` | Resume using stored parameters; `source_dir` may be omitted |
| `--restart` | `False` | Clear the progress file and start fresh |

Progress tracking is **on by default**. The CSV is written to `<source_dir>/conversion_progress.csv` unless overridden. Use `--no-progress` to disable or `--progress-file` to specify a different path.

`--continue` loads all stored conversion parameters (source_dir, output_dir, crf, preset, fps, filter settings, quality thresholds, rules, config) from the progress file and applies them to the current run. `source_dir` may be omitted from the command line when using `--continue`; it is read from the file. Run-mode flags (`--check`, `--verbose`, `--remove-cine`, etc.) are **not stored** and must be re-specified each run.

`--restart` deletes the progress file before running, so all files are re-queued from scratch (equivalent to a first run).

### Module structure of `progress_log.py`

```python
@dataclass
class FileRecord:
    source: str; output: str; status: str
    crf: str; preset: str; fps: str; vf: str
    psnr_avg: str; psnr_min: str; check_avg: str; check_min: str
    updated: str; error: str
    def options_match(self, crf, preset, fps, vf) -> bool: ...

class ProgressLog:
    def __init__(self, path: Path): ...
    def load(self) -> None: ...                    # reads CSV into dict keyed by source path
    def add_or_reconcile(                          # returns (status, force_overwrite, reason_or_None)
        self, src, dst, crf, preset, fps, vf, overwrite
    ) -> tuple[str, bool, str | None]: ...
    def get(self, src: Path) -> FileRecord | None: ...
    def update(self, src, status, *, psnr_avg, psnr_min,
               check_avg, check_min, error) -> None: ...   # updates + atomic write
    def write_initial(self) -> None: ...           # one-shot write after pre-loop
    def _write(self) -> None: ...                  # write .tmp then os.replace()
```

---

## `find_matched_cine_mp4.py` — find and verify matched source/converted pairs

A companion script for cleaning up after a batch conversion. It finds video files that share the same stem in the same directory, verifies the content matches via PSNR, and generates a removal script for the larger file in each passing pair.

### Discovery and matching

Walk `source_dir` recursively and collect all files whose suffix (lowercased) is in the configured extension set (default: `.cine`, `.mp4`, `.avi`, `.mov`). Group by `(parent_dir, stem)`. Any group with two or more files is a _match group_.

Within a match group with exactly two files, one pair is checked. For groups with three or more files, every unique pair is checked (e.g. `.cine` + `.mp4` + `.avi` → three pairs). In practice, groups of two are the common case.

### PSNR check

Reuses `_extract_gray_png` and the PSNR pipeline from `check_conversion.py`. For each pair:

1. Get durations of both files via `_get_duration`.
2. Sample `N` timestamps from each file via `_timestamps` (proportionally distributed, first 3 skipped to avoid keyframe artifacts).
3. Extract a grayscale PNG from each file at each timestamp using `_extract_gray_png` — with `vf=None` for both, since the conversion filter is unknown.
4. Compare each PNG pair with ffmpeg's `psnr` lavfi filter; parse `psnr_y` from stderr.
5. A pair passes if all sampled frames meet `--psnr-threshold`.

**Why the default threshold is lower (20 dB vs 30 dB):** `convert_cines.py` re-applies the known `vf` filter to the source before comparing, making it a near-identical comparison. Here the filter is unknown — the source may have been brightened, contrast-adjusted, or gamma-corrected. PSNR between raw original and enhanced encoded copy is typically 15–25 dB for a correct conversion. The 20 dB default is meant to confirm _same content_, not _lossless copy_.

### Metadata preservation

Before a CINE file can be safely deleted, its camera parameters must be preserved alongside the surviving MP4. Two sources are handled:

**Case 1 — XML sidecar already exists** (e.g. `shot1.cine` + `shot1.xml` saved by Phantom software): copy the XML to sit next to the MP4 if the two files are in different directories; if they're already co-located, do nothing.

**Case 2 — No XML sidecar**: use `pycine.file.read_header` to extract the binary header and generate a new XML. The output mirrors the Phantom sidecar schema (`CineFileHeader`, `BitmapInfoHeader`, `CameraSetup`) so it is human-readable and compatible with any tooling that already parses Phantom XML files.

Fields written from `read_header`:

| XML element | pycine source | Notes |
|---|---|---|
| `CineFileHeader/*` | `header['cinefileheader']` | FirstMovieImage, ImageCount, TriggerTime, … |
| `BitmapInfoHeader/*` | `header['bitmapinfoheader']` | Width, height, bit depth, … |
| `CameraSetup/*` | `header['setup']` | All SETUP fields: ShutterNs, FrameRate, RealBPP, BlackLevel, WhiteLevel, fGain, fGamma, Serial, Description, WBGain, … |

`TIMEBLOCK` and `EXPOSUREBLOCK` (per-frame timestamps and per-frame exposure) are **not** written when generating from the binary, because pycine does not parse those tagged blocks. If the existing Phantom XML is present, it is used as-is and those blocks are preserved.

Metadata is written before the CINE is added to the removal script, so a dry run (`-n`) generates and shows the XML path without deleting anything.

### Removal script

For every pair that passes, the **larger** file (by byte size) is written to a shell/bat removal script — the same format used by `convert_cines.py --remove-cine`. The user reviews and runs the script manually.

```sh
#!/bin/sh
# Auto-generated: video files with a smaller matched copy that passed PSNR check
# Review this list, then run: bash remove_originals.sh

rm /data/run1/shot1.cine
rm /data/run1/shot2.cine
```

### Arguments

| Flag | Default | Description |
|---|---|---|
| `source_dir` | _(required)_ | Root directory to scan |
| `--ext LIST` | `.cine,.mp4,.avi,.mov` | Comma-separated extensions to consider |
| `--psnr-threshold T` | `20.0` | Minimum PSNR in dB to count a pair as matching |
| `--psnr-frames N` | `5` | Number of frames to sample per pair |
| `--check-dir DIR` | _(temp, cleaned up)_ | Save extracted grayscale PNGs for visual inspection |
| `--remove-script PATH` | `remove_originals` next to `source_dir` | Base path for `.sh` / `.bat` scripts (no extension) |
| `--no-metadata` | off | Skip XML metadata extraction/copy |
| `-n` / `--dry-run` | off | List matches without running PSNR or writing scripts |
| `-v` / `--verbose` | off | Print per-frame PSNR values |

### Output format

```
[1/3] run1/shot1: .cine (48 MB)  vs  .mp4 (3.1 MB)
  psnr: PASS  (avg 23.4 dB, min 21.8 dB, 5/5 frames)  → .cine queued for removal
[2/3] run1/shot2: .cine (51 MB)  vs  .mp4 (3.3 MB)
  psnr: PASS  (avg 24.1 dB, min 22.5 dB, 5/5 frames)  → .cine queued for removal
[3/3] run2/shot1: .avi (120 MB)  vs  .mp4 (4.0 MB)
  psnr: FAIL  (avg 11.2 dB, min 9.4 dB, 0/5 frames passed, threshold 20.0 dB)

Done: 2 passed, 1 failed.  Originals queued for removal: 2.

Removal scripts written:
  Mac/Linux: remove_originals.sh
  Windows:   remove_originals.bat
```

### Module structure

```python
DEFAULT_MATCH_PSNR_THRESHOLD = 20.0   # separate from check_conversion defaults

def find_video_files(source_dir: Path, exts: set[str]) -> list[Path]: ...
    # os.walk, filter by suffix.lower(), return sorted list

def group_by_stem(files: list[Path]) -> dict[tuple[Path, str], list[Path]]: ...
    # key = (parent, stem.lower()); value = list of matching paths

def check_pair(
    file_a: Path,
    file_b: Path,
    *,
    n_frames: int,
    threshold: float,
    check_dir: Path | None,
    verbose: bool,
) -> CheckResult: ...
    # calls _extract_gray_png + psnr; returns CheckResult with larger_file attr

def ensure_metadata(cine_path: Path, mp4_path: Path) -> Path | None: ...
    # Returns the XML path written (or None if --no-metadata).
    # If <cine_path>.with_suffix('.xml') exists: copy to mp4_path.with_suffix('.xml')
    #   if the two dirs differ; otherwise no-op.
    # Else: call extract_cine_metadata(cine_path) and write next to mp4_path.

def extract_cine_metadata(cine_path: Path) -> dict: ...
    # Calls pycine.file.read_header; returns a plain dict of all scalar fields
    # from cinefileheader, bitmapinfoheader, and setup (arrays serialised to lists).

def write_metadata_xml(meta: dict, out_path: Path) -> None: ...
    # Serialises the dict to Phantom-compatible XML (CineFileHeader +
    # BitmapInfoHeader + CameraSetup elements); uses xml.etree.ElementTree.

def write_removal_script(to_remove: list[Path], script_base: Path) -> None: ...
    # writes .sh and .bat; same format as convert_cines.py

def main(): ...
```

`check_pair` imports `_extract_gray_png`, `_get_duration`, `_timestamps`, and `_parse_psnr_y` from `check_conversion` directly; no new PSNR logic is needed. `extract_cine_metadata` and `write_metadata_xml` use only `pycine` and `xml.etree.ElementTree` (stdlib).
