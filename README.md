# convert_cines

Recursively converts video files (default: `.cine`) to H.265 MP4 using ffmpeg. Supports brightness, contrast, and gamma adjustment, with per-file rules based on filename patterns.

## Requirements

- Python 3.10+, managed by `uv`
- `ffmpeg` on PATH
- `pyyaml` (only needed when using `--config`)

## Usage

```
uv run convert_cines.py source_dir [options]
```

### Basic examples

```bash
# Convert all .cine files under current directory (output alongside source files)
uv run convert_cines.py .

# Convert to a separate output directory (mirrors source folder structure)
uv run convert_cines.py rawdata/videos --output-dir rawdata/videos/compressed

# Overwrite files that have already been converted
uv run convert_cines.py rawdata/videos --output-dir rawdata/videos/compressed --overwrite
```

### Enhancement

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

Test mode lets you check enhancement settings on a small subset of files before committing to a full batch run.

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

---

## File matching enhancement rules

When different files in a batch need different enhancement settings, rules can map filename patterns to specific parameter values. Rules are checked in order; the first matching rule wins. Files that match no rule use the global `--max-intensity`, `--contrast`, and `--gamma` values.

Patterns use shell-style wildcards (`*`, `?`) and are matched against the **path relative to `source_dir`**.

### From the command line

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

### From a YAML config file

```bash
uv run convert_cines.py . --config enhance.yaml
```

The config file contains a `rules:` list. Each entry requires a `pattern` key plus any combination of `max_intensity`, `contrast`, and `gamma`.

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

### Combining CLI rules and a config file

CLI rules are checked before config file rules. Use this to override specific entries from a shared config on a given run:

```bash
uv run convert_cines.py . --config shared.yaml --rule "*night*:max_intensity=0.15"
```

### Verbose output

Add `-v` to print which enhancement values are applied to each file (useful when rules are active):

```bash
uv run convert_cines.py . --config enhance.yaml -v -n
# [1/12] run1/night_001.cine → run1/night_001.mp4
#   enhancement: max_intensity=0.25 contrast=1.0 gamma=0.75
```

---

## Output path behaviour

| Scenario | Output location |
|---|---|
| No `--output-dir` | MP4 written next to source file, same directory |
| `--output-dir /out` | Source directory tree mirrored under `/out` |

Example with mirrored output:
```
source:  rawdata/run1/sub/shot1.cine
output:  rawdata/compressed/run1/sub/shot1.mp4
```
