#!/usr/bin/env python3
"""render-diff-harness: deterministic regression harness for rendered video frames.

Hashes *decoded pixel data* (not file bytes) of PNG frames so that metadata
churn never produces false positives. Designed as a CI gate: build frames from
the same input twice, diff the hashes, fail the build on any unexpected change.

Pure Python standard library. See self_test.py for the test battery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import sys
import zlib
from pathlib import Path

MANIFEST_VERSION = 1
ALGORITHM = "png-pixel-sha256"
PNG_SIG = b"\x89PNG\r\n\x1a\n"
# color type -> channels (8-bit depth only)
COLOR_CHANNELS = {0: 1, 2: 3, 6: 4}


# ---------------------------------------------------------------- PNG codec

def _read_chunks(data: bytes):
    if not data.startswith(PNG_SIG):
        raise ValueError("not a PNG file (bad signature)")
    pos = 8
    n = len(data)
    while pos + 8 <= n:
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        cdata = data[pos + 8:pos + 8 + length]
        if pos + 12 + length > n:
            raise ValueError("truncated PNG chunk")
        yield ctype, cdata
        pos += 12 + length
        if ctype == b"IEND":
            return
    raise ValueError("PNG stream ended without IEND")


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytes:
    stride = width * bpp
    if len(raw) != (stride + 1) * height:
        raise ValueError("IDAT payload size mismatch")
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    for row in range(height):
        f = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if f == 0:
            pass
        elif f == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:  # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:  # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 0xFF
        else:
            raise ValueError(f"unsupported PNG filter type {f}")
        out[row * stride:(row + 1) * stride] = line
        prev = line
    return bytes(out)


def read_png(path):
    """Decode an 8-bit non-interlaced PNG (gray/RGB/RGBA).

    Returns dict: {width, height, channels, pixels}. Raises ValueError with a
    human-readable message for every unsupported/corrupt input.
    """
    data = Path(path).read_bytes()
    ihdr = None
    idat = bytearray()
    for ctype, cdata in _read_chunks(data):
        if ctype == b"IHDR":
            if len(cdata) != 13:
                raise ValueError("IHDR chunk has wrong length")
            ihdr = struct.unpack(">IIBBBBB", cdata)
        elif ctype == b"IDAT":
            idat.extend(cdata)
        elif ctype == b"IEND":
            break
    if ihdr is None:
        raise ValueError("PNG has no IHDR chunk")
    width, height, depth, ctype, comp, filt, interlace = ihdr
    if depth != 8:
        raise ValueError(f"unsupported bit depth {depth} (only 8)")
    if ctype not in COLOR_CHANNELS:
        raise ValueError(f"unsupported color type {ctype} (only 0/2/6)")
    if comp != 0 or filt != 0:
        raise ValueError("unsupported compression/filter method")
    if interlace != 0:
        raise ValueError("interlaced PNG not supported")
    if width == 0 or height == 0:
        raise ValueError("PNG has zero-width or zero-height image")
    if not idat:
        raise ValueError("PNG has no IDAT data")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ValueError(f"corrupt zlib stream in IDAT: {exc}") from exc
    bpp = COLOR_CHANNELS[ctype]
    pixels = _unfilter(raw, width, height, bpp)
    return {"width": width, "height": height, "channels": bpp, "pixels": pixels}


def write_png(path, width: int, height: int, pixels: bytes, color_type: int = 2) -> None:
    """Write an 8-bit PNG (filter 0 rows). Used for fixtures and demos."""
    channels = COLOR_CHANNELS[color_type]
    stride = width * channels
    if len(pixels) != stride * height:
        raise ValueError(f"pixel buffer is {len(pixels)} bytes, expected {stride * height}")
    raw = b"".join(b"\x00" + pixels[r * stride:(r + 1) * stride] for r in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)

    def chunk(ctype: bytes, cdata: bytes) -> bytes:
        return (struct.pack(">I", len(cdata)) + ctype + cdata
                + struct.pack(">I", zlib.crc32(ctype + cdata) & 0xFFFFFFFF))

    blob = PNG_SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    Path(path).write_bytes(blob)


# ------------------------------------------------------------------ hashing

def frame_hash(path) -> dict:
    """Content hash over decoded pixels + geometry (metadata-independent)."""
    img = read_png(path)
    head = f"{img['width']}x{img['height']}:{img['channels']}:".encode()
    digest = hashlib.sha256(head + img["pixels"]).hexdigest()
    return {"width": img["width"], "height": img["height"],
            "channels": img["channels"], "sha256": digest}


def pixel_diff_ratio(path_a, path_b, tol: int = 0) -> float:
    """Fraction of pixels whose any-channel delta exceeds `tol`. 1.0 if geometry differs."""
    a, b = read_png(path_a), read_png(path_b)
    if (a["width"], a["height"]) != (b["width"], b["height"]):
        return 1.0
    ca, cb = a["channels"], b["channels"]
    if ca != cb:
        return 1.0
    pa, pb = a["pixels"], b["pixels"]
    total = a["width"] * a["height"]
    if total == 0:
        return 0.0
    diff = 0
    for i in range(0, len(pa), ca):
        for c in range(ca):
            if abs(pa[i + c] - pb[i + c]) > tol:
                diff += 1
                break
    return diff / total


# ----------------------------------------------------------------- manifest

def init_manifest(frames_dir, out_path, copy_to=None) -> dict:
    frames = {}
    frames_dir = Path(frames_dir)
    copy_dir = Path(copy_to) if copy_to else None
    if copy_dir is not None:
        copy_dir.mkdir(parents=True, exist_ok=True)
    for p in sorted(frames_dir.iterdir()):
        if p.is_file() and p.suffix.lower() == ".png":
            frames[p.name] = frame_hash(p)
            if copy_dir is not None:
                shutil.copy2(p, copy_dir / p.name)  # keep true baseline pixels
    manifest = {"manifest_version": MANIFEST_VERSION, "algorithm": ALGORITHM, "frames": frames}
    Path(out_path).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def check_frames(frames_dir, baseline_path, max_pixel_diff_ratio: float = 0.0,
                 fail_on_empty: bool = False, baseline_frames_dir=None) -> dict:
    """Compare a frames directory against a baseline manifest.

    Returns report dict; caller decides exit code via report["status"]:
    "pass" | "fail" | "error".

    Tolerance mode (`max_pixel_diff_ratio > 0`) needs the *original* baseline
    pixels: pass `baseline_frames_dir` (the copy made by `init --copy-to`).
    Without it the tolerance pass fails closed — a changed frame stays changed
    (never silently compared against itself).
    """
    try:
        baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "error", "problems": [f"baseline not found: {baseline_path}"]}
    except json.JSONDecodeError as exc:
        return {"status": "error", "problems": [f"baseline is not valid JSON: {exc}"]}
    if not isinstance(baseline, dict) or "frames" not in baseline:
        return {"status": "error", "problems": ["baseline manifest missing 'frames' key"]}

    frames_dir = Path(frames_dir)
    if not frames_dir.is_dir():
        return {"status": "error", "problems": [f"frames dir not found: {frames_dir}"]}
    present = {p.name for p in frames_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png"}

    report = {"status": "pass", "missing": [], "extra": [], "changed": [], "problems": [],
              "summary": {"missing": 0, "extra": 0, "changed": 0}}
    base_frames = baseline.get("frames") or {}
    if not base_frames and not present and fail_on_empty:
        report["status"] = "fail"
        report["problems"].append("no baseline frames and no frames on disk (use --fail-on-empty to enforce)")
        return report

    for name, entry in sorted(base_frames.items()):
        if name not in present:
            report["missing"].append(name)
            continue
        try:
            current = frame_hash(frames_dir / name)
        except ValueError as exc:
            report["problems"].append(f"{name}: unreadable frame ({exc})")
            report["status"] = "error"
            continue
        if current["sha256"] != entry.get("sha256"):
            report["changed"].append({"frame": name, "expected": entry.get("sha256"),
                                      "actual": current["sha256"], "pixel_diff_ratio": None})
    for name in sorted(present - set(base_frames)):
        report["extra"].append(name)

    # Tolerance pass: a changed frame may still pass if the pixel difference
    # against the *original baseline pixels* stays within the configured ratio.
    # Without baseline pixels we fail closed (no silent self-comparison).
    if report["changed"] and max_pixel_diff_ratio > 0.0:
        if not baseline_frames_dir:
            report["problems"].append(
                "tolerance requested but no --baseline-frames given; "
                "changed frames stay failed (fail-closed)")
        else:
            still_changed = []
            for item in report["changed"]:
                base_p = Path(baseline_frames_dir) / item["frame"]
                if not base_p.is_file():
                    still_changed.append(item)
                    continue
                try:
                    ratio = pixel_diff_ratio(base_p, Path(frames_dir) / item["frame"])
                except ValueError:
                    ratio = 1.0
                item["pixel_diff_ratio"] = round(ratio, 6)
                if ratio > max_pixel_diff_ratio:
                    still_changed.append(item)
            report["changed"] = still_changed

    report["summary"] = {"missing": len(report["missing"]), "extra": len(report["extra"]),
                         "changed": len(report["changed"])}
    if report["missing"] or report["extra"] or report["changed"] or report["status"] == "error":
        report["status"] = "fail" if report["status"] != "error" else "error"
    return report


# ---------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="render-diff-harness",
        description="Deterministic frame-hash regression gate for rendered video pipelines.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_hash = sub.add_parser("hash", help="print pixel hash of each PNG in a dir")
    ap_hash.add_argument("--frames", required=True)

    ap_init = sub.add_parser("init", help="create a baseline manifest from a frames dir")
    ap_init.add_argument("--frames", required=True)
    ap_init.add_argument("--out", required=True, help="baseline manifest JSON path")
    ap_init.add_argument("--copy-to", default=None,
                         help="also copy baseline PNGs here (needed for tolerance mode)")

    ap_chk = sub.add_parser("check", help="diff a frames dir against a baseline (CI gate)")
    ap_chk.add_argument("--frames", required=True)
    ap_chk.add_argument("--baseline", required=True)
    ap_chk.add_argument("--max-pixel-diff-ratio", type=float, default=0.0,
                        help="tolerance for changed frames, 0.0-1.0 (default: exact)")
    ap_chk.add_argument("--baseline-frames", default=None,
                        help="dir of baseline PNGs (from init --copy-to); "
                             "required for tolerance mode")
    ap_chk.add_argument("--fail-on-empty", action="store_true",
                        help="fail when both baseline and frames dir are empty")
    ap_chk.add_argument("--json", action="store_true", help="machine-readable report")

    args = ap.parse_args(argv)

    if args.cmd == "hash":
        d = Path(args.frames)
        if not d.is_dir():
            print(f"error: frames dir not found: {d}", file=sys.stderr)
            return 2
        files = sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() == ".png")
        if not files:
            print("no PNG frames found", file=sys.stderr)
            return 2
        for p in files:
            try:
                print(f"{frame_hash(p)['sha256']}  {p.name}")
            except ValueError as exc:
                print(f"{exc}  {p.name}", file=sys.stderr)
                return 2
        return 0

    if args.cmd == "init":
        d = Path(args.frames)
        if not d.is_dir():
            print(f"error: frames dir not found: {d}", file=sys.stderr)
            return 2
        manifest = init_manifest(d, args.out, copy_to=args.copy_to)
        print(f"wrote {args.out} ({len(manifest['frames'])} frames)"
              + (f" + baseline copy in {args.copy_to}" if args.copy_to else ""))
        return 0

    if args.cmd == "check":
        report = check_frames(args.frames, args.baseline,
                              max_pixel_diff_ratio=args.max_pixel_diff_ratio,
                              fail_on_empty=args.fail_on_empty,
                              baseline_frames_dir=args.baseline_frames)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            for problem in report["problems"]:
                print(f"problem: {problem}", file=sys.stderr)
            for name in report["missing"]:
                print(f"missing frame: {name}", file=sys.stderr)
            for name in report["extra"]:
                print(f"extra frame: {name}", file=sys.stderr)
            for item in report["changed"]:
                ratio = item.get("pixel_diff_ratio")
                tail = f" pixel_diff_ratio={ratio}" if ratio is not None else ""
                print(f"changed frame: {item['frame']}{tail}", file=sys.stderr)
            print(f"render-diff: {report['status']}")
        return {"pass": 0, "fail": 1}.get(report["status"], 2)
    return 2


if __name__ == "__main__":
    sys.exit(main())
