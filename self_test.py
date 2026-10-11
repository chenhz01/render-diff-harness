#!/usr/bin/env python3
"""self_test battery for render-diff-harness.

Three coverage classes (ALIGNMENT-16 rule 97):
  functional    - core diff/manifest behavior works
  error paths   - corrupt inputs degrade to clean errors, not crashes
  extreme edges - empty / huge / degenerate inputs

Run:  python self_test.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import render_diff_harness as rdh  # noqa: E402

HERE = Path(__file__).parent
PY = sys.executable or "python"


def solid_png(path, w, h, rgb=(10, 20, 30), color_type=2):
    px = bytes(rgb) * (w * h)
    rdh.write_png(path, w, h, px, color_type=color_type)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)


# ------------------------------------------------------------- functional

class Functional(TempCase):
    def test_png_roundtrip_hash_stable(self):
        a, b = self.d / "a.png", self.d / "b.png"
        solid_png(a, 8, 6)
        solid_png(b, 8, 6)
        self.assertEqual(rdh.frame_hash(a), rdh.frame_hash(b))

    def test_hash_changes_when_pixels_change(self):
        a, b = self.d / "a.png", self.d / "b.png"
        solid_png(a, 8, 6, rgb=(1, 2, 3))
        solid_png(b, 8, 6, rgb=(1, 2, 4))
        self.assertNotEqual(rdh.frame_hash(a)["sha256"], rdh.frame_hash(b)["sha256"])

    def test_manifest_init_and_check_pass(self):
        frames = self.d / "frames"
        frames.mkdir()
        for i in range(3):
            solid_png(frames / f"f{i}.png", 16, 9)
        mpath = self.d / "baseline.json"
        rdh.init_manifest(frames, mpath)
        report = rdh.check_frames(frames, mpath)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["missing"] + report["extra"] + report["changed"], [])

    def test_check_fails_on_changed_frame(self):
        frames = self.d / "frames"
        frames.mkdir()
        solid_png(frames / "a.png", 16, 9, rgb=(0, 0, 0))
        mpath = self.d / "baseline.json"
        rdh.init_manifest(frames, mpath)
        solid_png(frames / "a.png", 16, 9, rgb=(200, 0, 0))
        report = rdh.check_frames(frames, mpath)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["changed"][0]["frame"], "a.png")

    def test_pixel_tolerance_absorbs_tiny_change(self):
        frames = self.d / "frames"
        frames.mkdir()
        w, h = 100, 100
        px = bytes([12]) * (w * h * 3)
        rdh.write_png(frames / "a.png", w, h, px)  # RGB: 3 bytes/pixel
        base_copy = self.d / "base_frames"
        mpath = self.d / "baseline.json"
        rdh.init_manifest(frames, mpath, copy_to=base_copy)
        px2 = bytearray(px)
        px2[0] = 200  # one pixel out of 10_000
        rdh.write_png(frames / "a.png", w, h, bytes(px2))
        report = rdh.check_frames(frames, mpath, max_pixel_diff_ratio=0.05,
                                  baseline_frames_dir=base_copy)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["changed"], [])

    def test_tolerance_without_baseline_frames_fails_closed(self):
        # Regression guard for the v0.1.0 bug where a changed frame was
        # compared against itself (ratio 0) and tolerance passed everything.
        frames = self.d / "frames"
        frames.mkdir()
        w, h = 50, 50
        rdh.write_png(frames / "a.png", w, h, bytes([12]) * (w * h * 3))
        mpath = self.d / "baseline.json"
        rdh.init_manifest(frames, mpath)  # no --copy-to
        rdh.write_png(frames / "a.png", w, h, bytes([250]) * (w * h * 3))
        report = rdh.check_frames(frames, mpath, max_pixel_diff_ratio=0.9)
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("fail-closed" in p for p in report["problems"]))

    def test_cli_end_to_end(self):
        frames = self.d / "frames"
        frames.mkdir()
        solid_png(frames / "a.png", 10, 10)
        base = self.d / "b.json"
        r0 = subprocess.run([PY, str(HERE / "render_diff_harness.py"), "init",
                             "--frames", str(frames), "--out", str(base)],
                            capture_output=True, text=True)
        self.assertEqual(r0.returncode, 0, r0.stderr)
        r1 = subprocess.run([PY, str(HERE / "render_diff_harness.py"), "check",
                             "--frames", str(frames), "--baseline", str(base)],
                            capture_output=True, text=True)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        solid_png(frames / "a.png", 10, 10, rgb=(9, 9, 9))
        r2 = subprocess.run([PY, str(HERE / "render_diff_harness.py"), "check",
                             "--frames", str(frames), "--baseline", str(base), "--json"],
                            capture_output=True, text=True)
        self.assertEqual(r2.returncode, 1)
        self.assertIn('"status": "fail"', r2.stdout)


# ------------------------------------------------------------ error paths

class ErrorPaths(TempCase):
    def test_corrupt_png_raises_clean_error(self):
        p = self.d / "bad.png"
        p.write_bytes(rdh.PNG_SIG + b"\x00" * 40)  # valid sig, garbage after
        with self.assertRaises(ValueError):
            rdh.frame_hash(p)

    def test_truncated_png_raises(self):
        p = self.d / "cut.png"
        solid_png(p, 8, 8)
        data = p.read_bytes()
        p.write_bytes(data[:len(data) // 2])
        with self.assertRaises(ValueError):
            rdh.frame_hash(p)

    def test_check_with_bad_json_baseline_is_error(self):
        frames = self.d / "frames"
        frames.mkdir()
        bad = self.d / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        report = rdh.check_frames(frames, bad)
        self.assertEqual(report["status"], "error")

    def test_check_with_missing_baseline_is_error(self):
        frames = self.d / "frames"
        frames.mkdir()
        report = rdh.check_frames(frames, self.d / "nope.json")
        self.assertEqual(report["status"], "error")

    def test_check_with_missing_frames_dir_is_error(self):
        base = self.d / "b.json"
        base.write_text('{"frames": {}}', encoding="utf-8")
        report = rdh.check_frames(self.d / "nope", base)
        self.assertEqual(report["status"], "error")


# ----------------------------------------------------------- extreme edges

class ExtremeEdges(TempCase):
    def test_palette_png_rejected_gracefully(self):
        p = self.d / "pal.png"
        blob = rdh.PNG_SIG
        ihdr = (8).to_bytes(4, "big") + (8).to_bytes(4, "big") + bytes([8, 3, 0, 0, 0])
        blob += bytes([0, 0, 0, 13]) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
        p.write_bytes(blob)
        with self.assertRaises(ValueError):
            rdh.frame_hash(p)

    def test_empty_dir_and_empty_manifest(self):
        frames = self.d / "frames"
        frames.mkdir()
        base = self.d / "b.json"
        rdh.init_manifest(frames, base)
        self.assertEqual(rdh.check_frames(frames, base)["status"], "pass")
        self.assertEqual(
            rdh.check_frames(frames, base, fail_on_empty=True)["status"], "fail")

    def test_oversize_dimensions_roundtrip(self):
        a = self.d / "wide.png"
        w, h = 2000, 2
        rdh.write_png(a, w, h, bytes(3 * w * h))
        img = rdh.read_png(a)
        self.assertEqual((img["width"], img["height"]), (w, h))

    def test_single_pixel_frame(self):
        a = self.d / "1x1.png"
        rdh.write_png(a, 1, 1, b"\x01\x02\x03")
        self.assertEqual(rdh.frame_hash(a)["width"], 1)

    def test_long_filename_frame(self):
        frames = self.d / "frames"
        frames.mkdir()
        name = "f" * 180 + ".png"
        solid_png(frames / name, 4, 4)
        base = self.d / "b.json"
        rdh.init_manifest(frames, base)
        self.assertEqual(rdh.check_frames(frames, base)["status"], "pass")

    def test_extra_frame_in_dir_fails_check(self):
        frames = self.d / "frames"
        frames.mkdir()
        solid_png(frames / "a.png", 4, 4)
        base = self.d / "b.json"
        rdh.init_manifest(frames, base)
        solid_png(frames / "surprise.png", 4, 4)
        report = rdh.check_frames(frames, base)
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["extra"], ["surprise.png"])

    def test_manifest_is_deterministic(self):
        frames = self.d / "frames"
        frames.mkdir()
        solid_png(frames / "a.png", 5, 5)
        b1, b2 = self.d / "b1.json", self.d / "b2.json"
        rdh.init_manifest(frames, b1)
        rdh.init_manifest(frames, b2)
        self.assertEqual(b1.read_text(encoding="utf-8"), b2.read_text(encoding="utf-8"))

    def test_init_copy_to_preserves_original_pixels(self):
        frames = self.d / "frames"
        frames.mkdir()
        solid_png(frames / "a.png", 8, 8, rgb=(1, 2, 3))
        copy = self.d / "copy"
        base = self.d / "b.json"
        rdh.init_manifest(frames, base, copy_to=copy)
        # overwrite the live frame after baseline; the copy must keep the old pixels
        solid_png(frames / "a.png", 8, 8, rgb=(250, 250, 250))
        self.assertEqual(rdh.frame_hash(copy / "a.png")["sha256"],
                         json.loads(base.read_text(encoding="utf-8"))["frames"]["a.png"]["sha256"])

    def test_report_summary_counts(self):
        frames = self.d / "frames"
        frames.mkdir()
        solid_png(frames / "a.png", 4, 4)
        solid_png(frames / "b.png", 4, 4)
        base = self.d / "b.json"
        rdh.init_manifest(frames, base)
        solid_png(frames / "b.png", 4, 4, rgb=(99, 99, 99))
        solid_png(frames / "c.png", 4, 4)  # extra
        (frames / "a.png").unlink()        # missing
        report = rdh.check_frames(frames, base)
        self.assertEqual(report["summary"], {"missing": 1, "extra": 1, "changed": 1})

    def test_tolerance_end_to_end_cli(self):
        frames = self.d / "frames"
        frames.mkdir()
        w, h = 60, 60
        rdh.write_png(frames / "a.png", w, h, bytes([7]) * (w * h * 3))
        base, copy = self.d / "b.json", self.d / "copy"
        r0 = subprocess.run([PY, str(HERE / "render_diff_harness.py"), "init",
                             "--frames", str(frames), "--out", str(base),
                             "--copy-to", str(copy)], capture_output=True, text=True)
        self.assertEqual(r0.returncode, 0, r0.stderr)
        rdh.write_png(frames / "a.png", w, h,
                      bytes([7]) * (w * h * 3 - 1) + bytes([8]))  # 1 pixel differs
        r1 = subprocess.run([PY, str(HERE / "render_diff_harness.py"), "check",
                             "--frames", str(frames), "--baseline", str(base),
                             "--baseline-frames", str(copy),
                             "--max-pixel-diff-ratio", "0.02", "--json"],
                            capture_output=True, text=True)
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)

    def test_perf_smoke_20_frames(self):
        frames = self.d / "frames"
        frames.mkdir()
        w, h = 320, 180
        px = bytes(3 * w * h)
        for i in range(20):
            rdh.write_png(frames / f"{i:03}.png", w, h, px)
        base = self.d / "b.json"
        t0 = time.perf_counter()
        rdh.init_manifest(frames, base)
        report = rdh.check_frames(frames, base)
        elapsed = time.perf_counter() - t0
        self.assertEqual(report["status"], "pass")
        self.assertLess(elapsed, 30.0)
        print(f"\n[perf] 20 frames {w}x{h}: init+check in {elapsed:.2f}s "
              f"(self_test measured)", file=sys.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
