#!/usr/bin/env python3
"""
BTIS3053 multi-camera pipeline - shared EDL resolution.
# this is spi project
This is the bridge between the Editing Decision List and the code. The EDL
stores only master time, camera and intent; everything concrete - source
timecodes, output positions, audio regions, total runtime - is derived here so
that the validator and the renderer can never disagree about what the EDL means.

Master time T has T = 0 at the audio sync event. For any camera,

    source_time = T + offset(camera)

with offsets measured by sync_verify.py and read from out/sync_report.json.

Transition handles
------------------
A crossfade needs footage from before the cut point. Rather than shortening the
timeline, the incoming segment reaches back `transition_sec` earlier in master
time and fades up over the outgoing one, exactly as a handle works in a normal
editor. Output time therefore stays a fixed shift of master time:

    output_time = T - body_in + title_duration

which keeps the audio bed a straight 1:1 map and is why no audio drift can creep
in as transitions are added or removed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out"

CAMERA_FILES = {
    "cam1": ROOT / "Camera1" / "Camera1-1.mp4",
    "cam2": ROOT / "Camera2" / "Camera2-1.mp4",
    "cam3": ROOT / "Camera3" / "Camera3-1.mp4",
    "cam4": ROOT / "Camera4" / "Camera4-1.mp4",
}

OVERLAP_TRANSITIONS = {"crossfade", "fade_in"}


@dataclass
class Segment:
    id: int
    camera: str
    t_in: float
    t_out: float
    reason: str
    transition: str
    transition_sec: float
    handle: float          # master-time reach-back for the incoming crossfade
    src_in: float          # timecode in the camera's own file
    src_out: float
    out_start: float       # position on the rendered timeline
    out_end: float
    lower_third: dict | None
    reviewed_by: str
    review_note: str

    @property
    def duration(self) -> float:
        return self.t_out - self.t_in

    @property
    def clip_duration(self) -> float:
        return self.src_out - self.src_in


@dataclass
class AudioRegion:
    camera: str
    t_in: float
    t_out: float
    src_in: float
    src_out: float
    out_start: float
    fade_in: float
    fade_out: float
    is_override: bool


@dataclass
class Timeline:
    edl: dict
    offsets: dict
    durations: dict
    segments: list[Segment]
    audio: list[AudioRegion]
    body_in: float
    body_out: float
    title_duration: float
    credits_duration: float
    credits_fade: float
    credits_out_start: float
    total_duration: float
    warnings: list[str] = field(default_factory=list)

    def out_time(self, t: float) -> float:
        return t - self.body_in + self.title_duration

    def live(self, camera: str, t: float) -> bool:
        return -self.offsets[camera] <= t <= self.durations[camera] - self.offsets[camera]

    def coverage(self, camera: str) -> tuple[float, float]:
        return -self.offsets[camera], self.durations[camera] - self.offsets[camera]


def load_sync(path: Path | None = None) -> tuple[dict, dict]:
    path = path or (OUT / "sync_report.json")
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run sync_verify.py first - the EDL is expressed in "
            f"master time and cannot be resolved without measured offsets."
        )
    report = json.loads(path.read_text(encoding="utf-8"))
    offsets = report["offsets_sec"]
    durations = {c: report["cameras"][c]["duration_sec"] for c in report["cameras"]}
    return offsets, durations


def load_edl(path: Path | None = None) -> dict:
    path = path or (HERE / "edl.json")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve(edl: dict | None = None,
            offsets: dict | None = None,
            durations: dict | None = None) -> Timeline:
    """Turn the declarative EDL into concrete source and output timecodes."""
    edl = edl if edl is not None else load_edl()
    if offsets is None or durations is None:
        offsets, durations = load_sync()

    warnings: list[str] = []

    raw = sorted(edl["segments"], key=lambda s: s["t_in"])
    body_in = min(s["t_in"] for s in raw)
    body_out = max(s["t_out"] for s in raw)

    title = edl.get("title", {}) or {}
    credits = edl.get("credits", {}) or {}
    title_duration = float(title.get("duration", 0.0))
    credits_duration = float(credits.get("duration", 0.0))
    credits_fade = float(credits.get("fade", 0.0))

    def out_time(t: float) -> float:
        return t - body_in + title_duration

    segments: list[Segment] = []
    for s in raw:
        cam = s["camera"]
        transition = s.get("transition", "cut")
        tsec = float(s.get("transition_sec", 0.0) or 0.0)
        handle = tsec if transition in OVERLAP_TRANSITIONS else 0.0

        clip_t_in = s["t_in"] - handle
        cov_lo, cov_hi = -offsets[cam], durations[cam] - offsets[cam]
        if handle and clip_t_in < cov_lo:
            warnings.append(
                f"segment {s['id']}: {cam} has no footage for the {tsec:g}s transition "
                f"handle before T={s['t_in']:g}; handle trimmed to fit"
            )
            clip_t_in = cov_lo
            handle = s["t_in"] - clip_t_in

        segments.append(Segment(
            id=s["id"], camera=cam,
            t_in=s["t_in"], t_out=s["t_out"],
            reason=s.get("reason", ""),
            transition=transition, transition_sec=tsec, handle=handle,
            src_in=clip_t_in + offsets[cam],
            src_out=s["t_out"] + offsets[cam],
            out_start=out_time(clip_t_in),
            out_end=out_time(s["t_out"]),
            lower_third=s.get("lower_third"),
            reviewed_by=s.get("reviewed_by", ""),
            review_note=s.get("review_note", ""),
        ))

    audio = _resolve_audio(edl, offsets, durations, body_in, body_out,
                           title_duration, credits_duration, credits_fade,
                           out_time, warnings)

    return Timeline(
        edl=edl, offsets=offsets, durations=durations,
        segments=segments, audio=audio,
        body_in=body_in, body_out=body_out,
        title_duration=title_duration,
        credits_duration=credits_duration,
        credits_fade=credits_fade,
        credits_out_start=out_time(body_out) - credits_fade,
        total_duration=title_duration + (body_out - body_in)
                       + credits_duration - credits_fade,
        warnings=warnings,
    )


def _resolve_audio(edl, offsets, durations, body_in, body_out,
                   title_duration, credits_duration, credits_fade,
                   out_time, warnings) -> list[AudioRegion]:
    """
    Build a gapless audio track as a sequence of regions.

    The bed runs under the title and the credits as well as the body, so the
    video never opens or closes in silence. Where an override is enabled the bed
    is genuinely replaced rather than mixed on top: each region reaches
    `crossfade` seconds into its neighbour and fades, so the two overlap at
    complementary gains and the sum stays roughly level.
    """
    spec = edl.get("audio", {}) or {}
    bed_cam = spec.get("bed", {}).get("camera")
    if not bed_cam:
        return []

    bed_lo, bed_hi = -offsets[bed_cam], durations[bed_cam] - offsets[bed_cam]

    audio_in = body_in - title_duration
    audio_out = body_out + (credits_duration - credits_fade)
    if audio_in < bed_lo:
        warnings.append(
            f"audio bed {bed_cam} starts at T={bed_lo:.2f}s, after the title needs it "
            f"at T={audio_in:.2f}s; the title will open in partial silence"
        )
        audio_in = bed_lo
    if audio_out > bed_hi:
        warnings.append(
            f"audio bed {bed_cam} ends at T={bed_hi:.2f}s, before the credits need it "
            f"at T={audio_out:.2f}s; the credits will end in partial silence"
        )
        audio_out = bed_hi

    overrides = [o for o in spec.get("overrides", []) if o.get("enabled")]
    overrides.sort(key=lambda o: o["t_in"])

    plan: list[tuple[str, float, float, bool]] = []
    cursor = audio_in
    for ov in overrides:
        cam = ov["camera"]
        lo, hi = -offsets[cam], durations[cam] - offsets[cam]
        t_in, t_out = max(ov["t_in"], lo, cursor), min(ov["t_out"], hi, audio_out)
        if t_out - t_in <= 0:
            warnings.append(f"audio override on {cam} has no usable span; skipped")
            continue
        if (t_in, t_out) != (ov["t_in"], ov["t_out"]):
            warnings.append(
                f"audio override on {cam} clamped to its coverage: "
                f"T={t_in:.2f}s..{t_out:.2f}s (asked {ov['t_in']:g}..{ov['t_out']:g})"
            )
        if t_in > cursor:
            plan.append((bed_cam, cursor, t_in, False))
        plan.append((cam, t_in, t_out, True))
        cursor = t_out
    if cursor < audio_out:
        plan.append((bed_cam, cursor, audio_out, False))

    fade = float(overrides[0].get("crossfade", 1.0)) if overrides else 0.0
    regions: list[AudioRegion] = []
    for i, (cam, t_in, t_out, is_ov) in enumerate(plan):
        lo, hi = -offsets[cam], durations[cam] - offsets[cam]
        f_in = fade if i > 0 else 0.0
        f_out = fade if i < len(plan) - 1 else 0.0
        # Reach into the neighbouring region so the two overlap and crossfade.
        start = max(t_in - f_in, lo)
        end = min(t_out + f_out, hi)
        regions.append(AudioRegion(
            camera=cam, t_in=start, t_out=end,
            src_in=start + offsets[cam], src_out=end + offsets[cam],
            out_start=out_time(start),
            fade_in=(t_in - start) if i > 0 else 0.0,
            fade_out=(end - t_out) if i < len(plan) - 1 else 0.0,
            is_override=is_ov,
        ))
    return regions


def switches(segments: list[Segment]) -> int:
    return sum(1 for a, b in zip(segments, segments[1:]) if a.camera != b.camera)
