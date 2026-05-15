#!/usr/bin/env python3
"""
find_matched_cine_mp4.py — find video files with matching names, verify content
via PSNR, preserve CINE metadata, and generate a removal script for the larger file.
"""
from __future__ import annotations

import argparse
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from itertools import combinations
from pathlib import Path

from check_conversion import CheckResult, FrameResult, _extract_gray_png, _get_duration, _timestamps, _parse_psnr_y

DEFAULT_MATCH_PSNR_THRESHOLD = 20.0
DEFAULT_PSNR_FRAMES = 5


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
# PSNR check
# ---------------------------------------------------------------------------

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
                png_a = check_dir / f"{stem}_{i:03d}_a.png"
                png_b = check_dir / f"{stem}_{i:03d}_b.png"
            else:
                png_a = tmp / f"a_{i:03d}.png"
                png_b = tmp / f"b_{i:03d}.png"

            try:
                _extract_gray_png(file_a, ta, None, png_a)
                _extract_gray_png(file_b, tb, None, png_b)
            except RuntimeError as e:
                return CheckResult(src=file_a, dst=file_b, frames=frames, passed=False,
                                   threshold=threshold, error=str(e))

            cmd = [
                "ffmpeg", "-hide_banner",
                "-i", str(png_a), "-i", str(png_b),
                "-lavfi", "[0:v][1:v]psnr",
                "-f", "null", "-",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                return CheckResult(src=file_a, dst=file_b, frames=frames, passed=False,
                                   threshold=threshold,
                                   error=f"PSNR failed at t={ta:.2f}s: {res.stderr[-300:]}")
            try:
                psnr = _parse_psnr_y(res.stderr)
            except RuntimeError as e:
                return CheckResult(src=file_a, dst=file_b, frames=frames, passed=False,
                                   threshold=threshold, error=str(e))

            passed = math.isinf(psnr) or psnr >= threshold
            frames.append(FrameResult(index=i, timestamp=ta, psnr=psnr, passed=passed))

            if verbose:
                psnr_s = "inf" if math.isinf(psnr) else f"{psnr:.1f}"
                print(f"    frame {i+1}/{n_frames} t={ta:.2f}s  PSNR={psnr_s} dB  [{'PASS' if passed else 'FAIL'}]")

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


def extract_cine_metadata(cine_path: Path) -> dict:
    from pycine.file import read_header
    return read_header(str(cine_path))


def write_metadata_xml(header: dict, out_path: Path) -> None:
    cfh = header['cinefileheader']
    bih = header['bitmapinfoheader']
    s = header['setup']

    root = ET.Element('chd')

    # CineFileHeader
    cfh_el = ET.SubElement(root, 'CineFileHeader')
    _sub(cfh_el, 'Type', 'CI')
    _sub(cfh_el, 'Compression', str(cfh.Compression))
    _sub(cfh_el, 'Version', str(cfh.Version))
    _sub(cfh_el, 'FirstMovieImage', str(cfh.FirstMovieImage))
    _sub(cfh_el, 'TotalImageCount', str(cfh.TotalImageCount))
    _sub(cfh_el, 'FirstImageNo', str(cfh.FirstImageNo))
    _sub(cfh_el, 'ImageCount', str(cfh.ImageCount))
    tt_el = ET.SubElement(cfh_el, 'TriggerTime')
    date_str, time_str = _trigger_time_strs(cfh.TriggerTime, s.RecordingTimeZone)
    _sub(tt_el, 'Date', date_str)
    _sub(tt_el, 'Time', time_str)

    # BitmapInfoHeader
    bih_el = ET.SubElement(root, 'BitmapInfoHeader')
    for field, _ in bih._fields_:
        _sub(bih_el, field, str(getattr(bih, field)))

    # CameraSetup
    cs = ET.SubElement(root, 'CameraSetup')

    def isub(tag: str, val) -> None:       _sub(cs, tag, str(int(val)))
    def fsub(tag: str, val) -> None:       _sub(cs, tag, f'{float(val):.6f}')
    def bsub(tag: str, val) -> None:       _sub(cs, tag, _bool_str(val))
    def ssub(tag: str, val: str) -> None:  _sub(cs, tag, val)

    isub('TrigFrame', s.TrigFrame)
    ssub('Mark', struct.pack('<H', s.Mark).decode('ascii', errors='replace'))
    isub('Length', s.Length)
    isub('SigOption', s.SigOption)
    isub('BinChannels', s.BinChannels)
    isub('SamplesPerImage', s.SamplesPerImage)
    ssub('BinName', '')
    isub('AnaOption', s.AnaOption)
    isub('AnaChannels', s.AnaChannels)
    isub('AnaBoard', s.AnaBoard)
    ssub('ChOption', '')
    ssub('AnaGain', '')
    ssub('AnaUnit', '')
    ssub('AnaName', '')
    isub('lFirstImage', s.lFirstImage)
    isub('dwImageCount', s.dwImageCount)
    isub('nQFactor', s.nQFactor)
    isub('wCineFileType', s.wCineFileType)
    szcp_el = ET.SubElement(cs, 'szCinePath')
    for i in range(4):
        e = ET.SubElement(szcp_el, 'szCinePath')
        e.set('no', str(i))
        e.text = ''
    isub('ImWidth', s.ImWidth)
    isub('ImHeight', s.ImHeight)
    isub('Serial', s.Serial)
    isub('Saturation', s.Saturation)
    isub('AutoExposure', s.AutoExposure)
    bsub('bFlipH', s.bFlipH)
    bsub('bFlipV', s.bFlipV)
    isub('Grid', s.Grid)
    isub('FrameRateDouble', s.FrameRate)
    isub('PostTrigger', s.PostTrigger)
    bsub('bEnableColor', s.bEnableColor)
    isub('CameraVersion', s.CameraVersion)
    isub('FirmwareVersion', s.FirmwareVersion)
    isub('SoftwareVersion', s.SoftwareVersion)
    isub('RecordingTimeZone', s.RecordingTimeZone)
    isub('CFA', s.CFA)
    isub('Bright', s.Bright)
    isub('Contrast', s.Contrast)
    isub('Gamma', s.Gamma)
    isub('AutoExpLevel', s.AutoExpLevel)
    isub('AutoExpSpeed', s.AutoExpSpeed)
    aer = ET.SubElement(cs, 'AutoExpRect')
    for field in ('left', 'right', 'top', 'bottom'):
        _sub(aer, field, str(getattr(s.AutoExpRect, field)))
    wbg_el = ET.SubElement(cs, 'WBGain')
    for i in range(4):
        e = ET.SubElement(wbg_el, 'WBGain')
        e.set('no', str(i))
        _sub(e, 'R', f'{s.WBGain[i].R:.6f}')
        _sub(e, 'B', f'{s.WBGain[i].B:.6f}')
    isub('Rotate', s.Rotate)
    wbv = ET.SubElement(cs, 'WBView')
    _sub(wbv, 'R', f'{s.WBView.R:.6f}')
    _sub(wbv, 'B', f'{s.WBView.B:.6f}')
    isub('RealBPP', s.RealBPP)
    isub('Conv8Min', s.Conv8Min)
    isub('Conv8Max', s.Conv8Max)
    isub('FilterCode', s.FilterCode)
    isub('FilterParam', s.FilterParam)
    uf_el = ET.SubElement(cs, 'UF')
    _sub(uf_el, 'dim', str(s.UF.dim))
    _sub(uf_el, 'shifts', str(s.UF.shifts))
    _sub(uf_el, 'bias', str(s.UF.bias))
    _sub(uf_el, 'Coef', '')
    isub('BlackCalSVer', s.BlackCalSVer)
    isub('WhiteCalSVer', s.WhiteCalSVer)
    isub('GrayCalSVer', s.GrayCalSVer)
    bsub('bStampTime', s.bStampTime)
    isub('SoundDest', s.SoundDest)
    isub('FRPSteps', s.FRPSteps)
    ssub('FRPImgNr', '')
    ssub('FRPRate', '')
    ssub('FRPExp', '')
    isub('MCCnt', s.MCCnt)
    mcp_el = ET.SubElement(cs, 'MCPercent')
    for i in range(64):
        e = ET.SubElement(mcp_el, 'MCPercent')
        e.set('no', str(i))
        e.text = f'{s.MCPercent[i]:.6f}'
    isub('CICalib', s.CICalib)
    isub('CalibWidth', s.CalibWidth)
    isub('CalibHeight', s.CalibHeight)
    isub('CalibRate', s.CalibRate)
    isub('CalibExp', s.CalibExp)
    isub('CalibEDR', s.CalibEDR)
    isub('CalibTemp', s.CalibTemp)
    hs_el = ET.SubElement(cs, 'HeadSerial')
    for i in range(4):
        e = ET.SubElement(hs_el, 'HeadSerial')
        e.set('no', str(i))
        e.text = str(s.HeadSerial[i])
    isub('RangeCode', s.RangeCode)
    isub('RangeSize', s.RangeSize)
    isub('Decimation', s.Decimation)
    isub('MasterSerial', s.MasterSerial)
    isub('Sensor', s.Sensor)
    isub('ShutterNs', s.ShutterNs)
    isub('EDRShutterNs', s.EDRShutterNs)
    isub('FrameDelayNs', s.FrameDelayNs)
    isub('ImPosXAcq', s.ImPosXAcq)
    isub('ImPosYAcq', s.ImPosYAcq)
    isub('ImWidthAcq', s.ImWidthAcq)
    isub('ImHeightAcq', s.ImHeightAcq)
    ssub('Description', _decode_cstr(bytes(s.Description)))
    bsub('RisingEdge', s.RisingEdge)
    isub('FilterTime', s.FilterTime)
    bsub('LongReady', s.LongReady)
    bsub('ShutterOff', s.ShutterOff)
    bsub('bMetaWB', s.bMetaWB)
    isub('Hue', s.Hue)
    isub('BlackLevel', s.BlackLevel)
    isub('WhiteLevel', s.WhiteLevel)
    ssub('LensDescription', _decode_cstr(bytes(s.LensDescription)))
    fsub('LensAperture', s.LensAperture)
    fsub('LensFocusDistance', s.LensFocusDistance)
    fsub('LensFocalLength', s.LensFocalLength)
    fsub('fOffset', s.fOffset)
    fsub('fGain', s.fGain)
    fsub('fSaturation', s.fSaturation)
    fsub('fHue', s.fHue)
    fsub('fGamma', s.fGamma)
    fsub('fGammaR', s.fGammaR)
    fsub('fGammaB', s.fGammaB)
    fsub('fFlare', s.fFlare)
    fsub('fPedestalR', s.fPedestalR)
    fsub('fPedestalG', s.fPedestalG)
    fsub('fPedestalB', s.fPedestalB)
    fsub('fChroma', s.fChroma)
    ssub('ToneLabel', _decode_cstr(bytes(s.ToneLabel)))
    isub('TonePoints', s.TonePoints)
    ssub('fTone', '')
    ssub('UserMatrixLabel', _decode_cstr(bytes(s.UserMatrixLabel)))
    bsub('EnableMatrices', s.EnableMatrices)
    um_el = ET.SubElement(cs, 'fUserMatrix')
    for i in range(9):
        e = ET.SubElement(um_el, 'fUserMatrix')
        e.set('no', str(i))
        e.text = f'{s.cmUser[i]:.6f}'
    bsub('EnableCrop', s.EnableCrop)
    cr_el = ET.SubElement(cs, 'CropRect')
    for field in ('left', 'right', 'top', 'bottom'):
        _sub(cr_el, field, str(getattr(s.CropRect, field)))
    bsub('EnableResample', s.EnableResample)
    isub('ResampleWidth', s.ResampleWidth)
    isub('ResampleHeight', s.ResampleHeight)
    fsub('fGain16_8', s.fGain16_8)
    frps_el = ET.SubElement(cs, 'FRPShape')
    for i in range(16):
        e = ET.SubElement(frps_el, 'FRPShape')
        e.set('no', str(i))
        e.text = str(s.FRPShape[i])

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
            f.write(f'rm {p}\n')
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


def _fmt_psnr_result(result: CheckResult, label: str = 'psnr') -> str:
    if result.error:
        return f'  {label}: ERROR  {result.error}'
    frames = result.frames
    if not frames:
        return f'  {label}: ERROR  no frames compared'
    n = len(frames)
    passed_n = sum(1 for f in frames if f.passed)
    finite = [f.psnr for f in frames if not math.isinf(f.psnr)]
    avg_s = 'inf' if not finite else f'{sum(finite) / len(finite):.1f}'
    min_p = min(f.psnr for f in frames)
    min_s = 'inf' if math.isinf(min_p) else f'{min_p:.1f}'
    if result.passed:
        return f'  {label}: PASS  (avg {avg_s} dB, min {min_s} dB, {passed_n}/{n} frames)'
    return f'  {label}: FAIL  (avg {avg_s} dB, min {min_s} dB, {passed_n}/{n} frames passed, threshold {result.threshold:.1f} dB)'


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Find matched video pairs, verify via PSNR, preserve CINE metadata, '
                    'generate a removal script for the larger file in each passing pair.'
    )
    parser.add_argument('source_dir', type=Path)
    parser.add_argument('--ext', default='.cine,.mp4,.avi,.mov',
                        help='Comma-separated extensions to consider (default: .cine,.mp4,.avi,.mov)')
    parser.add_argument('--psnr-threshold', type=float, default=DEFAULT_MATCH_PSNR_THRESHOLD,
                        metavar='T',
                        help=f'Minimum PSNR in dB to count a pair as matching (default: {DEFAULT_MATCH_PSNR_THRESHOLD})')
    parser.add_argument('--psnr-frames', type=int, default=DEFAULT_PSNR_FRAMES,
                        metavar='N',
                        help=f'Number of frames to sample per pair (default: {DEFAULT_PSNR_FRAMES})')
    parser.add_argument('--check-dir', type=Path, default=None,
                        help='Save extracted grayscale PNGs here for visual inspection')
    parser.add_argument('--remove-script', type=Path, default=None,
                        help='Base path for removal scripts (no extension; '
                             'default: remove_originals next to source_dir)')
    parser.add_argument('--no-metadata', action='store_true',
                        help='Skip XML metadata extraction/copy for CINE files')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='List matches without running PSNR or writing scripts')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Print per-frame PSNR values')
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

        result = check_pair(
            file_a, file_b,
            n_frames=args.psnr_frames,
            threshold=args.psnr_threshold,
            check_dir=args.check_dir,
            verbose=args.verbose,
        )
        print(_fmt_psnr_result(result))

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
        print(f'\nDry run: {len(pairs)} pair(s) found. Run without -n to check PSNR.')
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
