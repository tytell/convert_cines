#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
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


def build_cmd(src, dst, args):
    cmd = ["ffmpeg"]
    if args.overwrite:
        cmd.append("-y")
    else:
        cmd.append("-n")

    if args.fps:
        cmd += ["-r", str(args.fps)]
    cmd += ["-i", str(src)]

    vf = build_vf(args.max_intensity, args.contrast, args.gamma)
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
                        help="Maximum output intensity via curves filter, 0.0–1.0 "
                             "(lower = brighter output; default: 1.0 = no adjustment)")
    parser.add_argument("--contrast", type=float, default=1.0,
                        help="Contrast via eq filter (default: 1.0 = no adjustment)")
    parser.add_argument("--gamma", type=float, default=1.0,
                        help="Gamma via eq filter (default: 1.0 = no adjustment)")
    parser.add_argument("--test-count", type=int, default=None,
                        help="Number of files to process in test mode (default: 1)")
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
        print(f"[{i}/{len(files)}] {src} → {dst}")

        if dst.exists() and not args.overwrite:
            print("  skipping (output exists)")
            skipped += 1
            continue

        if args.dry_run:
            cmd = build_cmd(src, dst, args)
            print(" ", " ".join(cmd))
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)
        cmd = build_cmd(src, dst, args)
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
