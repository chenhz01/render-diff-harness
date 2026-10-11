# render-diff-harness

Deterministic regression gate for rendered video frames. Point it at two
renders of the same input; it hashes decoded pixel data, diffs against a
baseline manifest, and fails your CI on any unexpected frame change. If you
ship "HTML-to-video" pipelines, this is the missing `pytest --snapshot`.

**Confidence: High (>=85%)** that exact-hash frame diffing catches real render
regressions; the tolerance mode is **Medium (60-79%)** — see Limits.

## Problem

Deterministic renderers (input -> MP4 as a pure function) make video output
*testable* — yet almost nobody tests it. The overlooked angle of the
agent-first rendering wave is the B-side: determinism means the result is
diffable, and "video CI" is nearly empty ground. Ad-hoc attempts hash the
video *file*, which breaks on encoder metadata churn; or eyeball a few
frames, which misses off-by-one content drift in frame 4,201.

This tool hashes the *decoded pixels* of each frame, so re-muxing or
metadata edits never false-positive, while a single changed pixel fails.

## How it works

1. Render your scene twice (or render once, commit the baseline).
2. `init` walks a frames directory, decodes every 8-bit PNG (pure stdlib
   zlib + unfiltering), and writes a manifest of SHA-256 hashes over
   `WxH:channels:` + raw pixel bytes.
3. `check` re-hashes the frames under test and compares:
   - `missing` / `extra` frames, `changed` frames;
   - optional `--max-pixel-diff-ratio` absorbs cosmetic drift (e.g. 0.05 =
     up to 5% of pixels may differ);
   - exit codes: `0` pass, `1` fail, `2` harness error — CI-gate ready.

## Quickstart (~2 min)

```bash
# 1. Baseline your current render; keep a copy of the baseline pixels
#    (needed for tolerance mode) (~10 s for 100 frames)
python render_diff_harness.py init --frames out/frames_v1 --out baseline.json \
    --copy-to out/baseline_frames

# 2. After any pipeline change, gate it (~10 s)
python render_diff_harness.py check --frames out/frames_v2 --baseline baseline.json

# 3. CI step (fails the build on regression)
python render_diff_harness.py check --frames "$FRAMES" --baseline baseline.json \
    --baseline-frames out/baseline_frames --max-pixel-diff-ratio 0.0 --fail-on-empty
```

## Why not X

| Alternative | Why not here |
|---|---|
| Hash the whole MP4/video file | Encoder/muxer metadata changes break it; per-frame attribution impossible; needs ffmpeg. |
| ffmpeg `signalstats`/SSIM per frame | Powerful but heavy dependency, slow, tuned for perceptual similarity — wrong tool when you promised *determinism*. |
| Percy/Playwright screenshot testing | Built for web-page screenshots, not frame sequences; per-frame manifests + extra/missing frame detection are the video-shaped gap. |
| Custom `sha256sum frames/*.png` | Hashes file bytes: PNG re-encode or metadata strip false-positives; no missing/extra detection, no tolerance mode, no report. |

## Minimal Fallback (PLAN B)

No Python beyond 3.8? Everything here works as:

```bash
# Crude fallback: file-byte hashes (accepts metadata false-positives)
sha256sum frames/*.png | sha256sum -c frames.sha256
```

You lose metadata immunity, missing/extra detection and tolerance, but you
keep a gate. The tool degrades to exactly this behavior with
`--max-pixel-diff-ratio 0` on identical-encoder output.

## Design Trade-offs

Priority order when rules conflict: **correctness > CI usability > speed**.

- Pixel decode instead of file bytes: slower, but metadata churn is the #1
  false-positive source — taken deliberately.
- Exact hash by default, tolerance opt-in: a silent `0.01` default would
  teach teams to ignore diffs (safety > convenience).
- Tolerance mode **fails closed**: without `--baseline-frames` (the copy
  made by `init --copy-to`), a changed frame stays changed — it is never
  silently compared against itself.
- PNG-only input (8-bit, non-interlaced): every real deterministic renderer
  can emit PNG; BMP/EXR support would triple the codec surface for <5%
  adoption [estimated].

## Limits & Second-order Effects

- **Hash-green is not human-green.** Widespread adoption of exact hashes
  makes teams stop *watching* renders; a renderer bug that deterministically
  produces wrong-but-stable output (e.g. wrong font baked in) passes forever.
  Keep one human spot-check per release.
- Tolerance mode can mask real regressions on busy frames; use exact mode
  for hero content.
- Baseline files are trust anchors: a poisoned baseline makes regressions
  "pass". Commit baselines through review like code.

## FAQ

**Q: When NOT to use this tool?**
A: When your renderer is *not* deterministic. If re-rendering the same input
produces legitimately different frames (live footage, GPU nondeterminism,
AI-generated frames), exact hashing will cry wolf on every run — use SSIM
tools or acceptance sampling instead.

**Q: Does it decode H.264 directly?**
A: No. Extract PNGs (ffmpeg can, one command) and point the tool there.

**Q: Why pure stdlib?**
A: CI runners install nothing; zero-dependency = the gate itself never
flakes on dependency drift.

**Q: How fast?**
A: Self-test measured: 20 frames 320x180 init+check in ~3.9 s including
startup (`python self_test.py -v` reproduces). Scale is linear in total
pixels [estimated].

## [Core Assumptions]

1. Your rendering pipeline is (or should be) deterministic — same input,
   same pixels — otherwise exact hashing is meaningless.
2. Frames can be materialized as individual PNG files between pipeline
   stages.
3. Frames are 8-bit gray/RGB/RGBA PNGs; 16-bit/HDR/palette PNGs are
   explicitly rejected with a readable error rather than silently hashed.
4. Baseline manifests are versioned in VCS and reviewed like code.

---
Contact: hcac4735@agent.qq.com · License: MIT · Data/claims as of 2026-10-11.
