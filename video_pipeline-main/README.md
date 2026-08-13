# Multi-Camera Editing Pipeline

Semi-automated pipeline that turns four unsynchronised camera recordings of a
graduation performance into one edited highlight video, driven by a
machine-readable Editing Decision List.

**Semi-automated, not automated.** The tools measure, validate and render. A
person decides what to show and signs off every cut before anything is
published. `validate_edl.py` and `review_sheet.py` exist to make that sign-off
a real step rather than a claim.

**Local-only.** Every stage runs on this machine through FFmpeg and Python. No
footage, audio or still frame is uploaded to any cloud or AI service at any
point in the pipeline.

---

## Requirements

Python 3.11+, FFmpeg on `PATH`, and:

```bash
py -3 -m pip install numpy scipy moviepy pillow matplotlib
```

Source footage is expected at `../Camera{1,2,3,4}/Camera{1,2,3,4}-1.mp4`.
Change the paths in `edl_core.py` (`CAMERA_FILES`) if yours differ.

## Running the pipeline

Run in order. Each stage writes to `out/`.

```bash
py -3 sync_verify.py
```
Measures each camera's offset by cross-correlating audio onset envelopes,
cross-checks with a second method, and verifies all six camera pairs are
mutually consistent. Writes `out/sync_report.json`, which every later stage
reads. **Run this first** — nothing else can resolve master time without it.

```bash
py -3 preview_grid.py --from -50 --to 65 --columns 10
```
Contact sheet of all four cameras at the same instants, for choosing segments.
Doubles as a visual check on the offsets: if they were wrong, the columns would
show different moments.

```bash
py -3 validate_edl.py --csv
```
Checks the EDL against the footage and against the assignment's minimum
prototype requirements. `--csv` writes `out/edl_resolved.csv` for the report
appendix. Run it after every EDL edit; it is far faster than discovering a
problem during a render.

```bash
py -3 render_edl.py --fast     # 360p draft, ~3 min
py -3 render_edl.py            # 720p final
```
Renders the EDL. `--camera-tags` burns in which camera is live, which is useful
during review. `--from N --to M` renders a range of segments only.

```bash
py -3 review_sheet.py
```
Samples the render once per segment and lays each frame beside the decision
that produced it. This is the human review artefact, and the screenshot
evidence for report section 5.1.

## Files

| File | Role |
|---|---|
| `sync_verify.py` | Measures and validates camera offsets |
| `preview_grid.py` | Source contact sheet for segment selection |
| `edl.json` | **The edit.** All editorial decisions live here |
| `edl_core.py` | Shared EDL resolution: master time to source and output time |
| `validate_edl.py` | Physical and requirement checks, resolved CSV export |
| `render_edl.py` | EDL-to-code bridge: renders the final video |
| `review_sheet.py` | Human review sheet from the rendered output |
| `out/` | All generated artefacts |

## Changing the edit

Edit `edl.json`, never the Python. To move a cut, change `t_in`/`t_out`; to
switch angle, change `camera`. Then:

```bash
py -3 validate_edl.py && py -3 render_edl.py --fast
```

The validator will refuse any segment asking for footage a camera did not
record, and will tell you the coverage window it does have.

## How it works

### Master time

`T = 0` is the audio sync event. Every camera has an offset, and

```
source_time = T + offset(camera)
```

The EDL is written entirely in master time, so a segment means the same real
instant regardless of which camera covers it. Offsets are read from
`out/sync_report.json`, so re-measuring them never requires rewriting the EDL.

### Synchronisation method

Spectral flux — the frame-to-frame rise in log-compressed magnitude across
200–3000 Hz — cross-correlated against a reference camera. Raw waveform
correlation fails here because the four microphones sit in different corners of
the hall and receive different phase and reverb. Log compression before
differencing removes each microphone's distance and gain, leaving only the shape
of each attack, which is what actually matches across cameras.

Candidate lags are scored as `correlation / sqrt(overlap)` rather than as a mean
product. With z-scored features the mean product at a chance lag varies by about
`1/sqrt(overlap)`, so scoring by mean alone lets a noisy short overlap outrank a
clean long one. This mattered: with mean scoring the cam1–cam3 pair locked onto
a spurious lag 93 s away from the truth.

Three independent confirmations are required before the offsets are trusted: a
second feature (RMS envelope) must agree, the peak must beat its rivals, and all
six camera pairs must be mutually consistent — `lag(i,j)` must equal
`offset(i) - offset(j)`, which nothing forces to be true if any offset is wrong.

### Transition handles

A crossfade needs footage from before the cut point. Rather than shortening the
timeline, the incoming segment reaches back `transition_sec` earlier in master
time and fades up over the outgoing one, exactly as a handle works in a normal
editor. Output time therefore stays a fixed shift of master time:

```
output_time = T - body_in + title_duration
```

which is why the audio bed stays a straight 1:1 map and cannot drift as
transitions are added or removed.

### Audio

Video and audio are assembled independently. Each segment clip is stripped of
its own audio; the audio track is built separately from a bed plus optional
override regions, so a cut between cameras never produces an audio
discontinuity.

Where an override is enabled the bed is genuinely replaced, not mixed on top:
the track is split into regions, and each region reaches `crossfade` seconds
into its neighbour with complementary linear fades. Because that is a convex
combination, the sum can never exceed the louder of the two sources.

The bed also runs under the title and credits, so the video neither opens nor
closes in silence.

## Measured results

Offsets, reference camera 2, all six pairs consistent to within 30 ms (one frame
at 30 fps):

| Camera | Offset | Duration | Coverage in master time |
|---|---|---|---|
| cam1 front left | 57.45 s | 125.11 s | −57.45 → +67.66 |
| cam2 front right | 0.00 s (ref) | 43.05 s | 0.00 → +43.05 |
| cam3 wide back | 50.02 s | 95.71 s | −50.02 → +45.69 |
| cam4 side angle | 39.63 s | 97.11 s | −39.63 → +57.48 |

All four cameras overlap for only **43.05 s**, which is below the 60 s minimum
final length — the cut cannot be built from the four-camera window alone. Two or
more cameras are live across **107.5 s**, from T = −50.02 to T = +57.48, and
that is the window the EDL uses.

## Known limitations

- **The source audio is already clipped.** Cameras 1 and 2 peak at 0.0 dBFS in
  the original recordings; the drums overloaded the camera microphones before
  the footage reached this pipeline, and that cannot be undone. `audio.gain` in
  the EDL keeps headroom so the encoder does not add further distortion.
- **Camera selection is manual.** The EDL is written by a person. There is no
  automatic shot-quality scoring, so the pipeline reduces the mechanical work of
  synchronising and assembling, not the editorial judgement.
- **One sync event.** Offsets are constant, which assumes no camera drifted or
  paused mid-recording. The pairwise check would expose a gross drift but not a
  slow one.
