"""
Progress log for convert_cines.py.

Tracks per-file conversion status in a human-readable CSV so that long runs
can be resumed after interruption. The file is updated atomically (write to
.tmp then os.replace) after each status transition, so a crash never leaves
a partially-written file.

Status progression:
    queued
      → converted          (ffmpeg succeeded)
      → conversion_failed  (ffmpeg non-zero)
      → psnr_passed        (inline PSNR check passed)
      → psnr_failed        (inline PSNR check failed)
      → check_passed       (thorough --check passed)
      → check_failed       (thorough --check failed)
"""
from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FIELDNAMES = [
    'source', 'output', 'status',
    'crf', 'preset', 'fps', 'vf',
    'psnr_avg', 'psnr_min', 'check_avg', 'check_min',
    'updated', 'error',
]

# Statuses where all work is done — skip on resume
TERMINAL_STATUSES = frozenset({'check_passed', 'check_failed'})


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _fmt_psnr(v: float) -> str:
    return 'inf' if math.isinf(v) else f'{v:.2f}'


@dataclass
class FileRecord:
    source: str
    output: str
    status: str = 'queued'
    crf: str = ''
    preset: str = ''
    fps: str = ''
    vf: str = ''
    psnr_avg: str = ''
    psnr_min: str = ''
    check_avg: str = ''
    check_min: str = ''
    updated: str = ''
    error: str = ''

    def options_match(self, crf: int, preset: str, fps, vf: str | None) -> bool:
        return (
            self.crf == str(crf)
            and self.preset == preset
            and self.fps == (str(fps) if fps is not None else '')
            and self.vf == (vf or '')
        )


class ProgressLog:
    """Human-readable CSV progress tracker with atomic writes."""

    def __init__(self, path: Path):
        self.path = path
        self._records: dict[str, FileRecord] = {}

    def load(self) -> None:
        """Load existing progress from CSV (no-op if the file does not exist)."""
        if not self.path.exists():
            return
        with self.path.open(newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                kwargs = {k: row.get(k, '') for k in FIELDNAMES}
                rec = FileRecord(**kwargs)
                self._records[rec.source] = rec

    def add_or_reconcile(
        self,
        src: Path,
        dst: Path,
        crf: int,
        preset: str,
        fps,
        vf: str | None,
        overwrite: bool,
    ) -> tuple[str, bool, str | None]:
        """Register a file or reconcile with an existing record.

        Returns (effective_status, force_overwrite, requeue_reason).
        - force_overwrite is True when a previously-converted file must be
          re-converted because its options changed; the caller should pass
          -y to ffmpeg even if --overwrite was not set.
        - requeue_reason is a human-readable string when the file was
          re-queued, otherwise None.
        """
        key = str(src)
        crf_s = str(crf)
        fps_s = str(fps) if fps is not None else ''
        vf_s = vf or ''

        if key in self._records:
            rec = self._records[key]
            if not rec.options_match(crf, preset, fps, vf):
                parts = []
                if rec.crf != crf_s:
                    parts.append(f'crf {rec.crf}→{crf_s}')
                if rec.preset != preset:
                    parts.append(f'preset {rec.preset}→{preset}')
                if rec.fps != fps_s:
                    parts.append(f'fps {rec.fps or "auto"}→{fps_s or "auto"}')
                if rec.vf != vf_s:
                    parts.append('vf changed')
                reason = ', '.join(parts)
                rec.status = 'queued'
                rec.crf = crf_s
                rec.preset = preset
                rec.fps = fps_s
                rec.vf = vf_s
                rec.psnr_avg = rec.psnr_min = ''
                rec.check_avg = rec.check_min = ''
                rec.error = ''
                rec.updated = _now()
                return 'queued', True, reason
            return rec.status, False, None

        # New file — infer initial status from filesystem
        if dst.exists() and not overwrite:
            status = 'converted'
        else:
            status = 'queued'
        rec = FileRecord(
            source=key,
            output=str(dst),
            status=status,
            crf=crf_s,
            preset=preset,
            fps=fps_s,
            vf=vf_s,
            updated=_now(),
        )
        self._records[key] = rec
        return status, False, None

    def get(self, src: Path) -> FileRecord | None:
        return self._records.get(str(src))

    def update(
        self,
        src: Path,
        status: str,
        *,
        psnr_avg: float | None = None,
        psnr_min: float | None = None,
        check_avg: float | None = None,
        check_min: float | None = None,
        error: str = '',
    ) -> None:
        """Update a file's status and write the log atomically."""
        key = str(src)
        rec = self._records.get(key)
        if rec is None:
            return
        rec.status = status
        if psnr_avg is not None:
            rec.psnr_avg = _fmt_psnr(psnr_avg)
        if psnr_min is not None:
            rec.psnr_min = _fmt_psnr(psnr_min)
        if check_avg is not None:
            rec.check_avg = _fmt_psnr(check_avg)
        if check_min is not None:
            rec.check_min = _fmt_psnr(check_min)
        rec.error = error
        rec.updated = _now()
        self._write()

    def write_initial(self) -> None:
        """Write the full log once after all files have been registered via
        add_or_reconcile(). Subsequent writes happen automatically via update()."""
        self._write()

    def _write(self) -> None:
        """Write to a .tmp file then atomically replace the real file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / (self.path.name + '.tmp')
        with tmp.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for rec in self._records.values():
                writer.writerow({k: getattr(rec, k) for k in FIELDNAMES})
        os.replace(tmp, self.path)
