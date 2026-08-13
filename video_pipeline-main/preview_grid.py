#!/usr/bin/env python3
"""
BTIS3053 multi-camera pipeline - Step 2 aid: segment selection contact sheet.

Renders a grid of thumbnails: one row per camera, one column per master-time
sample. Because every column is the same instant of real time across all four
cameras, this is also a visual proof that the offsets from sync_verify.py are
correct - if they were wrong the columns would show different moments.

Use it to choose which camera covers which moment before writing the EDL, so
the "reason for selection" column records something that was actually seen
rather than assumed.

Cells are greyed out where that camera was not yet recording or had stopped.

Usage:  py -3 preview_grid.py [--from -50 --to 65 --columns 10]

Output: out/preview_grid.jpg
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out"

THUMB_W, THUMB_H = 320, 180
PAD, LABEL_W, HEADER_H = 4, 78, 26

CAMERA_FILES = {
    "cam1": ROOT / "Camera1" / "Camera1-1.mp4",
    "cam2": ROOT / "Camera2" / "Camera2-1.mp4",
    "cam3": ROOT / "Camera3" / "Camera3-1.mp4",
    "cam4": ROOT / "Camera4" / "Camera4-1.mp4",
}


def grab(path: Path, src_t: float):
    """Single frame at src_t seconds, as a PIL image, or None if out of range."""
    from PIL import Image
    import io

    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{src_t:.3f}", "-i", str(path),
         "-frames:v", "1", "-vf", f"scale={THUMB_W}:{THUMB_H}",
         "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return Image.open(io.BytesIO(proc.stdout)).convert("RGB")


def main() -> int:
    from PIL import Image, ImageDraw, ImageFont

    report_path = OUT / "sync_report.json"
    if not report_path.exists():
        print("ERROR: run sync_verify.py first (out/sync_report.json missing)", file=sys.stderr)
        return 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    offsets = report["offsets_sec"]
    durations = {c: report["cameras"][c]["duration_sec"] for c in report["cameras"]}

    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="t_from", type=float,
                    default=report["summary"]["master_t_in"])
    ap.add_argument("--to", dest="t_to", type=float,
                    default=report["summary"]["master_t_out"])
    ap.add_argument("--columns", type=int, default=10)
    args = ap.parse_args()

    step = (args.t_to - args.t_from) / (args.columns - 1)
    times = [args.t_from + i * step for i in range(args.columns)]
    cams = sorted(CAMERA_FILES)

    sheet_w = LABEL_W + args.columns * (THUMB_W + PAD) + PAD
    sheet_h = HEADER_H + len(cams) * (THUMB_H + PAD) + PAD
    sheet = Image.new("RGB", (sheet_w, sheet_h), (24, 24, 28))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
        small = ImageFont.truetype("arial.ttf", 11)
    except OSError:
        font = small = ImageFont.load_default()

    for col, t in enumerate(times):
        x = LABEL_W + col * (THUMB_W + PAD)
        draw.text((x + 4, 6), f"T = {t:+.1f}s", fill=(235, 235, 235), font=font)

    for row, cam in enumerate(cams):
        y = HEADER_H + row * (THUMB_H + PAD)
        draw.text((6, y + THUMB_H // 2 - 14), cam, fill=(235, 235, 235), font=font)
        draw.text((6, y + THUMB_H // 2 + 2),
                  f"+{offsets[cam]:.2f}s", fill=(150, 150, 160), font=small)

        for col, t in enumerate(times):
            x = LABEL_W + col * (THUMB_W + PAD)
            src_t = t + offsets[cam]

            if not (0 <= src_t <= durations[cam]):
                draw.rectangle([x, y, x + THUMB_W, y + THUMB_H], fill=(38, 38, 44))
                draw.text((x + THUMB_W // 2 - 34, y + THUMB_H // 2 - 7),
                          "not recording", fill=(110, 110, 120), font=small)
                continue

            img = grab(CAMERA_FILES[cam], src_t)
            if img is None:
                draw.rectangle([x, y, x + THUMB_W, y + THUMB_H], fill=(60, 30, 30))
                continue
            sheet.paste(img, (x, y))
            draw.text((x + 4, y + THUMB_H - 15), f"src {src_t:.1f}s",
                      fill=(255, 235, 120), font=small)

    OUT.mkdir(exist_ok=True)
    path = OUT / "preview_grid.jpg"
    sheet.save(path, quality=88)
    print(f"Contact sheet: {path}")
    print(f"Master time {args.t_from:.1f}s .. {args.t_to:.1f}s in {args.columns} columns "
          f"(every {step:.1f}s)")
    print("Each column is the same real instant across all four cameras.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
