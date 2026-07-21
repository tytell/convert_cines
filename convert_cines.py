#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "pyyaml>=6.0",
# ]
# ///
import argparse
import csv
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import logging

logging.basicConfig(level=logging.WARNING,
                    format='%(name)s - %(levelname)s - %(message)s')

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

from check_conversion import (
    DEFAULT_PSNR_FRAMES,
    DEFAULT_PSNR_THRESHOLD,
    _get_duration,
    check_file,
    verify_psnr,
)
from progress_log import ProgressLog, TERMINAL_STATUSES


def print_psnr_result(result):
    if result.error:
        logger.error(f"  psnr: ERROR  {result.error}")
        return
    psnrs = [f.psnr for f in result.frames]
    avg = sum(psnrs) / len(psnrs)
    mn = min(psnrs)
    n = len(psnrs)
    avg_s = "inf" if math.isinf(avg) else f"{avg:.1f}"
    mn_s = "inf" if math.isinf(mn) else f"{mn:.1f}"
    if result.passed:
        logger.info(f"  psnr: PASS  (avg {avg_s} dB, min {mn_s} dB, {n}/{n} frames)")
    else:
        n_pass = sum(1 for f in result.frames if f.passed)
        logger.info(f"  psnr: FAIL  (avg {avg_s} dB, min {mn_s} dB, "
                    f"{n_pass}/{n} frames passed, threshold {result.threshold:.1f} dB)")


def print_check_result(result):
    if result.error:
        logger.error(f"  check: ERROR  {result.error}")
        return
    psnrs = [f.psnr for f in result.frames]
    avg = sum(psnrs) / len(psnrs)
    mn = min(psnrs)
    n = len(psnrs)
    avg_s = "inf" if math.isinf(avg) else f"{avg:.1f}"
    mn_s = "inf" if math.isinf(mn) else f"{mn:.1f}"
    if result.passed:
        logger.info(f"  check: PASS  (avg {avg_s} dB, min {mn_s} dB, {n}/{n} frames)")
    else:
        n_pass = sum(1 for f in result.frames if f.passed)
        logger.info(f"  check: FAIL  (avg {avg_s} dB, min {mn_s} dB, "
                    f"{n_pass}/{n} frames passed, threshold {result.threshold:.1f} dB)")

LUTRGB_CUBIC_FILTER = (
    "lutrgb="
    "r='clip(3*val*val/255 - 2*val*val*val/(255*255), 0, 255)':"
    "g='clip(3*val*val/255 - 2*val*val*val/(255*255), 0, 255)':"
    "b='clip(3*val*val/255 - 2*val*val*val/(255*255), 0, 255)'"
)
def build_vf(max_intensity, contrast, gamma, lutrgb_cubic=False):
    parts = []
    if max_intensity != 1.0:
        max_intensity = max(0.0, min(1.0, max_intensity))
        parts.append(f"curves=all='0/0 {max_intensity}/1 1/1'")
    eq_params = []
    if contrast != 1.0:
        eq_params.append(f"contrast={contrast}")
    if gamma != 1.0:
        eq_params.append(f"gamma={gamma}")
    if eq_params:
        parts.append("eq=" + ":".join(eq_params))
    if lutrgb_cubic:
        parts.append(LUTRGB_CUBIC_FILTER)
    return ",".join(parts) if parts else None


def tiff_output_dir(src: Path, source_root: Path, tiff_dir: Path) -> Path:
    rel = src.relative_to(source_root) if src.is_relative_to(source_root) else Path(src.name)
    return tiff_dir / rel.parent / src.stem


def output_path(src, source_root, output_dir, suffix=""):
    if output_dir is None:
        return src.parent / (src.stem + suffix + ".mp4")
    rel = src.relative_to(source_root)
    return output_dir / rel.parent / (src.stem + suffix + ".mp4")


def find_files(source_dir, ext):
    ext = ext.lower()
    results = []
    for root, _, files in os.walk(source_dir):
        for f in files:
            if Path(f).suffix.lower() == ext:
                results.append(Path(root) / f)
    return sorted(results)


# -----------------------------------------------------------------------------
# Rule loading: --rule flags, YAML --config, CSV --config
# -----------------------------------------------------------------------------

ENHANCEMENT_KEYS = {"max_intensity", "contrast", "gamma"}
TRIM_PLAIN_KEYS = {"start_frame", "start_sec", "duration_frames", "duration_sec"}
TRIM_END_KEYS = {"end_frame", "end_sec"}
TRIM_DURATION_KEYS = {"duration_frames", "duration_sec"}
KNOWN_OVERRIDE_KEYS = ENHANCEMENT_KEYS | TRIM_PLAIN_KEYS | TRIM_END_KEYS

# Matches bare "end" or "end - x" / "end-x" (x is a non-negative frame count or seconds).
_END_RE = re.compile(r'^\s*end\s*(-\s*(?P<x>\d+(?:\.\d+)?))?\s*$', re.IGNORECASE)


def _parse_plain_number(raw: str, field: str) -> float:
    """Parse a non-negative numeric override value (start/duration fields)."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field}={raw!r} is not a number")
    if val < 0:
        raise ValueError(f"{field}={raw!r} must not be negative")
    return val


def _parse_end_value(raw: str, field: str) -> tuple[str, float]:
    """Parse an end_frame/end_sec value: plain number, bare 'end', or 'end - x'.

    Returns ('abs', value) for a plain absolute number, or ('rel', x) for an
    end-relative expression (x frames/seconds before the last one; x=0 for bare 'end').
    """
    m = _END_RE.match(raw)
    if m:
        x = float(m.group("x")) if m.group("x") else 0.0
        return ("rel", x)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{field}={raw!r} is not a number or 'end' / 'end - x'")
    if val < 0:
        raise ValueError(f"{field}={raw!r} must not be negative")
    return ("abs", val)


def validate_overrides(overrides: dict, context: str) -> None:
    """Validate an overrides dict (raw string values) for known keys and unambiguous units.

    Raises ValueError with a message naming `context` (e.g. a rule pattern, CSV row,
    or "command line") on any problem. Only checks syntax; resolving an 'end - x'
    expression to an absolute value still requires probing the source file, deferred
    to resolve_trim().
    """
    for key in overrides:
        if key not in KNOWN_OVERRIDE_KEYS:
            raise ValueError(f"{context}: unrecognized parameter '{key}'")
    if "start_frame" in overrides and "start_sec" in overrides:
        raise ValueError(f"{context}: cannot set both start_frame and start_sec")
    if "end_frame" in overrides and "end_sec" in overrides:
        raise ValueError(f"{context}: cannot set both end_frame and end_sec")
    if (overrides.keys() & TRIM_END_KEYS) and (overrides.keys() & TRIM_DURATION_KEYS):
        raise ValueError(f"{context}: cannot set both an end and a duration")
    for key in ("start_frame", "duration_frames", "start_sec", "duration_sec"):
        if key in overrides:
            _parse_plain_number(overrides[key], key)
    for key in ("end_frame", "end_sec"):
        if key in overrides:
            _parse_end_value(overrides[key], key)


def _load_yaml_rules(path: Path) -> list[tuple[str, dict]]:
    import yaml
    data = yaml.safe_load(open(path))
    rules = []
    for entry in data.get("rules", []):
        entry = dict(entry)
        pattern = entry.pop("pattern")
        rules.append((pattern, {k: str(v) for k, v in entry.items()}))
    return rules


def _load_csv_rules(path: Path) -> list[tuple[str, dict]]:
    """Load rules from a CSV with a 'pattern' (or 'filename') column plus any
    combination of enhancement/trim columns. Missing columns simply aren't
    included in the per-row overrides dict, so they fall back to CLI defaults."""
    rules = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = dict(row)
            pattern = row.pop("pattern", None)
            filename = row.pop("filename", None)
            pattern = (pattern or filename or "").strip()
            if not pattern:
                raise ValueError(
                    f"{path}: CSV row missing a 'pattern'/'filename' value: {row}"
                )
            overrides = {
                k.strip(): v.strip()
                for k, v in row.items()
                if k is not None and v is not None and v.strip() != ""
            }
            rules.append((pattern, overrides))
    return rules


def load_rules(args, parser: argparse.ArgumentParser):
    """Return a list of (pattern, overrides_dict[str, str]) from --rule flags and
    --config (YAML or CSV, dispatched on file extension). Validates all rules plus
    the global CLI trim flags before returning; calls parser.error() on failure."""
    rules = []
    for r in (args.rule or []):
        pattern, _, params_str = r.partition(":")
        params = dict(kv.split("=", 1) for kv in params_str.split(",") if kv)
        rules.append((pattern.strip(), params))
    if args.config:
        try:
            if args.config.suffix.lower() == ".csv":
                rules.extend(_load_csv_rules(args.config))
            else:
                rules.extend(_load_yaml_rules(args.config))
        except (ValueError, OSError) as e:
            parser.error(str(e))

    cli_trim = {
        k: getattr(args, k)
        for k in ("start_frame", "start_sec", "end_frame", "end_sec",
                   "duration_frames", "duration_sec")
        if getattr(args, k) is not None
    }
    try:
        validate_overrides(cli_trim, "command line")
        for pattern, overrides in rules:
            validate_overrides(overrides, f"rule '{pattern}'")
    except ValueError as e:
        parser.error(str(e))

    return rules


def _is_exact_pattern(pattern: str) -> bool:
    """True if `pattern` has no glob wildcards, i.e. it's a literal relative path
    rather than a pattern matching a block of files."""
    return not any(ch in pattern for ch in "*?[")


def resolve_overrides(rel_path, rules) -> dict:
    """Merge overrides from every rule matching `rel_path`, per parameter.

    Exact-filename rules (no wildcards) take priority over pattern rules; within
    the same specificity tier, earlier-declared rules win. A parameter already
    filled by a higher-priority rule is not overwritten by a lower-priority one
    that also sets it — this is a strict superset of "first matching rule wins",
    which is what you get automatically when only one rule matches a file.
    """
    matches = [(p, ov) for p, ov in rules if fnmatch(str(rel_path), p)]
    # Stable sort: preserves declaration order within each specificity tier.
    matches.sort(key=lambda m: 0 if _is_exact_pattern(m[0]) else 1)
    merged: dict = {}
    for _, overrides in matches:
        for k, v in overrides.items():
            merged.setdefault(k, v)
    return merged


def resolve_enhancement(rel_path, rules, args):
    """Return (max_intensity, contrast, gamma) for a file, merging all matching rules."""
    overrides = resolve_overrides(rel_path, rules)
    return (
        float(overrides["max_intensity"]) if "max_intensity" in overrides else args.max_intensity,
        float(overrides["contrast"]) if "contrast" in overrides else args.contrast,
        float(overrides["gamma"]) if "gamma" in overrides else args.gamma,
    )


# -----------------------------------------------------------------------------
# Trimming
# -----------------------------------------------------------------------------


@dataclass
class TrimSpec:
    """Resolved trim window for one file.

    Frame-exact mode (start0/end0 set) uses an ffmpeg select+setpts filter for
    exact, keyframe-independent trimming. Time-based mode (start0 is None) uses
    -ss/-t placed after -i, which is also keyframe-independent (accurate seek).
    start_sec/duration_sec are always populated (even in frame-exact mode) for
    PSNR/check source-offset windowing and the progress-log canonical string.
    """
    start_sec: float = 0.0
    duration_sec: float | None = None
    start0: int | None = None
    end0: int | None = None

    @property
    def frame_exact(self) -> bool:
        return self.start0 is not None

    def filter_str(self) -> str | None:
        if not self.frame_exact:
            return None
        if self.end0 is None:
            return f"select='gte(n,{self.start0})',setpts=PTS-STARTPTS"
        return f"select='between(n,{self.start0},{self.end0})',setpts=PTS-STARTPTS"

    def canonical(self) -> str:
        """A string capturing the fully-resolved trim window, used for progress-log
        change detection. Empty string when there's no trim at all, so files that
        were never trimmed don't spuriously re-queue against old progress logs."""
        if self.frame_exact:
            end_s = str(self.end0) if self.end0 is not None else "end"
            return f"frame:{self.start0}:{end_s}"
        if self.start_sec == 0.0 and self.duration_sec is None:
            return ""
        dur_s = f"{self.duration_sec:.6f}" if self.duration_sec is not None else ""
        return f"time:{self.start_sec:.6f}:{dur_s}"


def _pick_override(overrides: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in overrides:
            return k, overrides[k]
    return None


def resolve_trim(rel_path, rules, args, src: Path) -> TrimSpec:
    """Return the resolved TrimSpec for a file, merging all matching rules
    (falling back to CLI globals for start/end/duration independently, exactly
    like resolve_enhancement). Raises ValueError if merging rules of different
    specificity produces an ambiguous combination (e.g. one rule sets start_frame,
    another sets start_sec) that no single rule had on its own — each rule is
    validated alone at load time, but a merge-only conflict can only be caught here."""
    matched_overrides = resolve_overrides(rel_path, rules)
    validate_overrides(matched_overrides, f"file '{rel_path}' (merged from matching rules)")

    def pick(keys):
        found = _pick_override(matched_overrides, keys)
        if found:
            return found
        for k in keys:
            v = getattr(args, k)
            if v is not None:
                return k, v
        return None

    start_pick = pick(("start_frame", "start_sec"))
    end_pick = pick(("end_frame", "end_sec"))
    dur_pick = pick(("duration_frames", "duration_sec"))

    if not start_pick and not end_pick and not dur_pick:
        return TrimSpec()

    use_frames = (
        (start_pick and start_pick[0] == "start_frame")
        or (end_pick and end_pick[0] == "end_frame")
        or (dur_pick and dur_pick[0] == "duration_frames")
    )

    start_unit = start_val = None
    if start_pick:
        key, raw = start_pick
        start_val = _parse_plain_number(raw, key)
        start_unit = "frame" if key == "start_frame" else "sec"

    end_unit = end_val = None
    if end_pick:
        key, raw = end_pick
        kind, val = _parse_end_value(raw, key)
        unit = "frame" if key == "end_frame" else "sec"
        if kind == "rel":
            if unit == "frame":
                val = _get_frame_count(src) - val
            else:
                val = _get_duration(src) - val
        end_val = val
        end_unit = unit

    dur_unit = dur_val = None
    if dur_pick:
        key, raw = dur_pick
        dur_val = _parse_plain_number(raw, key)
        dur_unit = "frame" if key == "duration_frames" else "sec"

    if use_frames:
        fps = _get_fps(src)
        start0 = 0
        if start_unit == "frame":
            start0 = int(round(start_val)) - 1
        elif start_unit == "sec":
            start0 = int(round(start_val * fps))
        start0 = max(0, start0)

        end0 = None
        if end_unit == "frame":
            end0 = int(round(end_val)) - 1
        elif end_unit == "sec":
            end0 = int(round(end_val * fps)) - 1
        elif dur_unit == "frame":
            end0 = start0 + int(round(dur_val)) - 1
        elif dur_unit == "sec":
            end0 = start0 + int(round(dur_val * fps)) - 1

        start_sec = start0 / fps
        duration_sec = (end0 - start0 + 1) / fps if end0 is not None else None
        return TrimSpec(start_sec=start_sec, duration_sec=duration_sec, start0=start0, end0=end0)

    start_sec = start_val if start_unit == "sec" else 0.0
    if end_unit == "sec":
        duration_sec = max(0.0, end_val - start_sec)
    elif dur_unit == "sec":
        duration_sec = dur_val
    else:
        duration_sec = None
    return TrimSpec(start_sec=start_sec, duration_sec=duration_sec)


def build_cmd(src, dst, args, vf, trim: TrimSpec, *, force_overwrite=False):
    """vf is the enhancement-only filter chain (from build_vf); trim is the resolved
    TrimSpec. Frame-exact trims are folded into the -vf chain via a select+setpts
    filter; time-based trims use -ss/-t placed after -i (output/accurate seeking,
    so neither mechanism is tied to keyframes)."""
    cmd = ["ffmpeg"]
    if args.overwrite or force_overwrite:
        cmd.append("-y")
    else:
        cmd.append("-n")

    if args.fps:
        cmd += ["-r", str(args.fps)]
    cmd += ["-i", str(src)]

    if not trim.frame_exact:
        if trim.start_sec:
            cmd += ["-ss", f"{trim.start_sec:.6f}"]
        if trim.duration_sec is not None:
            cmd += ["-t", f"{trim.duration_sec:.6f}"]

    full_vf = ",".join(f for f in (trim.filter_str(), vf) if f)
    if full_vf:
        cmd += ["-vf", full_vf]
    if args.test_frames:
        cmd += ["-vframes", str(int(args.test_frames))]
    cmd += [
        "-vcodec", "libx265",
        "-crf", str(args.crf),
        "-preset", args.preset,
        "-pix_fmt", "yuvj420p",
        "-tag:v", "hvc1"
    ]
    cmd.append(str(dst))

    return cmd


def _tiff_count_type(val: str):
    """argparse type for --tiff-count: accepts a positive integer or 'all'."""
    if val.lower() == 'all':
        return None
    try:
        n = int(val)
        if n < 1:
            raise argparse.ArgumentTypeError(
                f"--tiff-count must be a positive integer or 'all', got: {val!r}"
            )
        return n
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--tiff-count must be a positive integer or 'all', got: {val!r}"
        )


def _get_fps(src: Path) -> float:
    """Return the source's frame rate via ffprobe (r_frame_rate)."""
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=r_frame_rate',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(src)],
        capture_output=True, text=True,
    )
    fps_str = r.stdout.strip()
    if not fps_str:
        raise RuntimeError(f"Cannot determine frame rate for {src}")
    num, _, den = fps_str.partition('/')
    return int(num) / (int(den) if den else 1)


def _get_frame_count(src: Path) -> int:
    """Return total video frame count via ffprobe."""
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
         '-show_entries', 'stream=nb_frames',
         '-of', 'default=noprint_wrappers=1:nokey=1', str(src)],
        capture_output=True, text=True,
    )
    val = r.stdout.strip()
    if r.returncode == 0 and val and val != 'N/A':
        return int(val)
    # Fallback: duration × frame_rate
    try:
        fps = _get_fps(src)
        duration = _get_duration(src)
    except RuntimeError:
        raise RuntimeError(f"Cannot determine frame count for {src}")
    return round(duration * fps)


def extract_tiffs(src: Path, out_dir: Path, args) -> bool:
    """Extract raw 16-bit grayscale TIFFs from src into out_dir. Returns True on success."""
    if not args.overwrite and out_dir.exists() and any(out_dir.glob('*.tiff')):
        print("  tiffs: skipping (already extracted)")
        return True

    out_dir.mkdir(parents=True, exist_ok=True)
    out_pattern = out_dir / f"{src.stem}_%04d.tiff"

    pair_sep = args.tiff_pair_sep
    select_expr = None

    if args.tiff_every:
        M = args.tiff_every
        if pair_sep:
            select_expr = f"eq(mod(n,{M}),0)+eq(mod(n,{M}),{pair_sep})"
        else:
            select_expr = f"not(mod(n,{M}))"
    elif args.tiff_count is not None:
        K = args.tiff_count
        try:
            total = _get_frame_count(src)
        except (RuntimeError, ValueError) as e:
            logger.error(f"  tiffs: ERROR  {e}")
            return False
        anchors = [total // 2] if K == 1 else [
            round(i * (total - 1) / (K - 1)) for i in range(K)
        ]
        if pair_sep:
            terms = [t for a in anchors for t in (f"eq(n,{a})", f"eq(n,{a + pair_sep})")]
        else:
            terms = [f"eq(n,{a})" for a in anchors]
        select_expr = '+'.join(terms)

    if select_expr:
        vf = f"select='{select_expr}',format=gray16le"
        cmd = ['ffmpeg', '-y', '-i', str(src),
               '-vf', vf, '-vsync', '0', '-pix_fmt', 'gray16le', str(out_pattern)]
    else:
        cmd = ['ffmpeg', '-y', '-i', str(src),
               '-vf', 'format=gray16le', '-pix_fmt', 'gray16le', str(out_pattern)]

    if args.verbose:
        logger.info(f"  {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=not args.verbose)
    if result.returncode != 0:
        if not args.verbose:
            logger.error(result.stderr.decode(errors='replace'))
        logger.error(f"  tiffs: ERROR  ffmpeg exited with code {result.returncode}")
        return False

    n_tiffs = len(list(out_dir.glob('*.tiff')))
    if pair_sep:
        logger.info(f"  tiffs: {n_tiffs} frames ({n_tiffs // 2} pairs) → {out_dir}/")
    else:
        logger.info(f"  tiffs: {n_tiffs} frames → {out_dir}/")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Recursively convert video files to H.265 MP4 using ffmpeg."
    )
    general = parser.add_argument_group("General")
    general.add_argument("source_dir", type=Path, nargs="?", default=None,
                        help="Root directory to search (may be omitted with --continue)")
    general.add_argument("--ext", default=".cine", help="File extension to find (default: .cine)")
    general.add_argument("--output-dir", type=Path, default=None,
                        help="Output root directory (mirrors source structure). "
                             "If omitted, MP4s are written next to source files.")
    general.add_argument("--suffix", default="",
                        help="Suffix to add to output filenames before extension (default: '')")
    general.add_argument("--overwrite", action="store_true", default=False,
                        help="Overwrite existing output files")
    general.add_argument("-v", "--verbose", action="store_true",
                        help="Print detailed processing info")

    enhancement = parser.add_argument_group("Image enhancement")
    enhancement.add_argument("--max-intensity", type=float, default=1.0,
                        help="Default maximum output intensity via curves filter, 0.0–1.0 "
                             "(lower = brighter output; default: 1.0 = no adjustment)")
    enhancement.add_argument("--contrast", type=float, default=1.0,
                        help="Default contrast via eq filter (default: 1.0 = no adjustment)")
    enhancement.add_argument("--gamma", type=float, default=1.0,
                        help="Default gamma via eq filter (default: 1.0 = no adjustment)")
    enhancement.add_argument("--lutrgb-cubic", action="store_true",
                        help="Use cubic interpolation for LUTRGB filter (default: False)")

    encoding = parser.add_argument_group("Encoding options")
    encoding.add_argument("--crf", type=int, default=28, help="H.265 CRF value (default: 28)")
    encoding.add_argument("--preset", default="slow", help="x265 preset (default: slow)")
    encoding.add_argument("--fps", type=float, default=None, help="Output frame rate")

    test_mode = parser.add_argument_group("Test mode")
    test_mode.add_argument("--test-count", type=int, default=None,
                        help="Number of files to process in test mode")
    test_mode.add_argument("--test-files", nargs="+", type=Path, default=None,
                        help="Specific files to process in test mode (overrides --test-count)")
    test_mode.add_argument("--test-frames", type=float, default=None,
                        help="Number of frames to encode per file in test mode")

    dry_run = parser.add_argument_group("Dry run")
    dry_run.add_argument("-n", "--dry-run", action="store_true",
                        help="Print ffmpeg commands without running them")

    rules = parser.add_argument_group("File matching enhancement rules and trimming")
    rules.add_argument("--rule", action="append", metavar="PATTERN:param=value,...",
                        help="Per-file enhancement rule (repeatable). Pattern matches relative "
                             "path with wildcards. E.g.: --rule '*dark*:max_intensity=0.3,gamma=0.8'")
    rules.add_argument("--config", type=Path, default=None,
                        help="YAML or CSV config file (dispatched on extension) with per-file "
                             "enhancement/trim overrides. YAML: a 'rules:' list of {pattern, "
                             "max_intensity, contrast, gamma, start_frame, end_frame, "
                             "duration_frames, start_sec, end_sec, duration_sec}. CSV: one row "
                             "per file/pattern with a 'pattern' or 'filename' column plus any "
                             "of the same fields as columns; missing columns fall back to CLI "
                             "defaults. CLI --rule flags take priority over --config.")
    rules.add_argument("--start-frame", default=None, metavar="N",
                        help="Default trim start, as a 1-indexed frame number")
    rules.add_argument("--end-frame", default=None, metavar="N",
                        help="Default trim end (inclusive), as a 1-indexed frame number, "
                             "'end' (last frame), or 'end - N' (N frames before the last). "
                             "Mutually exclusive with --duration-frames/--duration-sec")
    rules.add_argument("--duration-frames", default=None, metavar="N",
                        help="Default trim duration in frames. "
                             "Mutually exclusive with --end-frame/--end-sec")
    rules.add_argument("--start-sec", default=None, metavar="T",
                        help="Default trim start, in seconds from the start of the file")
    rules.add_argument("--end-sec", default=None, metavar="T",
                        help="Default trim end, in seconds, 'end' (last timestamp), or "
                             "'end - T' (T seconds before the last). "
                             "Mutually exclusive with --duration-frames/--duration-sec")
    rules.add_argument("--duration-sec", default=None, metavar="T",
                        help="Default trim duration in seconds. "
                             "Mutually exclusive with --end-frame/--end-sec")

    progress = parser.add_argument_group("Progress tracking and resuming")
    progress.add_argument("--progress-file", type=Path, default=None, metavar="PATH",
                        help="Path for the progress CSV (default: conversion_progress.csv inside "
                             "source_dir). Created automatically; tracks status of every file so "
                             "a run can be resumed after interruption.")
    progress.add_argument("--no-progress", action="store_true",
                        help="Disable progress tracking entirely (no CSV file).")
    progress.add_argument("--continue", dest="continue_run", action="store_true",
                        help="Resume a previous run using all parameters stored in the progress "
                             "file. source_dir may be omitted; it is read from the progress file. "
                             "Run-mode flags (--check, --verbose, --remove-cine, etc.) still come "
                             "from the current command line.")
    progress.add_argument("--restart", action="store_true",
                        help="Clear the progress file and start fresh. Re-converts all files "
                             "regardless of prior status.")

    quality = parser.add_argument_group("Quality checks")
    psnr_group = quality.add_mutually_exclusive_group()
    psnr_group.add_argument("--test-psnr", action="store_true",
                        help="Run inline PSNR check after each conversion "
                             "(slow, and may not make sense for color videos)")
    psnr_group.add_argument("--no-test-psnr", dest="test_psnr", action="store_false")
    parser.set_defaults(test_psnr=None)  # None = auto, True = force, False = disable

    quality.add_argument("--psnr-frames", type=int, default=DEFAULT_PSNR_FRAMES, metavar="N",
                        help=f"Frames to sample for inline PSNR check (default: {DEFAULT_PSNR_FRAMES})")
    quality.add_argument("--psnr-threshold", type=float, default=DEFAULT_PSNR_THRESHOLD, metavar="T",
                        help=f"Minimum acceptable PSNR in dB for inline check (default: {DEFAULT_PSNR_THRESHOLD})")
    quality.add_argument("--check", action="store_true",
                        help="Run thorough R-channel PSNR check by extracting grayscale frames "
                             "from source and output. Runs after conversion and for skipped files.")
    quality.add_argument("--check-frames", type=int, default=DEFAULT_PSNR_FRAMES, metavar="N",
                        help=f"Frames to sample for --check (default: {DEFAULT_PSNR_FRAMES})")
    quality.add_argument("--check-dir", type=Path, default=None, metavar="DIR",
                        help="Save extracted grayscale PNGs under DIR, "
                             "mirroring the source directory structure")
    quality.add_argument("--keep-frames", action="store_true",
                        help="Save extracted check frames alongside the converted MP4 "
                             "(ignored if --check-dir is set)")

    removing = parser.add_argument_group("Removing verified CINE files")
    removing.add_argument("--remove-cine", action="store_true",
                        help="After --check, write a shell script listing all CINE files that "
                             "passed, so you can review and run it to delete them.")
    removing.add_argument("--remove-script", type=Path, default=None, metavar="PATH",
                        help="Path for the generated removal script (default: remove_cines.sh "
                             "next to the source directory). A .bat file is also written for Windows.")

    tiff = parser.add_argument_group("TIFF frame extraction")
    tiff.add_argument("--tiff-dir", type=Path, default=None, metavar="DIR",
                        help="Output root for extracted TIFFs (enables TIFF extraction). "
                             "Mirrors source directory structure. Always writes raw 16-bit "
                             "grayscale with no enhancement filters applied.")
    tiff.add_argument("--tiff-count", type=_tiff_count_type, default=None, metavar="N",
                        help="Frames to extract per file: integer for N evenly-distributed "
                             "frames, 'all' for every frame (default when --tiff-dir is set). "
                             "Mutually exclusive with --tiff-every.")
    tiff.add_argument("--tiff-every", type=int, default=None, metavar="M",
                        help="Extract one frame (or one pair) every M source frames. "
                             "Mutually exclusive with --tiff-count.")
    tiff.add_argument("--tiff-pair-sep", type=int, default=None, metavar="N",
                        help="Also extract a frame N source frames after each anchor, "
                             "producing consecutive pairs in the output. "
                             "Requires --tiff-every or --tiff-count N (not 'all').")

    args = parser.parse_args()

    # -------------------------------------------------------------------------
    # TIFF extraction argument validation
    # -------------------------------------------------------------------------
    if args.tiff_dir is not None:
        if args.tiff_count is not None and args.tiff_every is not None:
            parser.error("--tiff-count and --tiff-every are mutually exclusive")
        if args.tiff_pair_sep is not None:
            if args.tiff_count is None and args.tiff_every is None:
                parser.error(
                    "--tiff-pair-sep requires --tiff-every M or --tiff-count N (not 'all')"
                )
            if args.tiff_every is not None and args.tiff_pair_sep >= args.tiff_every:
                parser.error(
                    f"--tiff-pair-sep ({args.tiff_pair_sep}) must be less than "
                    f"--tiff-every ({args.tiff_every})"
                )
    else:
        for flag in ('tiff_count', 'tiff_every', 'tiff_pair_sep'):
            if getattr(args, flag) is not None:
                print(f"Warning: --{flag.replace('_', '-')} has no effect without --tiff-dir",
                      file=sys.stderr)

    # -------------------------------------------------------------------------
    # Progress file setup: resolve path, handle --continue / --restart
    # -------------------------------------------------------------------------
    progress_enabled = not args.no_progress and not args.dry_run
    log: ProgressLog | None = None

    if args.continue_run:
        # Determine the progress file path before we know source_dir
        if args.progress_file is not None:
            progress_path = args.progress_file
        elif args.source_dir is not None:
            progress_path = args.source_dir / "conversion_progress.csv"
        else:
            parser.error("--continue requires either source_dir or --progress-file PATH")

        if not progress_path.exists():
            parser.error(f"--continue: progress file not found: {progress_path}")

        if progress_enabled:
            log = ProgressLog(progress_path)
            log.load()
            if not log.params.get('source_dir'):
                parser.error(
                    f"--continue: progress file has no stored parameters (was it created "
                    f"with --no-progress?): {progress_path}"
                )

            stored = log.args_from_params()

            # If source_dir was given on the command line, verify it matches
            if args.source_dir is not None and stored['source_dir'] is not None:
                if args.source_dir.resolve() != stored['source_dir'].resolve():
                    parser.error(
                        f"--continue: source_dir on command line ({args.source_dir}) does not "
                        f"match progress file ({stored['source_dir']})"
                    )

            # Apply all stored conversion parameters to args
            args.__dict__.update(stored)
            started = log.params.get('started', 'unknown')
            print(f"Resuming from {progress_path}  (started {started})")
        else:
            # --continue with --no-progress: we still need source_dir
            if args.source_dir is None:
                parser.error("--continue with --no-progress requires source_dir")

    # source_dir must be known by now
    if args.source_dir is None:
        parser.error("source_dir is required (or use --continue to resume a previous run)")

    if not args.source_dir.is_dir():
        parser.error(f"source_dir is not a directory: {args.source_dir}")

    # Default progress file path
    if args.progress_file is None:
        args.progress_file = args.source_dir / "conversion_progress.csv"

    # --restart: delete the progress file so we start fresh
    if args.restart:
        if args.progress_file.exists():
            args.progress_file.unlink()
            print(f"Cleared progress file: {args.progress_file}")
        log = None   # will be re-created below as empty

    # Load (or create) the log for a normal / post-restart run
    if progress_enabled and log is None:
        log = ProgressLog(args.progress_file)
        log.load()

        # If a progress file exists with different source/output folders, stop
        # and explain — the user probably needs --restart or a different path.
        if log.params.get('source_dir') and not log.check_folder_match(
            args.source_dir, args.output_dir
        ):
            stored_src = log.params['source_dir']
            stored_out = log.params.get('output_dir', '') or '(same as source)'
            parser.error(
                f"Progress file {args.progress_file} was created for a different directory:\n"
                f"  stored source_dir : {stored_src}\n"
                f"  stored output_dir : {stored_out}\n"
                f"Use --restart to clear and start fresh, or --progress-file to specify a "
                f"different progress file."
            )

    # Record current parameters in the log (updates last_run; preserves started)
    if log is not None:
        log.set_params(args)

    # -------------------------------------------------------------------------
    # File discovery
    # -------------------------------------------------------------------------
    rules = load_rules(args, parser)

    test_mode = False
    if args.test_files:
        files = [p.resolve() for p in args.test_files]
        test_mode = True
    else:
        files = find_files(args.source_dir, args.ext)
        if args.test_count is not None:
            test_mode = True
            nf = len(files)
            step = max(1, nf // args.test_count)
            files = files[::step][: args.test_count]
        elif args.test_frames is not None:
            test_mode = True

    if not files:
        print("No files found.")
        return

    if test_mode:
        print(f"[TEST MODE] Processing {len(files)} file(s)")

    logger.debug(f"{args.psnr_threshold=}, {args.psnr_frames=}")

    # -------------------------------------------------------------------------
    # Resolve trim for every file upfront (not just when progress tracking is
    # active): catches merge conflicts between rules of different specificity
    # (e.g. a pattern rule sets start_frame, an exact-file rule sets start_sec
    # for the same file) and any 'end - x' ffprobe failure before any file is
    # touched, rather than partway through a long batch.
    # -------------------------------------------------------------------------
    trim_map: dict[Path, TrimSpec] = {}
    for src in files:
        rel = src.relative_to(args.source_dir) if src.is_relative_to(args.source_dir) else Path(src.name)
        try:
            trim_map[src] = resolve_trim(rel, rules, args, src)
        except ValueError as e:
            parser.error(str(e))

    # -------------------------------------------------------------------------
    # Pre-loop: register all files in the progress log
    # -------------------------------------------------------------------------
    succeeded, skipped, failed = 0, 0, 0
    psnr_failed, check_failed = 0, 0
    # Inline PSNR is suppressed when --check is active (unless --test-psnr forces it),
    # since the thorough check subsumes it.
    if not args.check and args.test_psnr:
        run_psnr = True
    elif test_mode and args.test_psnr is None:
        run_psnr = True
    else:
        run_psnr = False
    cines_to_remove: list[Path] = []
    seen_src_dirs: set[Path] = set()

    force_overwrite_map: dict[Path, bool] = {}
    if log is not None:
        for src in files:
            dst_pre = output_path(src, args.source_dir, args.output_dir, args.suffix)
            rel_pre = (src.relative_to(args.source_dir)
                       if src.is_relative_to(args.source_dir) else Path(src.name))
            mi, co, ga = resolve_enhancement(rel_pre, rules, args)
            vf_pre = build_vf(mi, co, ga)
            _, force_ow, reason = log.add_or_reconcile(
                src, dst_pre, args.crf, args.preset, args.fps, vf_pre, trim_map[src].canonical(),
                args.overwrite
            )
            if reason:
                print(f"  {src.name}: re-queued ({reason})")
            force_overwrite_map[src] = force_ow
        log.write_initial()

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------
    for i, src in enumerate(files, 1):
        is_first_in_dir = src.parent not in seen_src_dirs
        seen_src_dirs.add(src.parent)
        dst = output_path(src, args.source_dir, args.output_dir, args.suffix)
        rel = src.relative_to(args.source_dir) if src.is_relative_to(args.source_dir) else Path(src.name)
        max_intensity, contrast, gamma = resolve_enhancement(rel, rules, args)
        vf = build_vf(max_intensity, contrast, gamma)
        trim = trim_map[src]

        print(f"[{i}/{len(files)}] {src} → {dst}")
        if rules and args.verbose:
            print(f"  enhancement: max_intensity={max_intensity} contrast={contrast} gamma={gamma}")
        if trim.canonical() and args.verbose:
            print(f"  trim: {trim.canonical()}")

        if args.check_dir is not None:
            try:
                rel = src.parent.relative_to(args.source_dir)
            except ValueError:
                rel = Path('.')
            check_dir = args.check_dir / rel
        elif args.keep_frames:
            check_dir = dst.parent
        else:
            check_dir = None

        record = log.get(src) if log else None
        status = record.status if record else 'queued'
        force_ow = force_overwrite_map.get(src, False)

        # Skip files that are fully done
        if status in TERMINAL_STATUSES:
            logger.info(f"  skipping ({status})")
            if args.remove_cine and status == 'check_passed' and not is_first_in_dir:
                cines_to_remove.append(src)
            skipped += 1
            if args.tiff_dir is not None:
                extract_tiffs(src, tiff_output_dir(src, args.source_dir, args.tiff_dir), args)
            continue

        # --- Conversion ---
        conversion_ok = False
        if status in ('converted', 'psnr_passed', 'psnr_failed'):
            # Already converted in a prior run; skip ffmpeg
            skipped += 1
            conversion_ok = True
        elif dst.exists() and not args.overwrite and not force_ow:
            logger.info("  skipping (output exists)")
            skipped += 1
            if log:
                log.update(src, 'converted')
            conversion_ok = True
        else:
            logger.info(f"Converting {src} → {dst}")
            cmd = build_cmd(src, dst, args, vf, trim, force_overwrite=force_ow)
            if args.dry_run:
                print(" ", " ".join(cmd))
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            interrupted = False
            try:
                result = subprocess.run(cmd)
                returncode = result.returncode
            except KeyboardInterrupt:
                interrupted = True
                returncode = -1
            if returncode != 0:
                # Remove the partial output so a resume re-encodes from scratch
                if dst.exists():
                    dst.unlink()
                    logger.error(f"  removed partial output: {dst.name}")
                if interrupted:
                    if log:
                        log.update(src, 'conversion_failed', error="interrupted")
                    raise KeyboardInterrupt
                logger.error(f"  ERROR: ffmpeg exited with code {returncode}")
                failed += 1
                if log:
                    log.update(src, 'conversion_failed',
                               error=f"ffmpeg exit code {returncode}")
            else:
                succeeded += 1
                conversion_ok = True
                if log:
                    log.update(src, 'converted')

        if not conversion_ok:
            continue

        # --- Inline PSNR ---
        if run_psnr and status not in ('psnr_passed', 'psnr_failed'):
            logger.info(f"Verifying PSNR for {src}")
            r = verify_psnr(src, dst, vf, n_frames=args.psnr_frames,
                            threshold=args.psnr_threshold, start_sec=trim.start_sec,
                            verbose=args.verbose)
            print_psnr_result(r)
            if not r.passed:
                psnr_failed += 1
            if log and not r.error:
                psnrs = [f.psnr for f in r.frames]
                log.update(src, 'psnr_passed' if r.passed else 'psnr_failed',
                           psnr_avg=sum(psnrs) / len(psnrs), psnr_min=min(psnrs))

        # --- Thorough check ---
        if args.check and not args.test_frames:
            logger.info(f"Detailed check for {src}")
            r = check_file(src, dst, vf, n_frames=args.check_frames,
                           threshold=args.psnr_threshold,
                           check_dir=check_dir, start_sec=trim.start_sec,
                           verbose=args.verbose)
            print_check_result(r)
            if not r.passed:
                check_failed += 1
            elif args.remove_cine and not is_first_in_dir:
                cines_to_remove.append(src)
            if log:
                psnrs = [f.psnr for f in r.frames] if r.frames else []
                log.update(
                    src,
                    'check_passed' if r.passed else 'check_failed',
                    check_avg=sum(psnrs) / len(psnrs) if psnrs else None,
                    check_min=min(psnrs) if psnrs else None,
                    error=r.error or '',
                )

        # --- TIFF extraction ---
        if args.tiff_dir is not None:
            logger.info(f"Extracting TIFFs for {src}")
            extract_tiffs(src, tiff_output_dir(src, args.source_dir, args.tiff_dir), args)

    if not args.dry_run:
        summary = f"\nDone: {succeeded} succeeded, {skipped} skipped, {failed} failed."
        if run_psnr:
            summary += f"  PSNR: {psnr_failed} failed."
        if args.check:
            summary += f"  Check: {check_failed} failed."
        if args.remove_cine and cines_to_remove:
            summary += f"  CINEs queued for removal: {len(cines_to_remove)}."
        print(summary)

    if args.remove_cine and cines_to_remove:
        script_base = args.remove_script or (args.source_dir.parent / "remove_cines")
        sh_path = script_base.with_suffix(".sh")
        bat_path = script_base.with_suffix(".bat")

        with sh_path.open("w") as f:
            f.write("#!/bin/sh\n")
            f.write("# Auto-generated: CINE files that passed --check\n")
            f.write("# Review this list, then run: bash {}\n\n".format(sh_path.name))
            for p in cines_to_remove:
                f.write(f'rm "{p}"\n')
        sh_path.chmod(sh_path.stat().st_mode | 0o111)

        with bat_path.open("w") as f:
            f.write("@echo off\n")
            f.write("rem Auto-generated: CINE files that passed --check\n")
            f.write("rem Review this list, then run: {}\n\n".format(bat_path.name))
            for p in cines_to_remove:
                f.write(f"del \"{p}\"\n")

        print(f"\nRemoval scripts written:")
        print(f"  Mac/Linux: {sh_path}")
        print(f"  Windows:   {bat_path}")


if __name__ == "__main__":
    main()
