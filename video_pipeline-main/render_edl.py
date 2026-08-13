#!/usr/bin/env python3
"""
BTIS3053 multi-camera pipeline - Step 4: the EDL-to-code bridge.

Reads edl.json, resolves it through edl_core, and renders the final video with
MoviePy. Nothing about the edit is decided here: this file knows how to draw a
title card and how to place a clip, but every choice of what to show and when
comes from the EDL. Changing the edit means editing the JSON, not the code.

Video and audio are assembled independently. Each segment clip is stripped of
its own audio and positioned by output time, while the audio track is built
separately from the bed and override regions. Because edl_core keeps output time
a fixed shift of master time, the two cannot drift apart no matter how many
transitions are added.

Usage
  py -3 render_edl.py                 full quality render to out/final_video.mp4
  py -3 render_edl.py --fast          360p draft for checking the edit quickly
  py -3 render_edl.py --camera-tags   burn in which camera is live, for review
  py -3 render_edl.py --from 1 --to 4 render only segments 1 to 4
"""

import argparse
import sys
import time
from pathlib import Path

from moviepy import (AudioFileClip, ColorClip, CompositeAudioClip,
                     CompositeVideoClip, TextClip, VideoFileClip, afx, vfx)

from edl_core import CAMERA_FILES, HERE, OUT, load_edl, load_sync, resolve

BG = (12, 14, 20)
MASTER_FADE_IN, MASTER_FADE_OUT = 1.5, 2.0

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\times.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
]
BOLD_CANDIDATES = [
    r"C:\Windows\Fonts\timesbd.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
]


def pick_font(candidates: list[str]) -> str:
    for path in candidates:
        if Path(path).exists():
            return path
    raise FileNotFoundError(
        "No usable font found. Add a .ttf path to FONT_CANDIDATES in render_edl.py."
    )


def title_card(tl, w: int, h: int, font: str, bold: str):
    spec = tl.edl["title"]
    dur, fade = tl.title_duration, float(spec.get("fade", 1.0))

    layers = [ColorClip((w, h), color=BG, duration=dur)]

    # Caption boxes get an explicit height. Left to size themselves they come out
    # too short for a heading that wraps, and the last line is clipped.
    heading = TextClip(font=bold, text="\n".join(spec["lines"]),
                       font_size=int(w * 0.048), color="white",
                       method="caption", size=(int(w * 0.84), int(h * 0.42)),
                       text_align="center", vertical_align="center", duration=dur)
    layers.append(heading.with_position(("center", int(h * 0.18))))

    if spec.get("subtitle"):
        sub = TextClip(font=font, text=spec["subtitle"],
                       font_size=int(w * 0.028), color=(190, 195, 210),
                       method="caption", size=(int(w * 0.84), int(h * 0.12)),
                       text_align="center", vertical_align="top", duration=dur)
        layers.append(sub.with_position(("center", int(h * 0.62))))

    return (CompositeVideoClip(layers, size=(w, h))
            .with_duration(dur)
            .with_start(0)
            .with_effects([vfx.FadeIn(fade)]))


def credits_card(tl, w: int, h: int, font: str, bold: str):
    spec = tl.edl["credits"]
    dur, fade = tl.credits_duration, tl.credits_fade

    body = TextClip(font=font, text="\n".join(spec["lines"]),
                    font_size=int(w * 0.024), color=(225, 228, 238),
                    method="caption", size=(int(w * 0.80), int(h * 0.84)),
                    text_align="center", vertical_align="center",
                    interline=8, duration=dur)

    layers = [ColorClip((w, h), color=BG, duration=dur),
              body.with_position("center")]

    return (CompositeVideoClip(layers, size=(w, h))
            .with_duration(dur)
            .with_start(tl.credits_out_start)
            .with_effects([vfx.CrossFadeIn(fade)]))


def lower_third(spec: dict, seg, tl, w: int, h: int, font: str):
    """Semi-transparent bar with a caption, appearing partway into a segment."""
    start = tl.out_time(seg.t_in) + float(spec.get("at", 1.0))
    dur = float(spec.get("duration", 4.0))
    fade = min(0.5, dur / 4)

    text = TextClip(font=font, text=spec["text"], font_size=int(w * 0.028),
                    color="white", duration=dur)
    pad_x, pad_y = int(w * 0.018), int(h * 0.020)
    bar_w, bar_h = text.w + 2 * pad_x, text.h + 2 * pad_y
    x, y = int(w * 0.06), int(h * 0.80)

    bar = (ColorClip((bar_w, bar_h), color=(0, 0, 0), duration=dur)
           .with_opacity(0.55).with_position((x, y)))
    text = text.with_position((x + pad_x, y + pad_y))

    return [c.with_start(start).with_effects(
                [vfx.CrossFadeIn(fade), vfx.CrossFadeOut(fade)])
            for c in (bar, text)]


def camera_tag(seg, tl, w: int, h: int, font: str):
    """Debug overlay naming the live camera. For the human-review pass only."""
    label = f"{seg.camera.upper()}  {tl.edl.get('cameras', {}).get(seg.camera, '')}"
    text = TextClip(font=font, text=label.strip(), font_size=int(w * 0.020),
                    color=(255, 235, 120), duration=seg.duration)
    return text.with_start(tl.out_time(seg.t_in)).with_position(
        (int(w * 0.06), int(h * 0.06)))


def build_audio(tl):
    gain = float((tl.edl.get("audio", {}) or {}).get("gain", 1.0))
    regions = []
    for r in tl.audio:
        clip = AudioFileClip(str(CAMERA_FILES[r.camera])).subclipped(r.src_in, r.src_out)
        fx = []
        if r.fade_in > 0:
            fx.append(afx.AudioFadeIn(r.fade_in))
        if r.fade_out > 0:
            fx.append(afx.AudioFadeOut(r.fade_out))
        if fx:
            clip = clip.with_effects(fx)
        regions.append(clip.with_start(r.out_start))

    if not regions:
        return None
    effects = [afx.AudioFadeIn(MASTER_FADE_IN), afx.AudioFadeOut(MASTER_FADE_OUT)]
    if gain != 1.0:
        effects.append(afx.MultiplyVolume(gain))
    return CompositeAudioClip(regions).with_effects(effects)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edl", type=Path, default=HERE / "edl.json")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--fast", action="store_true",
                    help="640x360 ultrafast draft for checking the edit")
    ap.add_argument("--camera-tags", action="store_true",
                    help="burn in the live camera name, for the review pass")
    ap.add_argument("--from", dest="seg_from", type=int, default=None)
    ap.add_argument("--to", dest="seg_to", type=int, default=None)
    args = ap.parse_args()

    tl = resolve(load_edl(args.edl), *load_sync())
    for w in tl.warnings:
        print(f"WARNING: {w}")

    spec = tl.edl["output"]
    w, h = (640, 360) if args.fast else (spec["width"], spec["height"])
    fps = spec["fps"]
    font, bold = pick_font(FONT_CANDIDATES), pick_font(BOLD_CANDIDATES)

    segments = tl.segments
    if args.seg_from is not None:
        segments = [s for s in segments if s.id >= args.seg_from]
    if args.seg_to is not None:
        segments = [s for s in segments if s.id <= args.seg_to]
    if not segments:
        print("No segments selected.", file=sys.stderr)
        return 1
    partial = len(segments) != len(tl.segments)

    print(f"Rendering {len(segments)} segment(s) at {w}x{h} {fps}fps"
          f"{' [draft]' if args.fast else ''}")
    print(f"Timeline  {tl.total_duration:.2f}s "
          f"= {tl.title_duration:g}s title + {tl.body_out - tl.body_in:.2f}s body "
          f"+ {tl.credits_duration:g}s credits - {tl.credits_fade:g}s overlap")

    layers = []
    if not partial:
        layers.append(title_card(tl, w, h, font, bold))

    for s in segments:
        clip = (VideoFileClip(str(CAMERA_FILES[s.camera]))
                .subclipped(s.src_in, s.src_out)
                .resized((w, h))
                .with_audio(None)
                .with_start(s.out_start))
        if s.handle > 0:
            clip = clip.with_effects([vfx.CrossFadeIn(s.handle)])
        layers.append(clip)

        if s.lower_third:
            layers.extend(lower_third(s.lower_third, s, tl, w, h, font))
        if args.camera_tags:
            layers.append(camera_tag(s, tl, w, h, font))

    if not partial:
        layers.append(credits_card(tl, w, h, font, bold))

    video = CompositeVideoClip(layers, size=(w, h))
    if not partial:
        video = video.with_duration(tl.total_duration)
        audio = build_audio(tl)
        if audio is not None:
            video = video.with_audio(audio.with_duration(video.duration))

    out_path = args.out or (HERE / tl.edl["output"]["file"])
    if args.fast and args.out is None:
        out_path = out_path.with_name(out_path.stem + "_draft.mp4")
    if partial and args.out is None:
        out_path = out_path.with_name(
            f"{out_path.stem}_seg{segments[0].id}-{segments[-1].id}.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.time()
    video.write_videofile(
        str(out_path),
        fps=fps,
        codec="libx264",
        audio_codec="aac",
        audio_fps=spec.get("audio_fps", 44100),
        bitrate=None if args.fast else spec.get("video_bitrate"),
        preset="ultrafast" if args.fast else "medium",
        threads=None,
    )
    elapsed = time.time() - started

    size_mb = out_path.stat().st_size / 1e6
    print(f"\nWrote {out_path}")
    print(f"  {video.duration:.2f}s, {size_mb:.1f} MB, rendered in {elapsed / 60:.1f} min")
    if not partial:
        print(f"  {len({s.camera for s in segments})} camera angles, "
              f"{sum(1 for a, b in zip(segments, segments[1:]) if a.camera != b.camera)} "
              f"switches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
