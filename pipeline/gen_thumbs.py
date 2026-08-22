#!/usr/bin/env python3
"""
gen_thumbs.py — fill in missing thumbnails for every review candidate.

SKIP-EXISTING and RESUMABLE: re-running never re-pays the ~36s/open cost for a
thumb already on disk, so a NAS stall is free to retry. Images via PIL/pillow-heif
(<=400px JPEG); videos via ffmpeg midpoint frame. High concurrency (latency-bound).

Writes ONLY into culling/contactsheets/thumbs/. Read-only on the library/DB.
Order: smaller/cheaper sets first so they become reviewable soonest.
"""

import io
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np  # noqa: F401  (kept parallel to ingest deps)
from PIL import Image, UnidentifiedImageError, ImageOps
import pillow_heif
pillow_heif.register_heif_opener()

try:
    import rawpy
    RAW_SUPPORTED = True
except ImportError:
    RAW_SUPPORTED = False

import candidates as C

THUMBS = C.THUMBS
os.makedirs(THUMBS, exist_ok=True)
def _envint(name, default):
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default
def _ram_gb():
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3)
    except Exception:
        return 8.0
_CORES = os.cpu_count() or 4
_RAM_GB = _ram_gb()
WORKERS = _envint("THUMB_WORKERS", 200)   # images: latency-bound reads, safe to fan out wide
# Image DECODE is memory-bound: 48 MP HEICs ~146 MB each when decoded; a wide decode
# OOM-kills (exit 137) on a small box. OOM guard stays conservative on ≤12 GB boxes
# (→ 12, ≈1.75 GB peak); larger RAM scales up, capped. Override via the IMG_WORKERS env.
IMG_WORKERS = _envint("IMG_WORKERS", 12 if _RAM_GB < 12 else min(48, int(_RAM_GB)))
VID_WORKERS = _envint("VID_WORKERS", max(8, min(_CORES * 4, 64)))  # ffmpeg process per video
LONG_EDGE = _envint("THUMB_LONG_EDGE", 400)
ORDER = ["A2a", "B2", "B5", "A2b", "B3"]   # B4 already fully thumbed
LOGP = os.path.join(C.CULL, "contactsheets", "_genthumbs.log")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOGP, "a") as f:
        f.write(line + "\n")


def thumb_path(idv):
    return os.path.join(THUMBS, f"{idv}.jpg")


def write_atomic(idv, pil_img):
    tmp = thumb_path(idv) + ".tmp"
    pil_img.save(tmp, "JPEG", quality=80)
    os.replace(tmp, thumb_path(idv))


def _load_raw_image(fp):
    """Decode a camera raw file (CR2/CR3/DNG/...) via rawpy. Prefers the
    embedded JPEG preview for speed; falls back to a full raw postprocess."""
    if not RAW_SUPPORTED:
        raise RuntimeError("rawpy not installed -- cannot decode raw file")
    with rawpy.imread(fp) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                return Image.open(io.BytesIO(thumb.data))
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            pass
        rgb = raw.postprocess()
    return Image.fromarray(rgb)


def make_image_thumb(fp, idv):
    with open(fp, "rb") as f:
        data = f.read()
    try:
        im = Image.open(io.BytesIO(data))
    except UnidentifiedImageError:
        im = _load_raw_image(fp)
    im.draft("RGB", (LONG_EDGE, LONG_EDGE))
    im = ImageOps.exif_transpose(im) or im   # same orientation fix as server.build_preview
    im = im.convert("RGB")
    im.thumbnail((LONG_EDGE, LONG_EDGE))
    write_atomic(idv, im)


def make_video_thumb(fp, idv, dur):
    ss = max(0.0, (float(dur) / 2) if dur else 1.0)
    out = thumb_path(idv)
    tmp = out + ".tmp.jpg"   # real .jpg ext: ffmpeg picks format from extension; ".tmp" fails detection
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-ss", f"{ss:.3f}", "-i", fp,
         "-frames:v", "1", "-vf", f"scale={LONG_EDGE}:-1", "-q:v", "4", tmp],
        capture_output=True, text=True, timeout=180)
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, out)
        return
    # cv2 fallback
    if os.path.exists(tmp):
        os.remove(tmp)
    import cv2
    cap = cv2.VideoCapture(fp)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if n > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, n // 2)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise RuntimeError(f"ffmpeg+cv2 both failed: {(r.stderr or '')[-120:]}")
    h, w = frame.shape[:2]
    s = LONG_EDGE / max(w, h)
    if s < 1:
        frame = cv2.resize(frame, (max(1, int(w * s)), max(1, int(h * s))))
    ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok2:
        raise RuntimeError("cv2 imencode failed")
    tmp = out + ".tmp"
    with open(tmp, "wb") as fo:
        fo.write(buf.tobytes())
    os.replace(tmp, out)


def main():
    _, _, by_id = C.load_all()
    prio = {k: i for i, k in enumerate(ORDER)}
    worklist = [it for idv, it in by_id.items() if not os.path.exists(thumb_path(idv))]
    worklist.sort(key=lambda it: min((prio.get(r, 99) for r in it["rules"]), default=99))
    log(f"gen_thumbs start: {len(worklist)} missing thumbs to make "
        f"(skip-existing across {len(by_id)} unique candidates)")

    def work(it):
        idv = it["id"]
        try:
            if it["is_video"]:
                make_video_thumb(it["fpath"], idv, it["dur"])
            else:
                make_image_thumb(it["fpath"], idv)
            return idv, None
        except Exception as e:
            return idv, f"{type(e).__name__}: {e}"

    imgs = [it for it in worklist if not it["is_video"]]
    vids = [it for it in worklist if it["is_video"]]
    log(f"  {len(imgs)} images @ {IMG_WORKERS}-way, then {len(vids)} videos @ {VID_WORKERS}-way")
    ok = [0]
    fails = []
    t0 = time.time()

    def run(pool, lst, tag):
        if not lst:
            return
        with ThreadPoolExecutor(max_workers=pool) as ex:
            for n, (idv, err) in enumerate(ex.map(work, lst), 1):
                if err:
                    fails.append((idv, err))
                else:
                    ok[0] += 1
                if n % 100 == 0:
                    log(f"  {tag} {n}/{len(lst)} ok={ok[0]} fail={len(fails)} "
                        f"({n/(time.time()-t0):.1f}/s)")

    run(IMG_WORKERS, imgs, "img")   # fast, reliable first
    run(VID_WORKERS, vids, "vid")   # ffmpeg-per-file, modest concurrency
    ok = ok[0]
    log(f"gen_thumbs DONE: ok={ok} fail={len(fails)} elapsed={time.time()-t0:.0f}s")
    for idv, err in fails[:40]:
        log(f"  FAIL id={idv}: {err}")


if __name__ == "__main__":
    main()
