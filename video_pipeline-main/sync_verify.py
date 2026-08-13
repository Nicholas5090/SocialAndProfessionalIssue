#!/usr/bin/env python3
"""
BTIS3053 multi-camera pipeline - Step 1: synchronisation verification.

Measures the trim-from-head offset of each camera by cross-correlating audio
features against a reference camera. Camera 2 is the reference: it started last,
so it is the shortest clip and its offset is 0 by definition.

Two independent features are measured so they can corroborate each other:

  onset  - spectral flux (log-compressed, band-limited to speech/clap energy).
           Primary method. Responds to transients, so a clap or a burst of
           applause produces a sharp, unambiguous correlation peak.
  rms    - broadband loudness envelope. Secondary cross-check. Smoother, so its
           peak is inherently blunter; it is here to confirm the onset result,
           not to compete with it.

Raw-sample correlation is deliberately not used: the four microphones sit in
different corners of the hall, so direct sound and reverb arrive with different
phase and the waveforms do not match even when perfectly aligned.

Precision note: cameras metres apart see sound arrive at genuinely different
times (~3 ms per metre). At 30 fps one frame is 33 ms, so agreement to within
about a frame is the practical ceiling and is all the edit needs.

Everything runs locally through FFmpeg. No footage or audio leaves this machine,
which is the privacy claim made in section 5.2 of the report.

Outputs: out/sync_report.json  and  out/sync_waveforms.png
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy.signal import correlate

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "out"

SR = 8000                 # analysis sample rate (Hz)
HOP = 80                  # 10 ms hop -> 100 Hz feature rate
WIN = 512                 # 64 ms analysis window
FEAT_HZ = SR / HOP
BAND_LO, BAND_HI = 200, 3000   # Hz; speech and clap energy, skips handling rumble

STRONG_PSR = 2.5          # peak-to-sidelobe above this is a confident lock
MODERATE_PSR = 1.8
MIN_OVERLAP_SEC = 20.0    # a candidate lag must overlap this much to be scored
PAIR_TOLERANCE_SEC = 0.10 # 3 frames at 30 fps; tolerance for the pairwise check

REFERENCE = "cam2"

CAMERAS = {
    "cam1": {"file": ROOT / "Camera1" / "Camera1-1.mp4", "label": "front left",  "estimate": 57.0},
    "cam2": {"file": ROOT / "Camera2" / "Camera2-1.mp4", "label": "front right", "estimate":  0.0},
    "cam3": {"file": ROOT / "Camera3" / "Camera3-1.mp4", "label": "wide back",   "estimate": 49.0},
    "cam4": {"file": ROOT / "Camera4" / "Camera4-1.mp4", "label": "side angle",  "estimate": 39.0},
}


# --------------------------------------------------------------------------- io

def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def load_audio(path: Path) -> np.ndarray:
    """Decode to mono 16-bit PCM at SR Hz and return a float array in [-1, 1]."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
        capture_output=True, check=True,
    )
    return np.frombuffer(out.stdout, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------------- features

def _frames(audio: np.ndarray) -> np.ndarray:
    # Calculate max full frames that fit into the audio array
    n = max(0, (len(audio) - WIN) // HOP + 1)
    # Construct 2D index matrix via NumPy broadcasting: (1, WIN) + (n, 1) -> (n, WIN)
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    return audio[idx] * np.hanning(WIN)[None, :]


def _zscore(x: np.ndarray) -> np.ndarray:
    return (x - x.mean()) / (x.std() + 1e-12)


def onset_feature(audio: np.ndarray) -> np.ndarray:
    """
    Spectral flux: half-wave-rectified frame-to-frame rise in log-compressed
    magnitude, summed over the speech/clap band.

    Log compression before differencing is what makes this comparable across
    cameras. It removes the effect of each microphone's distance and gain, so
    only the *shape* of the attack survives.
    """
    spec = np.abs(np.fft.rfft(_frames(audio), axis=1))
    freqs = np.fft.rfftfreq(WIN, 1 / SR)
    band = (freqs >= BAND_LO) & (freqs <= BAND_HI)

    logmag = np.log1p(100.0 * spec[:, band])
    flux = np.diff(logmag, axis=0, prepend=logmag[:1])
    return _zscore(np.maximum(flux, 0.0).sum(axis=1))


def rms_feature(audio: np.ndarray) -> np.ndarray:
    """Broadband loudness envelope, log-compressed. Secondary cross-check."""
    rms = np.sqrt((_frames(audio) ** 2).mean(axis=1) + 1e-12)
    return _zscore(np.log(rms + 1e-6))


# ------------------------------------------------------------------- correlation

def xcorr_lag(feat: np.ndarray, feat_ref: np.ndarray) -> dict:
    """
    Find where feat_ref's content begins inside feat. The lag may be negative,
    meaning feat_ref started first.

    When feat_ref is the reference camera the returned lag is exactly the
    trim-from-head offset: trimming `lag` seconds puts this camera's sync point
    at master time T = 0, the same instant as the reference camera's own start.

    Candidate lags overlap by different amounts, and a short overlap is a noisy
    measurement: with z-scored features the mean product at a chance lag has a
    spread of roughly 1/sqrt(overlap). Scoring by the mean product alone would
    therefore let a noisy 25 s overlap outrank a clean 96 s one. Dividing the
    raw sum by sqrt(overlap) instead gives each lag a z-statistic, so a short
    overlap has to correlate far more strongly to win. Lags overlapping by less
    than MIN_OVERLAP_SEC are excluded outright.
    """
    n, m = len(feat), len(feat_ref)
    corr = correlate(feat, feat_ref, mode="full", method="fft")

    lags = np.arange(-(m - 1), n)
    overlap = np.minimum(n, m + lags) - np.maximum(0, lags)

    valid = overlap >= MIN_OVERLAP_SEC * FEAT_HZ
    if not valid.any():
        return {"lag_sec": None, "peak": None, "psr": None, "top_candidates": []}

    score = np.full(len(corr), -np.inf)
    score[valid] = corr[valid] / np.sqrt(overlap[valid])

    k = int(np.argmax(score))
    peak = float(score[k])
    peak_corr = float(corr[k] / overlap[k])   # mean product: interpretable as a
                                              # correlation, but not used to rank

    # Peak-to-sidelobe ratio: how far the winning lag beats every rival lag more
    # than 1 s away from it. This, not the raw correlation value, is what says
    # whether the answer is trustworthy.
    guard = int(FEAT_HZ)
    mask = np.isfinite(score)
    mask[max(0, k - guard): k + guard + 1] = False
    sidelobe = float(np.abs(score[mask]).max()) if mask.any() else 0.0

    # Runner-up lags, to expose a near-tie that a single number would hide.
    rivals = []
    scratch = score.copy()
    for _ in range(3):
        j = int(np.argmax(scratch))
        if not np.isfinite(scratch[j]):
            break
        rivals.append({"lag_sec": round(float(lags[j]) / FEAT_HZ, 3),
                       "score": round(float(scratch[j]), 4)})
        scratch[max(0, j - guard): j + guard + 1] = -np.inf

    return {
        "lag_sec": round(float(lags[k]) / FEAT_HZ, 3),
        "peak": round(peak_corr, 4),
        "overlap_sec": round(float(overlap[k]) / FEAT_HZ, 2),
        "psr": round(peak / sidelobe, 2) if sidelobe > 1e-9 else None,
        "top_candidates": rivals,
    }


def pairwise_consistency(feats: dict, offsets: dict) -> list:
    """
    Independent check on the offsets: measure all six camera pairs directly and
    confirm each agrees with the offsets derived through the reference camera.

    For any pair, lag(i, j) must equal offset[i] - offset[j]. Nothing forces
    that to hold, so if all six residuals land within a frame or two the
    offsets are consistent as a set, not merely plausible one at a time.
    """
    names = sorted(feats)
    rows = []
    for a in range(len(names)):
        for b in range(a + 1, len(names)):
            i, j = names[a], names[b]
            measured = xcorr_lag(feats[i], feats[j])
            if measured["lag_sec"] is None:
                continue
            predicted = offsets[i] - offsets[j]
            residual = measured["lag_sec"] - predicted
            rows.append({
                "pair": f"{i}-{j}",
                "measured_lag_sec": measured["lag_sec"],
                "predicted_lag_sec": round(predicted, 3),
                "residual_sec": round(residual, 3),
                "psr": measured["psr"],
                "within_tolerance": abs(residual) <= PAIR_TOLERANCE_SEC,
            })
    return rows


def confidence(psr) -> str:
    if psr is None:
        return "reference"
    if psr >= STRONG_PSR:
        return "strong"
    return "moderate" if psr >= MODERATE_PSR else "weak"


# -------------------------------------------------------------------- coverage

def coverage_table(offsets: dict, durations: dict) -> list:
    """
    Camera availability on the master timeline, where T = 0 is the sync event.
    A camera is live from -offset to (duration - offset).
    """
    spans = {c: (-offsets[c], durations[c] - offsets[c]) for c in offsets}
    edges = sorted({round(v, 3) for span in spans.values() for v in span})

    rows = []
    for lo, hi in zip(edges, edges[1:]):
        mid = (lo + hi) / 2
        live = sorted(c for c, (a, b) in spans.items() if a <= mid <= b)
        rows.append({
            "t_in": round(lo, 3),
            "t_out": round(hi, 3),
            "duration": round(hi - lo, 3),
            "cameras": live,
            "count": len(live),
        })
    return rows


def plot(feats: dict, offsets: dict) -> Path | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    order = sorted(feats)
    fig, axes = plt.subplots(2, 1, figsize=(13, 7), sharex=True)
    for i, name in enumerate(order):
        t = np.arange(len(feats[name])) / FEAT_HZ
        axes[0].plot(t, feats[name] + 8 * i, lw=0.5, label=name)
        axes[1].plot(t - offsets[name], feats[name] + 8 * i, lw=0.5, label=name)

    axes[0].set_title("Spectral-flux onset envelopes, source time (before alignment)")
    axes[1].set_title("After applying measured offsets: master time, T = 0 at sync event")
    axes[1].axvline(0, color="k", ls="--", lw=1)
    axes[1].set_xlabel("seconds")
    for ax in axes:
        ax.legend(loc="upper right", fontsize=8, ncol=4)
        ax.set_yticks([])

    fig.tight_layout()
    path = OUT / "sync_waveforms.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


# ------------------------------------------------------------------------- main

def main() -> int:
    OUT.mkdir(exist_ok=True)

    missing = [c for c, m in CAMERAS.items() if not m["file"].exists()]
    if missing:
        print(f"ERROR: missing footage for {', '.join(missing)}", file=sys.stderr)
        return 1

    print(f"Reference camera : {REFERENCE}")
    print(f"Analysis         : {SR} Hz mono, {WIN}-sample window, "
          f"{HOP}-sample hop ({FEAT_HZ:.0f} Hz features), {BAND_LO}-{BAND_HI} Hz band\n")

    durations, onset, rms = {}, {}, {}
    for name, meta in CAMERAS.items():
        durations[name] = probe_duration(meta["file"])
        audio = load_audio(meta["file"])
        onset[name] = onset_feature(audio)
        rms[name] = rms_feature(audio)
        print(f"  {name} ({meta['label']:<11}) {durations[name]:7.2f} s")

    print()
    results, offsets = {}, {}
    for name in CAMERAS:
        if name == REFERENCE:
            primary = {"lag_sec": 0.0, "peak": 1.0, "psr": None, "top_candidates": []}
            secondary = dict(primary)
        else:
            primary = xcorr_lag(onset[name], onset[REFERENCE])
            secondary = xcorr_lag(rms[name], rms[REFERENCE])

        lag = primary["lag_sec"]
        est = CAMERAS[name]["estimate"]
        cross = secondary["lag_sec"] - lag

        results[name] = {
            "label": CAMERAS[name]["label"],
            "duration_sec": round(durations[name], 3),
            "offset_sec": lag,
            "confidence": confidence(primary["psr"]),
            "onset": primary,
            "rms_crosscheck": secondary,
            "methods_agree_within_1_frame": abs(cross) <= 1 / 30,
            "methods_delta_sec": round(cross, 3),
            "manual_estimate_sec": est,
            "estimate_delta_sec": round(lag - est, 3),
        }
        offsets[name] = lag

    hdr = (f"{'camera':<7}{'onset':>9}{'rms':>9}{'agree':>8}{'PSR':>7}"
           f"{'conf':>10}{'manual':>9}{'delta':>9}")
    print(hdr)
    print("-" * len(hdr))
    for name in sorted(results):
        r = results[name]
        psr = f"{r['onset']['psr']:.2f}" if r["onset"]["psr"] else "ref"
        agree = "yes" if r["methods_agree_within_1_frame"] else f"{r['methods_delta_sec']:+.2f}s"
        print(f"{name:<7}{r['offset_sec']:>8.2f}s{r['rms_crosscheck']['lag_sec']:>8.2f}s"
              f"{agree:>8}{psr:>7}{r['confidence']:>10}"
              f"{r['manual_estimate_sec']:>8.1f}s{r['estimate_delta_sec']:>+8.2f}s")

    weak = [n for n, r in results.items() if r["confidence"] == "weak"]
    if weak:
        print(f"\n  Weak lock on {', '.join(sorted(weak))} - runner-up lags:")
        for n in sorted(weak):
            cands = ", ".join(f"{c['lag_sec']:.2f}s ({c['score']:.3f})"
                              for c in results[n]["onset"]["top_candidates"])
            print(f"    {n}: {cands}")

    pairs = pairwise_consistency(onset, offsets)
    bad_pairs = [p for p in pairs if not p["within_tolerance"]]

    print(f"\nPairwise consistency check (all 6 pairs measured directly, "
          f"tolerance {PAIR_TOLERANCE_SEC * 1000:.0f} ms)")
    print(f"{'pair':<12}{'measured':>11}{'predicted':>12}{'residual':>11}{'PSR':>7}   ok")
    print("-" * 57)
    for p in pairs:
        psr = f"{p['psr']:.2f}" if p["psr"] else "  -"
        print(f"{p['pair']:<12}{p['measured_lag_sec']:>10.2f}s{p['predicted_lag_sec']:>11.2f}s"
              f"{p['residual_sec']:>+10.3f}s{psr:>7}   {'yes' if p['within_tolerance'] else 'NO'}")

    if bad_pairs:
        print(f"\n  {len(bad_pairs)} pair(s) inconsistent - offsets are NOT a valid set.")
    else:
        print(f"\n  All {len(pairs)} pairs consistent. The offsets agree as a set, "
              f"not just one at a time.")

    coverage = coverage_table(offsets, durations)
    multicam = sum(r["duration"] for r in coverage if r["count"] >= 2)
    full = sum(r["duration"] for r in coverage if r["count"] == 4)
    master_in = min(r["t_in"] for r in coverage)
    master_out = max(r["t_out"] for r in coverage)

    print("\nCamera availability on the master timeline (T = 0 at sync event)")
    print(f"{'t_in':>9}{'t_out':>9}{'dur':>8}   cameras")
    print("-" * 46)
    for r in coverage:
        print(f"{r['t_in']:>8.2f}s{r['t_out']:>8.2f}s{r['duration']:>7.2f}s   "
              f"{', '.join(r['cameras'])}")

    print(f"\nMaster span            {master_in:.2f}s .. {master_out:.2f}s "
          f"({master_out - master_in:.2f}s)")
    print(f"All 4 cameras live     {full:.2f}s   (below the 60 s minimum on its own)")
    print(f"2+ cameras live        {multicam:.2f}s  <- build the cut inside this window")

    png = plot(onset, offsets)
    if png:
        print(f"\nWaveform figure        {png}")

    report = {
        "generated_by": "sync_verify.py",
        "method": "spectral-flux onset cross-correlation, RMS envelope cross-check",
        "processing": "fully local via FFmpeg; no footage or audio uploaded",
        "analysis": {
            "sample_rate_hz": SR, "window": WIN, "hop": HOP,
            "feature_rate_hz": FEAT_HZ, "band_hz": [BAND_LO, BAND_HI],
        },
        "reference_camera": REFERENCE,
        "offsets_sec": offsets,
        "cameras": results,
        "pairwise_consistency": pairs,
        "coverage": coverage,
        "summary": {
            "master_t_in": master_in,
            "master_t_out": master_out,
            "master_span_sec": round(master_out - master_in, 3),
            "all_four_cameras_sec": round(full, 3),
            "two_or_more_cameras_sec": round(multicam, 3),
            "weak_locks": sorted(weak),
            "pairwise_all_consistent": not bad_pairs,
            "max_pairwise_residual_sec": round(max((abs(p["residual_sec"]) for p in pairs),
                                                  default=0.0), 3),
        },
    }
    (OUT / "sync_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report                 {OUT / 'sync_report.json'}")

    # The pairwise check is the real gate. A single weak PSR is tolerable if
    # every pair still agrees; an inconsistent pair is not tolerable at all.
    return 2 if bad_pairs else 0


if __name__ == "__main__":
    sys.exit(main())
