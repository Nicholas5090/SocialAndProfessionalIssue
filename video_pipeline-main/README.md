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

**spacific video** This pipeline is only suitable for target video.

---

## Requirements

Python 3.11+ and the following Python packages (install via the included
`requirements.txt`):

```bash
py -3 -m pip install -r requirements.txt
```

System dependency — FFmpeg

This pipeline uses FFmpeg for audio/video decoding and rendering. FFmpeg is a
separate system program (not a Python package) and must be installed and
available on the `PATH` before running the scripts.

Install FFmpeg on common platforms:

- macOS (Homebrew):

```bash
brew install ffmpeg
```

- Ubuntu / Debian:

```bash
sudo apt update
sudo apt install ffmpeg
```

- Fedora / CentOS / RHEL (dnf):

```bash
sudo dnf install ffmpeg
```

- Windows:

  1. Download a static build from https://ffmpeg.org/download.html or
     https://www.gyan.dev/ffmpeg/builds/.
  2. Unzip and add the `bin` directory to your PATH (system environment
     variable) so `ffmpeg` and `ffprobe` are accessible from the command line.

Verify installation:

```bash
ffmpeg -version
ffprobe -version
```

If you prefer pinned Python package versions, update `requirements.txt` with
version specifiers (for example `numpy>=1.26`) and the pip command above will
respect them.

---

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
