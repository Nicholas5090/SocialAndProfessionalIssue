#!/usr/bin/env python3
"""
BTIS3053 multi-camera pipeline - Step 5: human review sheet.

Samples the rendered video once per EDL segment and lays the frames out with
the decision that produced each one. This is what a reviewer signs off against:
the claim in the EDL and the frame it actually produced, side by side.

The pipeline is semi-automated, and this is the step that makes that honest. A
reviewer who spots a bad call edits the segment in edl.json, fills in
reviewed_by and review_note, and re-renders. Nothing is published on the
strength of the automation alone.

Doubles as the screenshot evidence for section 5.1 of the report.

Usage:  py -3 review_sheet.py [--video out/final_video_draft.mp4]

Output: out/review_sheet.jpg
"""

import argparse
import io
import subprocess
import sys
import textwrap
from pathlib import Path

from edl_core import HERE, OUT, load_edl, load_sync, resolve

THUMB_W, THUMB_H = 400, 225
CAPTION_H, PAD, HEADER_H = 96, 8, 52
COLUMNS = 4


def grab(video: Path, t: float):
    from PIL import Image
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", "-vf", f"scale={THUMB_W}:{THUMB_H}",
         "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None
    return Image.open(io.BytesIO(proc.stdout)).convert("RGB")


def main() -> int:
    from PIL import Image, ImageDraw, ImageFont

    ap = argparse.ArgumentParser()
    ap.add_argument("--edl", type=Path, default=HERE / "edl.json")
    ap.add_argument("--video", type=Path, default=OUT / "final_video_draft.mp4")
    args = ap.parse_args()

    if not args.video.exists():
        print(f"ERROR: {args.video} not found. Render it first "
              f"(py -3 render_edl.py --fast).", file=sys.stderr)
        return 1

    tl = resolve(load_edl(args.edl), *load_sync())

    cells = [{
        "t": tl.title_duration / 2,
        "head": "TITLE",
        "sub": f"0.0-{tl.title_duration:g}s",
        "body": " / ".join(tl.edl["title"]["lines"]),
        "state": "",
    }]
    for s in tl.segments:
        cells.append({
            "t": tl.out_time((s.t_in + s.t_out) / 2),
            "head": f"SEG {s.id}   {s.camera}",
            "sub": (f"master {s.t_in:+.1f}..{s.t_out:+.1f}s  |  src "
                    f"{s.src_in:.1f}..{s.src_out:.1f}s  |  {s.transition}"),
            "body": s.reason,
            "state": f"reviewed: {s.reviewed_by}" if s.reviewed_by.strip() else "UNREVIEWED",
        })
    cells.append({
        "t": tl.credits_out_start + tl.credits_duration / 2,
        "head": "CREDITS",
        "sub": f"{tl.credits_out_start:.1f}-{tl.total_duration:.1f}s",
        "body": tl.edl["credits"]["lines"][0],
        "state": "",
    })

    rows = (len(cells) + COLUMNS - 1) // COLUMNS
    cell_w, cell_h = THUMB_W + PAD, THUMB_H + CAPTION_H + PAD
    sheet = Image.new("RGB", (COLUMNS * cell_w + PAD, HEADER_H + rows * cell_h + PAD),
                      (22, 22, 26))
    draw = ImageDraw.Draw(sheet)

    def font(size, bold=False):
        for name in ([r"C:\Windows\Fonts\arialbd.ttf"] if bold else []) + \
                    [r"C:\Windows\Fonts\arial.ttf"]:
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                continue
        return ImageFont.load_default()

    f_head, f_sub, f_body = font(15, bold=True), font(11), font(12)

    reviewed = sum(1 for s in tl.segments if s.reviewed_by.strip())
    draw.text((PAD + 4, 10), f"Human review sheet  -  {args.video.name}",
              fill=(240, 240, 245), font=font(18, bold=True))
    draw.text((PAD + 4, 32),
              f"{len(tl.segments)} segments, {tl.total_duration:.1f}s, "
              f"{len({s.camera for s in tl.segments})} camera angles  |  "
              f"{reviewed}/{len(tl.segments)} signed off",
              fill=(170, 170, 185), font=f_sub)

    for i, cell in enumerate(cells):
        cx = PAD + (i % COLUMNS) * cell_w
        cy = HEADER_H + (i // COLUMNS) * cell_h

        img = grab(args.video, cell["t"])
        if img is None:
            draw.rectangle([cx, cy, cx + THUMB_W, cy + THUMB_H], fill=(60, 30, 30))
        else:
            sheet.paste(img, (cx, cy))

        ty = cy + THUMB_H + 5
        draw.text((cx + 2, ty), cell["head"], fill=(255, 235, 120), font=f_head)
        if cell["state"]:
            colour = (120, 220, 140) if cell["state"].startswith("reviewed") else (235, 120, 120)
            w = draw.textlength(cell["state"], font=f_sub)
            draw.text((cx + THUMB_W - w - 2, ty + 2), cell["state"], fill=colour, font=f_sub)

        draw.text((cx + 2, ty + 19), cell["sub"], fill=(150, 152, 165), font=f_sub)
        for j, line in enumerate(textwrap.wrap(cell["body"], width=58)[:3]):
            draw.text((cx + 2, ty + 34 + j * 15), line, fill=(214, 216, 226), font=f_body)

    OUT.mkdir(exist_ok=True)
    path = OUT / "review_sheet.jpg"
    sheet.save(path, quality=88)
    print(f"Review sheet: {path}")
    print(f"{reviewed}/{len(tl.segments)} segments signed off. "
          f"Record approvals in the reviewed_by and review_note fields of edl.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
