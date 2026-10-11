# Changelog — render-diff-harness

All notable changes. Format: Keep a Changelog-ish, versions pre-1.0.

## v0.1.1 — 2026-10-11 (round-2 iteration: correctness + coverage)

Round-2 self-audit against ALIGNMENT-16.md found and fixed a real bug.

### Fixed (correctness)
- **Tolerance mode compared a changed frame against itself** (`--baseline-frames`
  absent): `pixel_diff_ratio` was always 0, so any tolerance value silently
  passed every regression. Tolerance now requires the true baseline pixels
  (`init --copy-to` + `check --baseline-frames`) and **fails closed** without
  them, with an explicit problem message.

### Added (tests +19 → +23, +21% coverage)
- `test_tolerance_without_baseline_frames_fails_closed` (regression guard for the bug above)
- `test_init_copy_to_preserves_original_pixels`
- `test_report_summary_counts`
- `test_tolerance_end_to_end_cli`

### Changed
- Report includes a `summary` object (missing/extra/changed counts).
- README: Quickstart updated to two-step baseline; Design Trade-offs documents fail-closed semantics; perf figure re-measured (~3.9 s).

## v0.1.0 — 2026-10-11

Initial draft: PNG pixel codec (8-bit gray/RGB/RGBA, filters 0-4, stdlib
only), frame-hash manifest (`init`), CI gate (`check`) with missing/extra/
changed detection, optional pixel-tolerance pass, `--fail-on-empty`, `--json`,
exit codes 0/1/2, and a 19-test self battery (functional / error paths /
extreme edges).
