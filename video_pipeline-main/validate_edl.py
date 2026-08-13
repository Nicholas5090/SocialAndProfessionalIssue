#!/usr/bin/env python3
"""
BTIS3053 multi-camera pipeline - Step 3: EDL validation and human review status.

Checks the EDL against two separate things:

  1. Physical reality - every segment must ask for footage the camera actually
     recorded, at a master time it was actually running. A camera that stopped
     early cannot be cut to, and the validator is what catches that before a
     twenty-minute render does.

  2. The assignment's minimum prototype requirements - at least two camera
     angles, at least three switches, an opening title, a closing credit screen,
     a lower-third, a transition, and a final runtime of 60 to 180 seconds.

It also reports how many segments carry a human sign-off. The pipeline is
semi-automated by design: an unreviewed EDL is an incomplete one, so the review
count is printed as a first-class result rather than buried.

Usage:  py -3 validate_edl.py [--edl edl.json] [--csv]

Exit code 0 if every check passes, 1 otherwise.
"""

import argparse
import csv
import sys
from pathlib import Path

from edl_core import HERE, OUT, resolve, load_edl, load_sync, switches

MIN_ANGLES, MIN_SWITCHES = 2, 3
MIN_RUNTIME, MAX_RUNTIME = 60.0, 180.0


class Checks:
  # The verification result collector is used to record the PASS/FAIL status, name, and detailed information of each individual test.
    def __init__(self):
        self.rows = []

    def add(self, ok: bool, name: str, detail: str = "") -> bool:
        self.rows.append((ok, name, detail))
        return ok

    @property
    def failed(self):
        return [r for r in self.rows if not r[0]]

    def report(self):
        width = max(len(n) for _, n, _ in self.rows) + 2
        for ok, name, detail in self.rows:
            print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}{detail}")


def check_physical(tl, c: Checks):
    for s in tl.segments:
        lo, hi = tl.coverage(s.camera)
        dur = tl.durations[s.camera]

        c.add(s.duration > 0, f"segment {s.id} has positive duration",
              f"{s.duration:g}s")
        c.add(0 <= s.src_in and s.src_out <= dur,
              f"segment {s.id} inside {s.camera} file",
              f"src {s.src_in:.2f}..{s.src_out:.2f}s of {dur:.2f}s")
        # Handle offset check: ensures segment transition handles stay within physical recording bounds
        c.add(lo <= s.t_in - s.handle and s.t_out <= hi,
              f"segment {s.id} within {s.camera} coverage",
              f"T {s.t_in - s.handle:.2f}..{s.t_out:.2f}s in {lo:.2f}..{hi:.2f}s")

    for a, b in zip(tl.segments, tl.segments[1:]):
        gap = b.t_in - a.t_out
        c.add(abs(gap) < 1e-6, f"segments {a.id}->{b.id} join cleanly",
              "contiguous" if abs(gap) < 1e-6
              else (f"{gap:.2f}s gap" if gap > 0 else f"{-gap:.2f}s overlap"))

    for r in tl.audio:
        dur = tl.durations[r.camera]
        c.add(0 <= r.src_in and r.src_out <= dur,
              f"audio region on {r.camera} inside file",
              f"src {r.src_in:.2f}..{r.src_out:.2f}s of {dur:.2f}s")

    if tl.audio:
        covered = sum(r.t_out - r.t_in for r in tl.audio)
        span = tl.audio[-1].t_out - tl.audio[0].t_in
        c.add(covered >= span - 1e-6, "audio track is gapless",
              f"{span:.2f}s spanned")
        c.add(tl.audio[0].t_in <= tl.body_in and tl.audio[-1].t_out >= tl.body_out,
              "audio covers the whole body",
              f"T {tl.audio[0].t_in:.2f}..{tl.audio[-1].t_out:.2f}s")


def check_requirements(tl, c: Checks):
    angles = {s.camera for s in tl.segments}
    n_switch = switches(tl.segments)
    lowers = [s for s in tl.segments if s.lower_third]
    trans = [s for s in tl.segments if s.transition != "cut"]

    c.add(len(angles) >= MIN_ANGLES, f"at least {MIN_ANGLES} camera angles",
          f"{len(angles)} used: {', '.join(sorted(angles))}")
    c.add(n_switch >= MIN_SWITCHES, f"at least {MIN_SWITCHES} camera switches",
          f"{n_switch} switches")
    c.add(bool(tl.edl.get("title", {}).get("lines")), "opening title card",
          f"{tl.title_duration:g}s")
    c.add(bool(tl.edl.get("credits", {}).get("lines")), "closing credit screen",
          f"{tl.credits_duration:g}s")
    c.add(bool(lowers), "subtitle / label / lower-third",
          f"{len(lowers)} on segment(s) {', '.join(str(s.id) for s in lowers)}")
    c.add(bool(trans), "at least one transition",
          ", ".join(f"seg {s.id} {s.transition}" for s in trans))
    c.add(MIN_RUNTIME <= tl.total_duration <= MAX_RUNTIME,
          f"runtime within {MIN_RUNTIME:g}-{MAX_RUNTIME:g}s",
          f"{tl.total_duration:.2f}s")
    c.add(all(s.reason.strip() for s in tl.segments),
          "every segment states a reason",
          f"{sum(1 for s in tl.segments if s.reason.strip())}/{len(tl.segments)}")


def write_csv(tl, path: Path):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "t_in_master", "t_out_master", "duration", "camera",
                    "src_in", "src_out", "out_start", "transition", "transition_sec",
                    "reason", "reviewed_by", "review_note"])
        for s in tl.segments:
            w.writerow([s.id, f"{s.t_in:.2f}", f"{s.t_out:.2f}", f"{s.duration:.2f}",
                        s.camera, f"{s.src_in:.2f}", f"{s.src_out:.2f}",
                        f"{s.out_start:.2f}", s.transition, s.transition_sec or "",
                        s.reason, s.reviewed_by, s.review_note])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edl", type=Path, default=HERE / "edl.json")
    ap.add_argument("--csv", action="store_true",
                    help="also write out/edl_resolved.csv for the report appendix")
    args = ap.parse_args()

    tl = resolve(load_edl(args.edl), *load_sync())

    print(f"EDL           {args.edl.name}  ({len(tl.segments)} segments)")
    print(f"Master body   T {tl.body_in:+.2f}s .. {tl.body_out:+.2f}s "
          f"({tl.body_out - tl.body_in:.2f}s)")
    print(f"Runtime       {tl.title_duration:g}s title + "
          f"{tl.body_out - tl.body_in:.2f}s body + {tl.credits_duration:g}s credits "
          f"- {tl.credits_fade:g}s overlap = {tl.total_duration:.2f}s\n")

    hdr = (f"{'seg':>4}{'master in':>11}{'master out':>11}{'dur':>7}  {'cam':<5}"
           f"{'src in':>9}{'src out':>9}  {'out at':>8}  transition")
    print(hdr)
    print("-" * len(hdr))
    for s in tl.segments:
        tr = s.transition if s.transition == "cut" else f"{s.transition} {s.transition_sec:g}s"
        print(f"{s.id:>4}{s.t_in:>+10.2f}s{s.t_out:>+10.2f}s{s.duration:>6.1f}s  "
              f"{s.camera:<5}{s.src_in:>8.2f}s{s.src_out:>8.2f}s  "
              f"{s.out_start:>7.2f}s  {tr}")

    if tl.audio:
        print(f"\n{'audio region':<16}{'master in':>11}{'master out':>11}"
              f"{'src in':>10}{'src out':>10}{'fades':>12}")
        print("-" * 70)
        for r in tl.audio:
            tag = f"{r.camera}{' (override)' if r.is_override else ' (bed)'}"
            print(f"{tag:<16}{r.t_in:>+10.2f}s{r.t_out:>+10.2f}s"
                  f"{r.src_in:>9.2f}s{r.src_out:>9.2f}s"
                  f"{r.fade_in:>6.1f}/{r.fade_out:<5.1f}")

    c = Checks()
    print("\nPhysical checks - does the footage actually exist?")
    check_physical(tl, c)
    n_phys = len(c.rows)
    c.report()

    print("\nAssignment minimum prototype requirements")
    before = len(c.rows)
    check_requirements(tl, c)
    width = max(len(n) for _, n, _ in c.rows) + 2
    for ok, name, detail in c.rows[before:]:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}{detail}")

    reviewed = [s for s in tl.segments if s.reviewed_by.strip()]
    print(f"\nHuman review  {len(reviewed)}/{len(tl.segments)} segments signed off")
    if len(reviewed) < len(tl.segments):
        pending = ", ".join(str(s.id) for s in tl.segments if not s.reviewed_by.strip())
        print(f"              awaiting sign-off on segment(s) {pending}")
        print("              (not a validation failure, but the EDL is not final "
              "until a person has approved every cut)")

    for w in tl.warnings:
        print(f"\nWARNING       {w}")

    if args.csv:
        OUT.mkdir(exist_ok=True)
        path = OUT / "edl_resolved.csv"
        write_csv(tl, path)
        print(f"\nWrote         {path}")

    failed = c.failed
    print(f"\n{'FAILED' if failed else 'OK'}: {len(c.rows) - len(failed)}/{len(c.rows)} "
          f"checks passed ({n_phys} physical, {len(c.rows) - n_phys} requirement)")
    if failed:
        for _, name, detail in failed:
            print(f"  - {name}: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
