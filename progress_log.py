"""
Progress logs for convert_cines.py and find_matched_cine_mp4.py.

Both logs write a human-readable CSV preceded by comment lines that record run
parameters. Files are updated atomically (write to .tmp then os.replace) after
each status transition, so a crash never leaves a partially-written file.

ProgressLog — used by convert_cines.py
    Status progression:
        queued
          → converted          (ffmpeg succeeded)
          → conversion_failed  (ffmpeg non-zero)
          → psnr_passed        (inline PSNR check passed)
          → psnr_failed        (inline PSNR check failed)
          → check_passed       (thorough --check passed)
          → check_failed       (thorough --check failed)

PairProgressLog — used by find_matched_cine_mp4.py
    Status progression:
        queued
          → passed             (Spearman correlation check passed)
          → failed             (Spearman correlation check failed)
          → error              (exception during check — retried on resume)
"""
from __future__ import annotations

import csv
import io
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# ProgressLog constants
# ---------------------------------------------------------------------------

FIELDNAMES = [
    'source', 'output', 'status',
    'crf', 'preset', 'fps', 'vf', 'trim', 'video_only',
    'psnr_avg', 'psnr_min', 'check_avg', 'check_min',
    'updated', 'error',
]

# Parameters stored in the comment header for --continue resume.
# 'rule' and 'config' are handled separately (rule is multi-valued).
PARAM_KEYS = [
    'source_dir', 'output_dir', 'ext', 'suffix',
    'crf', 'preset', 'fps', 'video_only',
    'max_intensity', 'contrast', 'gamma',
    'start_frame', 'start_sec', 'end_frame', 'end_sec',
    'duration_frames', 'duration_sec',
    'psnr_frames', 'psnr_threshold',
    'check_frames',
    'config',
]

# Statuses where all work is done — skip on resume
TERMINAL_STATUSES = frozenset({'check_passed', 'check_failed'})

# ---------------------------------------------------------------------------
# PairProgressLog constants
# ---------------------------------------------------------------------------

PAIR_FIELDNAMES = [
    'source', 'output', 'status',
    'corr_avg', 'corr_min',
    'updated', 'error',
]

PAIR_PARAM_KEYS = [
    'source_dir', 'ext', 'threshold', 'frames', 'no_metadata',
]

PAIR_TERMINAL_STATUSES = frozenset({'passed', 'failed'})

# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _fmt_float(v: float, decimals: int = 2) -> str:
    return 'inf' if math.isinf(v) else f'{v:.{decimals}f}'


def _fmt_psnr(v: float) -> str:
    return _fmt_float(v, 2)

# ---------------------------------------------------------------------------
# Record dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FileRecord:
    source: str
    output: str
    status: str = 'queued'
    crf: str = ''
    preset: str = ''
    fps: str = ''
    vf: str = ''
    trim: str = ''
    video_only: str = ''
    psnr_avg: str = ''
    psnr_min: str = ''
    check_avg: str = ''
    check_min: str = ''
    updated: str = ''
    error: str = ''

    def options_match(
        self, crf: int, preset: str, fps, vf: str | None, trim: str | None,
        video_only: bool,
    ) -> bool:
        return (
            self.crf == str(crf)
            and self.preset == preset
            and self.fps == (str(fps) if fps is not None else '')
            and self.vf == (vf or '')
            and self.trim == (trim or '')
            and self.video_only == ('true' if video_only else '')
        )


@dataclass
class PairRecord:
    source: str   # file_a
    output: str   # file_b
    status: str = 'queued'
    corr_avg: str = ''
    corr_min: str = ''
    updated: str = ''
    error: str = ''

# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class _BaseProgressLog:
    """Shared CSV infrastructure: atomic writes, comment-header params, CSV rows."""

    _title: str = 'progress log'
    _param_keys: list[str] = []
    _fieldnames: list[str] = []
    _record_class: type = FileRecord

    def __init__(self, path: Path):
        self.path = path
        self._records: dict[str, Any] = {}
        self.params: dict = {}

    def load(self) -> None:
        """Load existing progress from CSV (no-op if the file does not exist)."""
        if not self.path.exists():
            return

        comment_lines = []
        data_lines = []
        for line in self.path.read_text(encoding='utf-8').splitlines(keepends=True):
            if line.startswith('#'):
                comment_lines.append(line)
            else:
                data_lines.append(line)

        for line in comment_lines:
            body = line[1:].strip()
            key, _, value = body.partition(':')
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            self._load_comment_param(key, value)

        reader = csv.DictReader(io.StringIO(''.join(data_lines)))
        for row in reader:
            kwargs = {k: row.get(k, '') for k in self._fieldnames}
            rec = self._record_class(**kwargs)
            self._records[rec.source] = rec

    def _load_comment_param(self, key: str, value: str) -> None:
        """Store a key/value from a comment header line. Override for special cases."""
        if key in self._param_keys or key in ('started', 'last_run'):
            self.params[key] = value

    def write_initial(self) -> None:
        """Write the full log once after all records have been registered."""
        self._write()

    def _write(self) -> None:
        """Write comment header + CSV to a .tmp file, then atomically replace."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / (self.path.name + '.tmp')
        with tmp.open('w', newline='', encoding='utf-8') as f:
            f.write(f'# {self._title}\n')
            for key in self._param_keys:
                f.write(f'# {key}: {self.params.get(key, "")}\n')
            self._write_extra_params(f)
            f.write(f'# started: {self.params.get("started", _now())}\n')
            f.write(f'# last_run: {_now()}\n')
            writer = csv.DictWriter(f, fieldnames=self._fieldnames)
            writer.writeheader()
            for rec in self._records.values():
                writer.writerow({k: getattr(rec, k) for k in self._fieldnames})
        os.replace(tmp, self.path)

    def _write_extra_params(self, _f) -> None:
        """Hook for subclasses to write additional comment-header lines."""
        pass

# ---------------------------------------------------------------------------
# ProgressLog
# ---------------------------------------------------------------------------


class ProgressLog(_BaseProgressLog):
    """Human-readable CSV progress tracker for convert_cines.py."""

    _title = 'convert_cines progress log'
    _param_keys = PARAM_KEYS
    _fieldnames = FIELDNAMES
    _record_class = FileRecord

    def _load_comment_param(self, key: str, value: str) -> None:
        if key == 'rule':
            self.params.setdefault('rules', []).append(value)
        else:
            super()._load_comment_param(key, value)

    def _write_extra_params(self, f) -> None:
        for rule in self.params.get('rules', []):
            f.write(f'# rule: {rule}\n')

    def set_params(self, args) -> None:
        """Record all conversion parameters from args into self.params.

        Preserves the existing 'started' timestamp if already set.
        """
        started = self.params.get('started') or _now()
        self.params.update({
            'source_dir': str(os.path.abspath(args.source_dir)),
            'output_dir': str(os.path.abspath(args.output_dir)) if args.output_dir else '',
            'ext': args.ext,
            'suffix': args.suffix,
            'crf': str(args.crf),
            'preset': args.preset,
            'fps': str(args.fps) if args.fps is not None else '',
            'video_only': 'true' if args.video_only else '',
            'max_intensity': str(args.max_intensity),
            'contrast': str(args.contrast),
            'gamma': str(args.gamma),
            'start_frame': args.start_frame or '',
            'start_sec': args.start_sec or '',
            'end_frame': args.end_frame or '',
            'end_sec': args.end_sec or '',
            'duration_frames': args.duration_frames or '',
            'duration_sec': args.duration_sec or '',
            'psnr_frames': str(args.psnr_frames),
            'psnr_threshold': str(args.psnr_threshold),
            'check_frames': str(args.check_frames),
            'rules': list(args.rule or []),
            'config': str(args.config) if args.config else '',
            'started': started,
        })

    def args_from_params(self) -> dict:
        """Return a dict of argument values reconstructed from stored params.

        Used by --continue to restore a previous invocation's parameters.
        Only covers stored (conversion) parameters; run-mode flags are not stored.
        """
        p = self.params

        def _str(k: str, default: str = '') -> str:
            return p.get(k, default)

        def _float(k: str, default: float) -> float:
            v = p.get(k, '')
            return float(v) if v else default

        def _int(k: str, default: int) -> int:
            v = p.get(k, '')
            return int(v) if v else default

        def _path(k: str):
            v = p.get(k, '')
            return Path(v) if v else None

        def _opt_str(k: str):
            v = p.get(k, '')
            return v if v else None

        def _bool(k: str, default: bool = False) -> bool:
            v = p.get(k, '')
            return v.lower() in ('1', 'true', 'yes', 'on') if v else default

        return {
            'source_dir': _path('source_dir'),
            'output_dir':  _path('output_dir'),
            'ext':         _str('ext', '.cine'),
            'suffix':      _str('suffix', ''),
            'crf':         _int('crf', 28),
            'preset':      _str('preset', 'slow'),
            'fps':         _float('fps', None) if p.get('fps') else None,
            'video_only':  _bool('video_only'),
            'max_intensity': _float('max_intensity', 1.0),
            'contrast':    _float('contrast', 1.0),
            'gamma':       _float('gamma', 1.0),
            'start_frame': _opt_str('start_frame'),
            'start_sec':   _opt_str('start_sec'),
            'end_frame':   _opt_str('end_frame'),
            'end_sec':     _opt_str('end_sec'),
            'duration_frames': _opt_str('duration_frames'),
            'duration_sec':    _opt_str('duration_sec'),
            'psnr_frames': _int('psnr_frames', 5),
            'psnr_threshold': _float('psnr_threshold', 30.0),
            'check_frames': _int('check_frames', 5),
            'rule':        p.get('rules') or None,
            'config':      _path('config'),
        }

    def check_folder_match(self, source_dir: Path, output_dir) -> bool:
        """Return True if stored source_dir and output_dir match the given values."""
        stored_src = self.params.get('source_dir', '')
        stored_out = self.params.get('output_dir', '')
        current_out = str(output_dir) if output_dir is not None else ''
        return str(source_dir) == stored_src and current_out == stored_out

    def add_or_reconcile(
        self,
        src: Path,
        dst: Path,
        crf: int,
        preset: str,
        fps,
        vf: str | None,
        trim: str | None,
        video_only: bool,
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
        trim_s = trim or ''
        video_only_s = 'true' if video_only else ''

        if key in self._records:
            rec = self._records[key]
            if not rec.options_match(crf, preset, fps, vf, trim, video_only):
                parts = []
                if rec.crf != crf_s:
                    parts.append(f'crf {rec.crf}→{crf_s}')
                if rec.preset != preset:
                    parts.append(f'preset {rec.preset}→{preset}')
                if rec.fps != fps_s:
                    parts.append(f'fps {rec.fps or "auto"}→{fps_s or "auto"}')
                if rec.vf != vf_s:
                    parts.append('vf changed')
                if rec.trim != trim_s:
                    parts.append('trim changed')
                if rec.video_only != video_only_s:
                    parts.append('video-only changed')
                reason = ', '.join(parts)
                rec.status = 'queued'
                rec.crf = crf_s
                rec.preset = preset
                rec.fps = fps_s
                rec.vf = vf_s
                rec.trim = trim_s
                rec.video_only = video_only_s
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
            trim=trim_s,
            video_only=video_only_s,
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

# ---------------------------------------------------------------------------
# PairProgressLog
# ---------------------------------------------------------------------------


class PairProgressLog(_BaseProgressLog):
    """Progress log for find_matched_cine_mp4.py — tracks pairs of video files."""

    _title = 'find_matched_cine_mp4 progress log'
    _param_keys = PAIR_PARAM_KEYS
    _fieldnames = PAIR_FIELDNAMES
    _record_class = PairRecord

    def set_params(self, args) -> None:
        """Record run parameters from args into self.params."""
        started = self.params.get('started') or _now()
        self.params.update({
            'source_dir': str(args.source_dir),
            'ext': args.ext,
            'threshold': str(args.threshold),
            'frames': str(args.frames),
            'no_metadata': str(args.no_metadata),
            'started': started,
        })

    def check_source_match(self, source_dir: Path) -> bool:
        """Return True if stored source_dir matches the given value."""
        return str(source_dir.absolute()) == os.path.abspath(self.params.get('source_dir', ''))

    def add_pair(self, file_a: Path, file_b: Path) -> str:
        """Register a pair if not already present. Returns current status."""
        key = str(file_a)
        if key in self._records:
            return self._records[key].status
        rec = PairRecord(
            source=str(file_a),
            output=str(file_b),
            status='queued',
            updated=_now(),
        )
        self._records[key] = rec
        return 'queued'

    def get(self, file_a: Path) -> PairRecord | None:
        return self._records.get(str(file_a))

    def update(
        self,
        file_a: Path,
        status: str,
        *,
        corr_avg: float | None = None,
        corr_min: float | None = None,
        error: str = '',
    ) -> None:
        """Update a pair's status and write the log atomically."""
        key = str(file_a)
        rec = self._records.get(key)
        if rec is None:
            return
        rec.status = status
        if corr_avg is not None:
            rec.corr_avg = _fmt_float(corr_avg, 4)
        if corr_min is not None:
            rec.corr_min = _fmt_float(corr_min, 4)
        rec.error = error
        rec.updated = _now()
        self._write()

    def passed_pairs(self) -> list[tuple[Path, Path]]:
        """Return all pairs with status 'passed' as (file_a, file_b) tuples."""
        return [
            (Path(rec.source), Path(rec.output))
            for rec in self._records.values()
            if rec.status == 'passed'
        ]
