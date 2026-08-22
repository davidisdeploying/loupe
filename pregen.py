#!/usr/bin/env python3
"""
pregen.py — one-time FULL-LIBRARY thumbnail backfill for loupe.

Scope: every asset in metadata.db EXCEPT the two protected work folders
(production/, long-video-elsewhere/) by path — null-safe (filepath is non-null,
so the LIKE filter can't drop rows the way a NOT(col=...) would). Glasses included.

Reuses gen_thumbs' thumb functions (so both video write-path fixes — cv2 imencode
+ ffmpeg .tmp.jpg — and the IMG_WORKERS=12 OOM cap come along for free) and writes
into the SHARED thumb cache. Skip-existing + idempotent: the ~24k candidate thumbs
already cached are reused, and a crash/reboot resumes without redoing work.

Images run first at IMG_WORKERS (12) — image DECODE is the memory-bound step on this
8 GB box; do NOT raise it. Then videos at VID_WORKERS (16, light single-frame extracts).

Usage:  python3 pregen.py [--limit N]   (--limit for a verification batch)
"""
import os
import sys
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.environ.get("DATA_ROOT")
# CODE vs DATA split: gen_thumbs.py (+ candidates.py) source now lives under pipeline/
# (PIPELINE_DIR); the thumb cache + metadata.db stay in V2 (the data dir). candidates.py
# resolves its data from $DATA_ROOT, else its OWN dir (now pipeline/) — so pin DATA_ROOT
# to V2 BEFORE importing gen_thumbs, or thumbs would be written under pipeline/. This is a
# short-lived child process, so the env set is not restored.
from loupe_common import V2, PIPELINE_DIR, EXCLUDE_SQL, VIDEO_EXT
os.environ["DATA_ROOT"] = V2
sys.path.insert(0, PIPELINE_DIR)
import gen_thumbs as G   # make_image_thumb / make_video_thumb / thumb_path / IMG_WORKERS / VID_WORKERS

META = os.path.join(V2, "metadata.db")
LOG = os.path.join(DATA_ROOT or HERE, "pregen.log")
IMG_WORKERS, VID_WORKERS = G.IMG_WORKERS, G.VID_WORKERS


def log(m):
    line = f"[{time.strftime('%H:%M:%S')}] {m}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    con = sqlite3.connect(f"file:{META}?mode=ro", uri=True)
    rows = con.execute(
        f"SELECT id, filepath, extension, duration_seconds FROM assets WHERE {EXCLUDE_SQL}"
    ).fetchall()
    con.close()

    # skip-existing (idempotent / resumable)
    todo = [(i, fp, (ext or "").upper(), dur)
            for (i, fp, ext, dur) in rows if not os.path.exists(G.thumb_path(i))]
    if limit:
        todo = todo[:limit]
    imgs = [(i, fp) for (i, fp, ext, dur) in todo if ext not in VIDEO_EXT]
    vids = [(i, fp, dur) for (i, fp, ext, dur) in todo if ext in VIDEO_EXT]
    log(f"pregen start: {len(rows)} in-scope, {len(todo)} missing "
        f"({len(imgs)} img @ {IMG_WORKERS}-way, {len(vids)} vid @ {VID_WORKERS}-way)"
        + (f"  [LIMIT {limit}]" if limit else ""))

    ok = [0]
    fail = [0]
    t0 = time.time()

    def do_img(t):
        i, fp = t
        try:
            if os.path.exists(G.thumb_path(i)):
                return
            G.make_image_thumb(fp, i)
            ok[0] += 1
        except Exception as e:
            fail[0] += 1
            if fail[0] <= 20:
                log(f"  img fail id={i}: {type(e).__name__}: {str(e)[:80]}")

    def do_vid(t):
        i, fp, dur = t
        try:
            if os.path.exists(G.thumb_path(i)):
                return
            G.make_video_thumb(fp, i, dur)
            ok[0] += 1
        except Exception as e:
            fail[0] += 1
            if fail[0] <= 40:
                log(f"  vid fail id={i}: {type(e).__name__}: {str(e)[:80]}")

    def run(pool, fn, items, tag):
        if not items:
            return
        with ThreadPoolExecutor(max_workers=pool) as ex:
            for n, _ in enumerate(ex.map(fn, items), 1):
                if n % 200 == 0:
                    log(f"  {tag} {n}/{len(items)} ok={ok[0]} fail={fail[0]} "
                        f"({n / (time.time() - t0):.1f}/s)")

    run(IMG_WORKERS, do_img, imgs, "img")   # memory-bound: capped at 12
    run(VID_WORKERS, do_vid, vids, "vid")   # light: 16-way
    log(f"pregen DONE: ok={ok[0]} fail={fail[0]} elapsed={time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
