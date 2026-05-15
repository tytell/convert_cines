#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "numpy>=2.4.4",
#     "pycine>=0.3.2",
# ]
# ///
"""
find_matched_cine_mp4.py — find video files with matching names, verify content
via Spearman rank correlation, preserve CINE metadata, and generate a removal
script for the larger file.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make sibling modules (check_conversion) importable when run from any directory
sys.path.insert(0, str(Path(__file__).parent))

import argparse
import ctypes
import os
import shutil
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from itertools import combinations
from pycine.file import read_header

import numpy as np

from check_conversion import CheckResult, FrameResult, _extract_gray_png, _get_duration, _timestamps

DEFAULT_MATCH_THRESHOLD = 0.99
DEFAULT_FRAMES = 5


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_video_files(source_dir: Path, exts: set[str]) -> list[Path]:
    files = []
    for dirpath, _, filenames in os.walk(source_dir):
        for fn in filenames:
            p = Path(dirpath) / fn
            if p.suffix.lower() in exts:
                files.append(p)
    return sorted(files)


def group_by_stem(files: list[Path]) -> dict[tuple[Path, str], list[Path]]:
    groups: dict[tuple[Path, str], list[Path]] = {}
    for f in files:
        key = (f.parent, f.stem.lower())
        groups.setdefault(key, []).append(f)
    return groups


# ---------------------------------------------------------------------------
# Spearman rank correlation check
# ---------------------------------------------------------------------------

def _load_gray_pixels(path: Path) -> np.ndarray:
    """Read a grayscale PNG as a flat float32 array via ffmpeg pipe."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(path),
           "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to read pixels from {path.name}: {res.stderr.decode()[-200:]}")
    return np.frombuffer(res.stdout, dtype=np.uint8).astype(np.float32)


def _spearman_corr(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman rank correlation of two flat pixel arrays.

    Invariant to any monotonic per-pixel transform (gain, gamma, curves),
    but sensitive to spatial differences — a reshuffled image scores near 0.
    """
    n = min(len(a), len(b))
    rank_a = np.argsort(np.argsort(a[:n])).astype(np.float64)
    rank_b = np.argsort(np.argsort(b[:n])).astype(np.float64)
    return float(np.corrcoef(rank_a, rank_b)[0, 1])


def check_pair(
    file_a: Path,
    file_b: Path,
    *,
    n_frames: int,
    threshold: float,
    check_dir: Path | None,
    verbose: bool,
) -> CheckResult:
    for f in (file_a, file_b):
        if not f.exists():
            return CheckResult(src=file_a, dst=file_b, frames=[], passed=False,
                               threshold=threshold, error=f"File not found: {f}")

    try:
        dur_a = _get_duration(file_a)
        dur_b = _get_duration(file_b)
    except RuntimeError as e:
        return CheckResult(src=file_a, dst=file_b, frames=[], passed=False,
                           threshold=threshold, error=str(e))

    times_a = _timestamps(dur_a, n_frames)
    times_b = _timestamps(dur_b, n_frames)
    frames: list[FrameResult] = []

    if check_dir is not None:
        check_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for i, (ta, tb) in enumerate(zip(times_a, times_b)):
            stem = file_a.stem
            if check_dir is not None:
                png_a = check_dir / f"{stem}_{i:03d}{file_a.suffix}.png"
                png_b = check_dir / f"{stem}_{i:03d}{file_b.suffix}.png"
            else:
                png_a = tmp / f"a_{i:03d}.png"
                png_b = tmp / f"b_{i:03d}.png"

            try:
                _extract_gray_png(file_a, ta, None, png_a)
                _extract_gray_png(file_b, tb, None, png_b)
                pixels_a = _load_gray_pixels(png_a)
                pixels_b = _load_gray_pixels(png_b)
                corr = _spearman_corr(pixels_a, pixels_b)
            except RuntimeError as e:
                return CheckResult(src=file_a, dst=file_b, frames=frames, passed=False,
                                   threshold=threshold, error=str(e))

            passed = corr >= threshold
            frames.append(FrameResult(index=i, timestamp=ta, psnr=corr, passed=passed))

            if verbose:
                print(f"    frame {i+1}/{n_frames} t={ta:.2f}s  r={corr:.4f}  [{'PASS' if passed else 'FAIL'}]")

    all_passed = all(f.passed for f in frames)
    return CheckResult(src=file_a, dst=file_b, frames=frames, passed=all_passed, threshold=threshold)


# ---------------------------------------------------------------------------
# Metadata extraction and XML generation
# ---------------------------------------------------------------------------

def _decode_cstr(b: bytes) -> str:
    return b.rstrip(b'\x00').decode('latin-1', errors='replace')


def _bool_str(val: int) -> str:
    return 'true' if val else 'false'


def _trigger_time_strs(tt, tz_seconds: int = 0) -> tuple[str, str]:
    """Convert TIME64 struct to (date_str, time_str) matching Phantom XML format.

    Phantom stores UTC in tt.seconds and RecordingTimeZone as (UTC - local) in seconds,
    so local_seconds = tt.seconds - tz_seconds.
    """
    import datetime as _dt
    dt = _dt.datetime.utcfromtimestamp(tt.seconds - tz_seconds)
    frac = tt.fractions / 4294967296.0
    ms = int(frac * 1000)
    us = (frac * 1000 - ms) * 1000
    date_str = dt.strftime('%a %b %d %Y')
    time_str = f"{dt.strftime('%H:%M:%S')}.{ms:03d} {us:06.2f}"
    return date_str, time_str


def _sub(parent: ET.Element, tag: str, text: str) -> ET.Element:
    el = ET.SubElement(parent, tag)
    el.text = text
    return el


# bool32_t is typedef'd as c_int, so these cannot be inferred from the ctypes type alone
_BOOL_FIELDS = frozenset({
    'bFlipH', 'bFlipV', 'bEnableColor', 'bStampTime', 'RisingEdge',
    'LongReady', 'ShutterOff', 'bMetaWB', 'EnableMatrices', 'EnableCrop',
    'EnableResample',
})

# SETUP struct fields whose Phantom XML name differs from the ctypes field name
_SETUP_NAME_MAP = {
    'FrameRate': 'FrameRateDouble',
    'cmUser':    'fUserMatrix',
}

# uint16_t fields that store two ASCII chars rather than a numeric value
_CHAR_PAIR_FIELDS = frozenset({'Type', 'Mark'})


def _val_to_xml(
    parent: ET.Element,
    tag: str,
    val,
    *,
    bool_fields: frozenset = frozenset(),
) -> None:
    """Serialize a ctypes field value to an XML child element under parent.

    Dispatch is based on the Python type of val:
      bytes           → decode as null-terminated latin-1 string (c_char * N)
      ctypes.Structure → recurse into _struct_to_xml
      ctypes.Array    → indexed children; element type determines formatting
      float           → 6 decimal places
      int (in bool_fields) → true/false
      int (in _CHAR_PAIR_FIELDS) → decode uint16_t as 2 ASCII chars
      int             → str()
    """
    if tag in _CHAR_PAIR_FIELDS and isinstance(val, int):
        _sub(parent, tag, struct.pack('<H', val).decode('ascii', errors='replace'))
    elif isinstance(val, bytes):
        _sub(parent, tag, _decode_cstr(val))
    elif isinstance(val, ctypes.Structure):
        el = ET.SubElement(parent, tag)
        _struct_to_xml(el, val, bool_fields=bool_fields)
    elif isinstance(val, ctypes.Array):
        el = ET.SubElement(parent, tag)
        if not len(val):
            return
        first = val[0]
        if isinstance(first, bytes):
            # 2D char array (c_char * N * M): indexed children with decoded strings
            for i, item in enumerate(val):
                child = ET.SubElement(el, tag)
                child.set('no', str(i))
                child.text = _decode_cstr(item)
        elif isinstance(first, ctypes.Structure):
            for i, item in enumerate(val):
                child = ET.SubElement(el, tag)
                child.set('no', str(i))
                _struct_to_xml(child, item, bool_fields=bool_fields)
        elif isinstance(first, float):
            for i, item in enumerate(val):
                child = ET.SubElement(el, tag)
                child.set('no', str(i))
                child.text = f'{item:.6f}'
        else:
            for i, item in enumerate(val):
                child = ET.SubElement(el, tag)
                child.set('no', str(i))
                child.text = str(item)
    elif isinstance(val, float):
        _sub(parent, tag, f'{val:.6f}')
    elif tag in bool_fields:
        _sub(parent, tag, _bool_str(val))
    else:
        _sub(parent, tag, str(val))


def _struct_to_xml(
    parent: ET.Element,
    obj,
    *,
    bool_fields: frozenset = frozenset(),
    name_map: dict | None = None,
) -> None:
    """Serialize all fields of a ctypes Structure to XML children of parent."""
    for field in obj._fields_:
        fname = field[0]   # (name, type) or (name, type, bitsize) for bitfields
        xml_name = (name_map or {}).get(fname, fname)
        val = getattr(obj, fname)
        _val_to_xml(parent, xml_name, val, bool_fields=bool_fields)


def extract_cine_metadata(cine_path: Path) -> dict:
    return read_header(str(cine_path))


def write_metadata_xml(header: dict, out_path: Path) -> None:
    cfh = header['cinefileheader']
    bih = header['bitmapinfoheader']
    s = header['setup']

    root = ET.Element('chd')

    # CineFileHeader: auto except TriggerTime which needs Date/Time sub-elements
    cfh_el = ET.SubElement(root, 'CineFileHeader')
    for field in cfh._fields_:
        fname = field[0]
        if fname == 'TriggerTime':
            tt_el = ET.SubElement(cfh_el, 'TriggerTime')
            date_str, time_str = _trigger_time_strs(cfh.TriggerTime, s.RecordingTimeZone)
            _sub(tt_el, 'Date', date_str)
            _sub(tt_el, 'Time', time_str)
        else:
            _val_to_xml(cfh_el, fname, getattr(cfh, fname),
                        bool_fields=frozenset())

    # BitmapInfoHeader and CameraSetup: fully automatic
    bih_el = ET.SubElement(root, 'BitmapInfoHeader')
    _struct_to_xml(bih_el, bih)

    cs_el = ET.SubElement(root, 'CameraSetup')
    _struct_to_xml(cs_el, s, bool_fields=_BOOL_FIELDS, name_map=_SETUP_NAME_MAP)

    xml_body = ET.tostring(root, encoding='unicode')
    with out_path.open('w', encoding='utf-8') as f:
        f.write('<?xml version="1.0"?>\n')
        f.write('<!-- xml cine header file - Copyright VisionResearch Inc. -->')
        f.write(xml_body)


def ensure_metadata(cine_path: Path, survivor_path: Path, *, no_metadata: bool) -> Path | None:
    """Preserve CINE metadata as XML next to survivor_path. Returns the XML path or None."""
    if no_metadata:
        return None
    xml_sidecar = cine_path.with_suffix('.xml')
    out_xml = survivor_path.with_suffix('.xml')
    if xml_sidecar.exists():
        if xml_sidecar.resolve() == out_xml.resolve():
            return out_xml
        shutil.copy2(xml_sidecar, out_xml)
        return out_xml
    try:
        header = extract_cine_metadata(cine_path)
        write_metadata_xml(header, out_xml)
        return out_xml
    except Exception as e:
        print(f"  warning: could not extract metadata from {cine_path.name}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Removal script
# ---------------------------------------------------------------------------

def write_removal_script(to_remove: list[Path], script_base: Path) -> tuple[Path, Path]:
    sh_path = script_base.with_suffix('.sh')
    bat_path = script_base.with_suffix('.bat')

    with sh_path.open('w', newline='\n') as f:
        f.write('#!/bin/sh\n')
        f.write('# Auto-generated: video files with a smaller matched copy that passed PSNR check\n')
        f.write('# Review this list, then run: bash remove_originals.sh\n\n')
        for p in to_remove:
            f.write(f'rm "{p}"\n')
    sh_path.chmod(sh_path.stat().st_mode | 0o111)

    with bat_path.open('w', newline='\r\n') as f:
        f.write('@echo off\n')
        f.write('REM Auto-generated: video files with a smaller matched copy that passed PSNR check\n')
        f.write('REM Review this list, then run this script\n\n')
        for p in to_remove:
            f.write(f'del "{p}"\n')

    return sh_path, bat_path


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _fmt_size(n: int) -> str:
    if n < 1024:
        return f'{n} B'
    elif n < 1024 ** 2:
        return f'{n / 1024:.1f} KB'
    elif n < 1024 ** 3:
        return f'{n / 1024 ** 2:.1f} MB'
    else:
        return f'{n / 1024 ** 3:.1f} GB'


def _fmt_corr_result(result: CheckResult, label: str = 'check') -> str:
    if result.error:
        return f'  {label}: ERROR  {result.error}'
    frames = result.frames
    if not frames:
        return f'  {label}: ERROR  no frames compared'
    n = len(frames)
    passed_n = sum(1 for f in frames if f.passed)
    corrs = [f.psnr for f in frames]   # psnr field repurposed to store correlation
    avg = sum(corrs) / len(corrs)
    min_c = min(corrs)
    if result.passed:
        return f'  {label}: PASS  (avg r={avg:.4f}, min r={min_c:.4f}, {passed_n}/{n} frames)'
    return (f'  {label}: FAIL  (avg r={avg:.4f}, min r={min_c:.4f}, '
            f'{passed_n}/{n} frames passed, threshold {result.threshold:.3f})')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Find matched video pairs, verify content via Spearman rank correlation, '
                    'preserve CINE metadata, and generate a removal script for the larger file '
                    'in each passing pair.'
    )
    parser.add_argument('source_dir', type=Path)
    parser.add_argument('--ext', default='.cine,.mp4,.avi,.mov',
                        help='Comma-separated extensions to consider (default: .cine,.mp4,.avi,.mov)')
    parser.add_argument('--threshold', type=float, default=DEFAULT_MATCH_THRESHOLD,
                        metavar='T',
                        help=f'Minimum Spearman rank correlation to count a pair as matching (default: {DEFAULT_MATCH_THRESHOLD})')
    parser.add_argument('--frames', type=int, default=DEFAULT_FRAMES,
                        metavar='N',
                        help=f'Number of frames to sample per pair (default: {DEFAULT_FRAMES})')
    parser.add_argument('--check-dir', type=Path, default=None, metavar='DIR',
                        help='Save extracted grayscale PNGs under DIR, '
                             'mirroring the source directory structure')
    parser.add_argument('--keep-frames', action='store_true',
                        help='Save extracted check frames alongside the video files '
                             '(ignored if --check-dir is set)')
    parser.add_argument('--remove-script', type=Path, default=None,
                        help='Base path for removal scripts (no extension; '
                             'default: remove_originals next to source_dir)')
    parser.add_argument('--no-metadata', action='store_true',
                        help='Skip XML metadata extraction/copy for CINE files')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='List matches without running checks or writing scripts')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Print per-frame correlation values')
    args = parser.parse_args()

    source_dir = args.source_dir.resolve()
    if not source_dir.is_dir():
        print(f'Error: {source_dir} is not a directory', file=sys.stderr)
        sys.exit(1)

    exts = {
        e.strip() if e.strip().startswith('.') else '.' + e.strip()
        for e in args.ext.split(',')
    }

    script_base = args.remove_script or (source_dir.parent / 'remove_originals')

    files = find_video_files(source_dir, exts)
    groups = group_by_stem(files)
    pairs: list[tuple[Path, Path]] = []
    for group_files in groups.values():
        if len(group_files) >= 2:
            for a, b in combinations(sorted(group_files), 2):
                pairs.append((a, b))

    if not pairs:
        print('No matched pairs found.')
        return

    to_remove: list[Path] = []
    n_passed = n_failed = n_error = 0

    for idx, (file_a, file_b) in enumerate(pairs):
        size_a = file_a.stat().st_size
        size_b = file_b.stat().st_size
        try:
            rel_a = file_a.relative_to(source_dir)
        except ValueError:
            rel_a = file_a
        label = str(rel_a.parent / file_a.stem) if rel_a.parent != Path('.') else file_a.stem
        print(f'[{idx+1}/{len(pairs)}] {label}: '
              f'{file_a.suffix} ({_fmt_size(size_a)})  vs  {file_b.suffix} ({_fmt_size(size_b)})')

        if args.dry_run:
            continue

        if args.check_dir is not None:
            try:
                rel = file_a.parent.relative_to(source_dir)
            except ValueError:
                rel = Path('.')
            pair_check_dir = args.check_dir / rel
        elif args.keep_frames:
            pair_check_dir = file_a.parent
        else:
            pair_check_dir = None

        result = check_pair(
            file_a, file_b,
            n_frames=args.frames,
            threshold=args.threshold,
            check_dir=pair_check_dir,
            verbose=args.verbose,
        )
        print(_fmt_corr_result(result))

        if result.error:
            n_error += 1
            continue

        if result.passed:
            n_passed += 1
            larger = file_a if size_a >= size_b else file_b
            survivor = file_b if larger == file_a else file_a

            # Preserve CINE metadata only when the CINE is the file being removed
            cine_file = next(
                (f for f in (file_a, file_b) if f.suffix.lower() == '.cine'), None
            )
            if cine_file is not None and larger == cine_file:
                xml_path = ensure_metadata(cine_file, survivor, no_metadata=args.no_metadata)
                if xml_path is not None:
                    print(f'  metadata: {xml_path}')

            to_remove.append(larger)
            print(f'  → {larger.suffix} queued for removal')
        else:
            n_failed += 1

    if args.dry_run:
        print(f'\nDry run: {len(pairs)} pair(s) found. Run without -n to check.')
        return

    print(f'\nDone: {n_passed} passed, {n_failed} failed', end='')
    if n_error:
        print(f', {n_error} error(s)', end='')
    print(f'.  Originals queued for removal: {len(to_remove)}.')

    if to_remove:
        sh_path, bat_path = write_removal_script(to_remove, script_base)
        print(f'\nRemoval scripts written:')
        print(f'  Mac/Linux: {sh_path}')
        print(f'  Windows:   {bat_path}')


if __name__ == '__main__':
    main()
