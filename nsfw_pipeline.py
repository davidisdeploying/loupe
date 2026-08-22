#!/usr/bin/env python3
"""
nsfw_pipeline.py — STAGE 1 of the optional on-device NSFW/nudity enrichment (headless
scan only; the /setup card, suppression surfaces, and opt-in gating are Stages 2-3 and
touch no server/UI code here). Second portable on-host enrichment after faces. Writes
ONLY ~/loupe/nsfw.db.

INPUT: the LOCAL thumb cache (culling/contactsheets/thumbs/<id>.jpg) — NOT originals over
CIFS. A 91k pass that open()s NAS originals (~36s/open) is untenable; thumb-resolution
inference is an accepted calibration variable (see Stage-3 calibration). NOTE: this
DIVERGES from faces_pipeline.py, which reads originals — deliberate, per the NAS-latency
constraint. metadata.db opened mode=ro. Resumable: assets already in `processed` are
skipped; assets with no thumb / no detection are STILL recorded (max_score 0) so resume
is clean. Images-only (video is a later fast-follow).

SIGNAL: stores the RAW per-asset MAX score over a sensitive-class set (+ the triggering
class) — NEVER a boolean. The flagged-set is derived at READ time from a tunable
threshold, so calibration can re-threshold over nsfw.db WITHOUT re-scanning.

MODEL: NudeNet (nudenet 3.4.2, bundled 320n.onnx, MIT). Inference fully on-device
(onnxruntime CPU); no network, nothing leaves the machine.

Usage:  nsfw_pipeline.py --all     (full library image pass; resumable)
Options: --commit-every N (default 25)
"""
import argparse, os, sqlite3, sys, time

# Leave one core for loupe + the backup; host-aware, overridable via OMP_NUM_THREADS env.
os.environ.setdefault("OMP_NUM_THREADS", str(max(1, (os.cpu_count() or 4) - 1)))

from loupe_common import V2, APP_DATA, METADATA_DB, EXCLUDE_SQL, VIDEO_EXT, ro
NSFW_DB = os.path.join(APP_DATA, "nsfw.db")
THUMBS = os.path.join(V2, "culling", "contactsheets", "thumbs")   # same cache pregen fills

# NudeNet detector labels reduced to the unambiguous nudity set; the per-asset signal is
# the MAX score over these. Storing raw max+class lets calibration re-threshold without a
# re-scan; the class SET itself is a documented calibration variable for the later pass.
SENSITIVE_CLASSES = {
    "FEMALE_GENITALIA_EXPOSED", "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED", "BUTTOCKS_EXPOSED", "ANUS_EXPOSED",
}


def init_nsfw_db():
    db = sqlite3.connect(NSFW_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    /* resume bookkeeping: every visited asset lands here, incl. no-thumb / no-detection */
    CREATE TABLE IF NOT EXISTS processed(
        asset_id INTEGER PRIMARY KEY,
        processed_at INTEGER NOT NULL);
    /* RAW per-asset signal — the flagged-set is derived at a tunable threshold at read
       time; this pipeline writes NO boolean. */
    CREATE TABLE IF NOT EXISTS scores(
        asset_id INTEGER PRIMARY KEY,
        max_score REAL,
        top_class TEXT);
    """)
    db.commit()
    return db


def load_detector():
    from nudenet import NudeDetector
    return NudeDetector()                 # auto-loads the bundled 320n.onnx (on-device)


def thumb_path(idv):
    return os.path.join(THUMBS, f"{idv}.jpg")


def asset_list(processed):
    conn = ro(METADATA_DB)
    # VIDEO_EXT is a frozenset (loupe_common) — bind via placeholders, NEVER interpolate the
    # container repr into SQL (mirrors the FIXED faces_pipeline form / server.py:550).
    # sorted() yields a list of upper-case exts, matching upper(extension).
    _vq = ",".join("?" * len(VIDEO_EXT))
    rows = conn.execute(
        f"SELECT id, extension FROM assets "
        f"WHERE upper(extension) NOT IN ({_vq}) AND {EXCLUDE_SQL} "
        f"ORDER BY id", sorted(VIDEO_EXT)).fetchall()
    conn.close()
    out = []
    for r in rows:
        if r["id"] in processed:
            continue
        if (r["extension"] or "").upper() in VIDEO_EXT:
            continue                       # images only this phase
        out.append(r["id"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--commit-every", type=int, default=25)
    args = ap.parse_args()

    db = init_nsfw_db()
    processed = {r[0] for r in db.execute("SELECT asset_id FROM processed")}
    todo = asset_list(processed)
    print(f"[nsfw] todo={len(todo)} (already processed={len(processed)})", flush=True)

    det = load_detector()
    print("[nsfw] model ready (nudenet 320n, on-device)", flush=True)

    t_start = time.time()
    done = 0
    hi = 0          # local debug tally at a nominal 0.5 — NOT persisted (no boolean stored)
    for idv in todo:
        max_score = 0.0
        top_class = None
        tp = thumb_path(idv)
        try:
            if os.path.exists(tp):
                for d in det.detect(tp):              # [{class, score, box}, ...]
                    if d["class"] in SENSITIVE_CLASSES and d["score"] > max_score:
                        max_score = float(d["score"])
                        top_class = d["class"]
        except Exception as e:
            print(f"[nsfw] WARN id={idv}: {type(e).__name__}: {e}", flush=True)
        db.execute("INSERT OR REPLACE INTO scores(asset_id,max_score,top_class) "
                   "VALUES(?,?,?)", (idv, max_score, top_class))
        db.execute("INSERT OR REPLACE INTO processed(asset_id,processed_at) "
                   "VALUES(?,?)", (idv, int(time.time())))
        if max_score >= 0.5:
            hi += 1
        done += 1
        if done % args.commit_every == 0:
            db.commit()
            rate = (time.time() - t_start) / done
            print(f"[nsfw] {done}/{len(todo)} assets · {hi} >=.5 · "
                  f"{rate:.2f}s/asset · ETA {rate*(len(todo)-done)/3600:.1f}h", flush=True)
    db.commit()
    elapsed = time.time() - t_start
    print(f"[nsfw] DONE {done} assets · {hi} >=.5 · "
          f"{elapsed:.0f}s · {elapsed/max(done,1):.2f}s/asset", flush=True)
    db.close()


if __name__ == "__main__":
    main()
