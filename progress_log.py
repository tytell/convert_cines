"""
Progress log for convert_cines.py.

Tracks per-file conversion status in a human-readable CSV so that long runs
can be resumed after interruption. The file is updated atomically (write to
.tmp then os.replace) after each status transition, so a crash never leaves
a partially-written file.

The CSV is preceded by comment lines (starting with #) that record all
conversion parameters so a run can be resumed with --continue without
re-specifying them.

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
import io
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

# Parameters stored in the comment header for --continue resume.
# 'rule' and 'config' are handled separately (rule is multi-valued).
PARAM_KEYS = [
    'source_dir', 'output_dir', 'ext', 'suffix',
    'crf', 'preset', 'fps',
    'max_intensity', 'contrast', 'gamma',
    'psnr_frames', 'psnr_threshold',
    'check_frames', 
    'config',
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
        self.params: dict = {}   # global params parsed from / written to the comment header

    def load(self) -> None:
        """Load existing progress from CSV (no-op if the file does not exist).

        Populates self.params from comment lines and self._records from CSV rows.
        """
        if not self.path.exists():
            return

        comment_lines = []
        data_lines = []
        for line in self.path.read_text(encoding='utf-8').splitlines(keepends=True):
            if line.startswith('#'):
                comment_lines.append(line)
            else:
                data_lines.append(line)

        # Parse params from comment header
        rules: list[str] = []
        for line in comment_lines:
            body = line[1:].strip()          # strip leading '#'
            key, _, value = body.partition(':')
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if key == 'rule':
                rules.append(value)
            elif key in PARAM_KEYS or key in ('started', 'last_run'):
                self.params[key] = value
        if rules:
            self.params['rules'] = rules

        # Parse CSV rows (skip blank lines that may follow comments)
        reader = csv.DictReader(io.StringIO(''.join(data_lines)))
        for row in reader:
            kwargs = {k: row.get(k, '') for k in FIELDNAMES}
            rec = FileRecord(**kwargs)
            self._records[rec.source] = rec

    def set_params(self, args) -> None:
        """Record all conversion parameters from args into self.params.

        Preserves the existing 'started' timestamp if already set.
        """
        started = self.params.get('started') or _now()
        self.params.update({
            'source_dir': str(args.source_dir),
            'output_dir': str(args.output_dir) if args.output_dir else '',
            'ext': args.ext,
            'suffix': args.suffix,
            'crf': str(args.crf),
            'preset': args.preset,
            'fps': str(args.fps) if args.fps is not None else '',
            'max_intensity': str(args.max_intensity),
            'contrast': str(args.contrast),
            'gamma': str(args.gamma),
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

        return {
            'source_dir': _path('source_dir'),
            'output_dir':  _path('output_dir'),
            'ext':         _str('ext', '.cine'),
            'suffix':      _str('suffix', ''),
            'crf':         _int('crf', 28),
            'preset':      _str('preset', 'slow'),
            'fps':         _float('fps', None) if p.get('fps') else None,
            'max_intensity': _float('max_intensity', 1.0),
            'contrast':    _float('contrast', 1.0),
            'gamma':       _float('gamma', 1.0),
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
        """Write the full log once after all files have been registered.

        Subsequent writes happen automatically via update().
        """
        self._write()

    def _write(self) -> None:
        """Write comment header + CSV to a .tmp file, then atomically replace the real file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / (self.path.name + '.tmp')
        with tmp.open('w', newline='', encoding='utf-8') as f:
            # Comment header with stored parameters
            f.write('# convert_cines progress log\n')
            for key in PARAM_KEYS:
                f.write(f'# {key}: {self.params.get(key, "")}\n')
            for rule in self.params.get('rules', []):
                f.write(f'# rule: {rule}\n')
            f.write(f'# started: {self.params.get("started", _now())}\n')
            f.write(f'# last_run: {_now()}\n')
            # CSV rows
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            for rec in self._records.values():
                writer.writerow({k: getattr(rec, k) for k in FIELDNAMES})
        os.replace(tmp, self.path)
