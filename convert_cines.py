#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path


def build_vf(max_intensity, contrast, gamma):
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
    return ",".join(parts) if parts else None


def output_path(src, source_root, output_dir):
    if output_dir is None:
        return src.with_suffix(".mp4")
    rel = src.relative_to(source_root)
    return output_dir / rel.parent / (src.stem + ".mp4")


def find_files(source_dir, ext):
    ext = ext.lower()
    results = []
    for root, _, files in os.walk(source_dir):
        for f in files:
            if Path(f).suffix.lower() == ext:
                results.append(Path(root) / f)
    return sorted(results)


def load_rules(args):
    """Return a list of (pattern, overrides_dict) from --rule flags and --config file."""
    rules = []
    for r in (args.rule or []):
        pattern, _, params_str = r.partition(":")
        params = dict(kv.split("=") for kv in params_str.split(",") if kv)
        rules.append((pattern.strip(), {k: float(v) for k, v in params.items()}))
    if args.config:
        import yaml
        data = yaml.safe_load(open(args.config))
        for entry in data.get("rules", []):
            entry = dict(entry)
            pattern = entry.pop("pattern")
            rules.append((pattern, {k: float(v) for k, v in entry.items()}))
    return rules


def resolve_enhancement(rel_path, rules, args):
    """Return (max_intensity, contrast, gamma) for a file, applying first matching rule."""
    for pattern, overrides in rules:
        if fnmatch(str(rel_path), pattern):
            return (
                overrides.get("max_intensity", args.max_intensity),
                overrides.get("contrast", args.contrast),
                overrides.get("gamma", args.gamma),
            )
    return args.max_intensity, args.contrast, args.gamma


def build_cmd(src, dst, args, max_intensity, contrast, gamma):
    cmd = ["ffmpeg"]
    if args.overwrite:
        cmd.append("-y")
    else:
        cmd.append("-n")

    if args.fps:
        cmd += ["-r", str(args.fps)]
    cmd += ["-i", str(src)]

    vf = build_vf(max_intensity, contrast, gamma)
    if vf:
        cmd += ["-vf", vf]
    if args.test_frames:
        cmd += ["-vframes", str(int(args.test_frames))]
    cmd += [
        "-vcodec", "libx265",
        "-crf", str(args.crf),
        "-preset", args.preset,
        "-pix_fmt", "yuvj420p",
        "-tag:v", "hvc1",
        str(dst),
    ]
    return cmd


def main():
    parser = argparse.ArgumentParser(
        description="Recursively convert video files to H.265 MP4 using ffmpeg."
    )
    parser.add_argument("source_dir", type=Path, help="Root directory to search")
    parser.add_argument("--ext", default=".cine", help="File extension to find (default: .cine)")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Output root directory (mirrors source structure). "
                             "If omitted, MP4s are written next to source files.")
    parser.add_argument("--overwrite", action="store_true", default=False,
                        help="Overwrite existing output files")
    parser.add_argument("--crf", type=int, default=28, help="H.265 CRF value (default: 28)")
    parser.add_argument("--preset", default="slow", help="x265 preset (default: slow)")
    parser.add_argument("--fps", type=float, default=None, help="Output frame rate")
    parser.add_argument("--max-intensity", type=float, default=1.0,
                        help="Default maximum output intensity via curves filter, 0.0–1.0 "
                             "(lower = brighter output; default: 1.0 = no adjustment)")
    parser.add_argument("--contrast", type=float, default=1.0,
                        help="Default contrast via eq filter (default: 1.0 = no adjustment)")
    parser.add_argument("--gamma", type=float, default=1.0,
                        help="Default gamma via eq filter (default: 1.0 = no adjustment)")
    parser.add_argument("--rule", action="append", metavar="PATTERN:param=value,...",
                        help="Per-file enhancement rule (repeatable). Pattern matches relative "
                             "path with wildcards. E.g.: --rule '*dark*:max_intensity=0.3,gamma=0.8'")
    parser.add_argument("--config", type=Path, default=None,
                        help="YAML config file with a 'rules:' list of {pattern, max_intensity, "
                             "contrast, gamma} entries. CLI --rule flags take priority.")
    parser.add_argument("--test-count", type=int, default=None,
                        help="Number of files to process in test mode")
    parser.add_argument("--test-files", nargs="+", type=Path, default=None,
                        help="Specific files to process in test mode (overrides --test-count)")
    parser.add_argument("--test-frames", type=float, default=None,
                        help="Number of frames to encode per file in test mode")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Print ffmpeg commands without running them")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Print detailed processing info")

    args = parser.parse_args()

    if not args.source_dir.is_dir():
        parser.error(f"source_dir is not a directory: {args.source_dir}")

    rules = load_rules(args)

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
            files = files[:1]

    if not files:
        print("No files found.")
        return

    if test_mode:
        print(f"[TEST MODE] Processing {len(files)} file(s)")

    succeeded, skipped, failed = 0, 0, 0

    for i, src in enumerate(files, 1):
        dst = output_path(src, args.source_dir, args.output_dir)
        rel = src.relative_to(args.source_dir) if src.is_relative_to(args.source_dir) else Path(src.name)
        max_intensity, contrast, gamma = resolve_enhancement(rel, rules, args)

        print(f"[{i}/{len(files)}] {src} → {dst}")
        if rules and args.verbose:
            print(f"  enhancement: max_intensity={max_intensity} contrast={contrast} gamma={gamma}")

        if dst.exists() and not args.overwrite:
            print("  skipping (output exists)")
            skipped += 1
            continue

        cmd = build_cmd(src, dst, args, max_intensity, contrast, gamma)

        if args.dry_run:
            print(" ", " ".join(cmd))
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"  ERROR: ffmpeg exited with code {result.returncode}", file=sys.stderr)
            failed += 1
        else:
            succeeded += 1

    if not args.dry_run:
        print(f"\nDone: {succeeded} succeeded, {skipped} skipped, {failed} failed.")


if __name__ == "__main__":
    main()
