from __future__ import annotations

import math
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_PSNR_FRAMES = 5
DEFAULT_PSNR_THRESHOLD = 30.0

_PSNR_Y_RE = re.compile(r"\[Parsed_psnr_0\s+@\s+\S+\]\s+PSNR\s+y:(\S+)")


@dataclass
class FrameResult:
    index: int
    timestamp: float
    psnr: float   # Y-channel (verify_psnr) or R-channel (check_file)
    passed: bool


@dataclass
class CheckResult:
    src: Path
    dst: Path
    frames: list[FrameResult]
    passed: bool
    threshold: float
    error: str | None = None


def _get_duration(path: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path.name}: {result.stderr.strip()}")
    return float(result.stdout.strip())


def _timestamps(duration: float, n_frames: int, offset: float = 0.0) -> list[float]:
    # skip the first few timestamps since they can be affected by keyframe seeking and may
    # not be representative of overall quality; sample evenly across the rest of the duration
    return [offset + (i + 0.5) * duration / (n_frames + 3) for i in range(n_frames + 3)][3:]


def _parse_psnr_y(stderr: str) -> float:
    m = _PSNR_Y_RE.search(stderr)
    if not m:
        raise RuntimeError(f"Could not parse PSNR from ffmpeg output:\n{stderr[-500:]}")
    val = m.group(1)
    return float("inf") if val.lower() == "inf" else float(val)


def verify_psnr(
    src: Path,
    dst: Path,
    vf: str | None,
    *,
    n_frames: int = DEFAULT_PSNR_FRAMES,
    threshold: float = DEFAULT_PSNR_THRESHOLD,
    start_sec: float = 0.0,
    verbose: bool = False,
) -> CheckResult:
    """Inline PSNR check. Extracts frames to a temp dir (reusing _extract_gray_png so that
    slow seek is used for both inputs) then compares with ffmpeg psnr.

    start_sec is the offset into src where dst's content begins (0.0 unless dst was
    trimmed from src), so source sampling stays aligned with the trimmed output."""
    if not src.exists():
        return CheckResult(src=src, dst=dst, frames=[], passed=False,
                           threshold=threshold, error=f"Source not found: {src}")
    if not dst.exists():
        return CheckResult(src=src, dst=dst, frames=[], passed=False,
                           threshold=threshold, error=f"Output not found: {dst}")

    try:
        _get_duration(src)  # existence/readability check
        dst_duration = _get_duration(dst)
    except RuntimeError as e:
        return CheckResult(src=src, dst=dst, frames=[], passed=False,
                           threshold=threshold, error=str(e))

    src_times = _timestamps(dst_duration, n_frames, offset=start_sec)
    dst_times = _timestamps(dst_duration, n_frames)
    frames = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for i, (ts, td) in enumerate(zip(src_times, dst_times)):
            src_png = tmp / f"src_{i:03d}.png"
            dst_png = tmp / f"dst_{i:03d}.png"

            try:
                _extract_gray_png(src, ts, vf, src_png)
                _extract_gray_png(dst, td, None, dst_png)
            except RuntimeError as e:
                return CheckResult(src=src, dst=dst, frames=frames, passed=False,
                                   threshold=threshold, error=str(e))

            cmd = [
                "ffmpeg", "-hide_banner",
                "-i", str(src_png), "-i", str(dst_png),
                "-lavfi", "[0:v][1:v]psnr",
                "-f", "null", "-",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return CheckResult(src=src, dst=dst, frames=frames, passed=False,
                                   threshold=threshold,
                                   error=f"ffmpeg failed at t={ts:.2f}s: {result.stderr[-300:]}")

            try:
                psnr = _parse_psnr_y(result.stderr)
            except RuntimeError as e:
                return CheckResult(src=src, dst=dst, frames=frames, passed=False,
                                   threshold=threshold, error=str(e))

            passed = math.isinf(psnr) or psnr >= threshold
            frames.append(FrameResult(index=i, timestamp=ts, psnr=psnr, passed=passed))

            if verbose:
                psnr_s = "inf" if math.isinf(psnr) else f"{psnr:.1f}"
                print(f"    frame {i+1}/{n_frames} t={ts:.2f}s  PSNR={psnr_s} dB  [{'PASS' if passed else 'FAIL'}]")

    all_passed = all(f.passed for f in frames)
    return CheckResult(src=src, dst=dst, frames=frames, passed=all_passed, threshold=threshold)


def _extract_gray_png(src: Path, timestamp: float, vf: str | None, out_path: Path) -> None:
    """Extract a single grayscale PNG from src at timestamp.
    Uses format=gray to normalize to 8-bit grayscale: for the CINE (gray16le) this pulls
    the single luma plane; for the MP4 (yuvj420p) it pulls the Y channel."""
    extract_vf = f"{vf},format=gray" if vf else "format=gray"
    cmd = [
        "ffmpeg", "-v", "warning",
        "-y",
        "-i", str(src),
        "-ss", f"{timestamp:.6f}",
        "-vf", extract_vf,
        "-vframes", "1",
        str(out_path),
    ]
    logger.debug(f"Extract frame: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Frame extraction failed at t={timestamp:.2f}s from {src.name}:\n{result.stderr[-300:]}"
        )


def check_file(
    src: Path,
    dst: Path,
    vf: str | None,
    *,
    n_frames: int = DEFAULT_PSNR_FRAMES,
    threshold: float = DEFAULT_PSNR_THRESHOLD,
    check_dir: Path | None = None,
    start_sec: float = 0.0,
    verbose: bool = False,
) -> CheckResult:
    """Thorough check: extract R-channel grayscale PNGs from both files, compare with PSNR.

    start_sec is the offset into src where dst's content begins (0.0 unless dst was
    trimmed from src), so source sampling stays aligned with the trimmed output."""
    if not src.exists():
        return CheckResult(src=src, dst=dst, frames=[], passed=False,
                           threshold=threshold, error=f"Source not found: {src}")
    if not dst.exists():
        return CheckResult(src=src, dst=dst, frames=[], passed=False,
                           threshold=threshold, error=f"Output not found: {dst}")

    try:
        _get_duration(src)  # existence/readability check
        dst_duration = _get_duration(dst)
    except RuntimeError as e:
        return CheckResult(src=src, dst=dst, frames=[], passed=False,
                           threshold=threshold, error=str(e))

    src_times = _timestamps(dst_duration, n_frames, offset=start_sec)
    dst_times = _timestamps(dst_duration, n_frames)
    frames = []

    if check_dir is not None:
        check_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        for i, (ts, td) in enumerate(zip(src_times, dst_times)):
            stem = src.stem
            if check_dir is not None:
                src_png = check_dir / f"{stem}_{i:03d}_src.png"
                dst_png = check_dir / f"{stem}_{i:03d}_dst.png"
            else:
                src_png = tmp / f"src_{i:03d}.png"
                dst_png = tmp / f"dst_{i:03d}.png"

            try:
                _extract_gray_png(src, ts, vf, src_png)
                _extract_gray_png(dst, td, None, dst_png)
            except RuntimeError as e:
                return CheckResult(src=src, dst=dst, frames=frames, passed=False,
                                   threshold=threshold, error=str(e))

            cmd = [
                "ffmpeg", "-hide_banner",
                "-i", str(src_png),
                "-i", str(dst_png),
                "-lavfi", "[0:v][1:v]psnr",
                "-f", "null", "-",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return CheckResult(src=src, dst=dst, frames=frames, passed=False,
                                   threshold=threshold,
                                   error=f"PSNR comparison failed at t={ts:.2f}s: {result.stderr[-300:]}")

            try:
                psnr = _parse_psnr_y(result.stderr)
            except RuntimeError as e:
                return CheckResult(src=src, dst=dst, frames=frames, passed=False,
                                   threshold=threshold, error=str(e))

            passed = math.isinf(psnr) or psnr >= threshold
            frames.append(FrameResult(index=i, timestamp=ts, psnr=psnr, passed=passed))

            if verbose:
                psnr_s = "inf" if math.isinf(psnr) else f"{psnr:.1f}"
                msg = f"    frame {i+1}/{n_frames} t={ts:.2f}s  PSNR={psnr_s} dB  [{'PASS' if passed else 'FAIL'}]"
                if check_dir is not None:
                    msg += f"\n      src: {src_png.name}  dst: {dst_png.name}"
                print(msg)

    all_passed = all(f.passed for f in frames)
    return CheckResult(src=src, dst=dst, frames=frames, passed=all_passed, threshold=threshold)
