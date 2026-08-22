#!/usr/bin/env python3
"""stage2b_import.py — append video-face embeddings into the live faces.db.

APPEND-ONLY to `faces` + `processed`. Never touches persons/assignments/rejections.
Idempotent: an asset already present in `processed` is skipped whole (faces has no
natural unique key, so a re-run must not double-insert). Row shape mirrors
faces_pipeline.py exactly (bbox_json rounds coords to 1dp, no `t` field — stills
rows carry none). Embedding stored as the RAW 2048-byte f32x512 BLOB verbatim.

Usage:
    stage2b_import.py --dry-run     # read shards, validate, report; NO writes
    stage2b_import.py               # real append into faces.db
"""
import argparse
import glob
import json
import os
import sqlite3
import sys
import time

import numpy as np

# W16/P7: overridable, default unchanged. The producer (pipeline/video/video_face_pass.py)
# writes its bundle to ~/loupe-ml/video/video-faces/export/ while this default points at
# ~/loupe/video-faces-export -- a leftover from when the two halves ran on different hosts.
# Making the path a parameter lets the bundle be validated where it actually is, without
# a filesystem link that would silently freeze one layout as the answer.
EXPORT = os.path.expanduser(
    os.environ.get("LOUPE_VIDEO_EXPORT_DIR", "~/loupe/video-faces-export"))
# faces.db moved to the state root in the 2026-08-07 restructure; this still pointed at
# the old repo-adjacent path. sqlite3.connect() CREATES a missing file, so a run here
# would silently open a brand-new EMPTY database instead of the live one -- it happens to
# fail safe, because the first statement is a SELECT on `processed` which does not exist
# yet, but only by luck. Resolved through loupe_common like every other store.
from loupe_common import APP_DATA as _APP_DATA          # noqa: E402

FACES_DB = os.environ.get("LOUPE_FACES_DB", os.path.join(_APP_DATA, "faces.db"))
EXP = {"embed_model": "buffalo_l", "embed_det": 1024,
       "embed_provider": "CUDAExecutionProvider", "embed_run": "video-2026-07-05"}
BATCH_ASSETS = 400


def load_jsonl():
    """asset_id -> n_faces (authoritative processed set incl 0-face assets)."""
    n_by_asset = {}
    with open(os.path.join(EXPORT, "assets_processed.jsonl")) as f:
        for line in f:
            r = json.loads(line)
            n_by_asset[int(r["asset_id"])] = int(r["n_faces"])
    return n_by_asset


def load_shards():
    """asset_id -> list of (bbox_json, det_score, embedding_bytes). Validates
    per-shard provenance scalars against EXP; aborts on any mismatch/NULL."""
    faces_by_asset = {}
    shards = sorted(glob.glob(os.path.join(EXPORT, "faces_shard_*.npz")))
    if not shards:
        sys.exit("FATAL: no shards found")
    total = 0
    for sp in shards:
        d = np.load(sp, allow_pickle=True)
        # provenance scalars (per-shard) — refuse any NULL / mismatch
        prov = {k: d[k].item() if hasattr(d[k], "item") else d[k] for k in
                ("embed_model", "embed_det", "embed_provider", "embed_run")}
        for k, v in EXP.items():
            if prov.get(k) != v:
                sys.exit(f"FATAL: {os.path.basename(sp)} provenance {k}="
                         f"{prov.get(k)!r} != {v!r}")
        aid = d["asset_id"]
        emb = d["embedding"]          # (N,512) f32
        bbox = d["bbox"]              # (N,4) f32  [x1,y1,x2,y2]
        wh = d["img_wh"]              # (N,2) int32 [w,h]
        det = d["det_score"]          # (N,) f32
        if emb.dtype != np.float32 or emb.shape[1] != 512:
            sys.exit(f"FATAL: {os.path.basename(sp)} embedding dtype/shape "
                     f"{emb.dtype}/{emb.shape}")
        for i in range(len(aid)):
            x1, y1, x2, y2 = (float(v) for v in bbox[i])
            w, h = int(wh[i][0]), int(wh[i][1])
            bj = json.dumps({"x1": round(x1, 1), "y1": round(y1, 1),
                             "x2": round(x2, 1), "y2": round(y2, 1),
                             "img_w": w, "img_h": h})
            row = np.ascontiguousarray(emb[i], dtype="<f4")
            eb = row.tobytes()
            if len(eb) != 2048:
                sys.exit(f"FATAL: {os.path.basename(sp)} row {i} embedding "
                         f"{len(eb)} bytes != 2048")
            faces_by_asset.setdefault(int(aid[i]), []).append(
                (bj, float(det[i]), eb))
            total += 1
    print(f"loaded {total} faces across {len(faces_by_asset)} face-bearing "
          f"assets from {len(shards)} shards", flush=True)
    return faces_by_asset, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    n_by_asset = load_jsonl()
    faces_by_asset, shard_total = load_shards()

    # integrity: every shard asset in jsonl; per-asset counts agree
    stray = set(faces_by_asset) - set(n_by_asset)
    if stray:
        sys.exit(f"FATAL: {len(stray)} shard assets absent from jsonl, e.g. "
                 f"{sorted(stray)[:5]}")
    mism = [(a, len(faces_by_asset.get(a, [])), n) for a, n in n_by_asset.items()
            if len(faces_by_asset.get(a, [])) != n]
    if mism:
        sys.exit(f"FATAL: {len(mism)} assets where shard-face-count != jsonl "
                 f"n_faces, e.g. {mism[:5]}")
    jsonl_sum = sum(n_by_asset.values())
    print(f"jsonl assets={len(n_by_asset)} sum_n_faces={jsonl_sum} "
          f"(== shard faces {shard_total}: {jsonl_sum == shard_total})", flush=True)

    conn = sqlite3.connect(FACES_DB, timeout=20)
    conn.execute("PRAGMA busy_timeout=15000")
    existing = {r[0] for r in conn.execute("SELECT asset_id FROM processed")}
    overlap = set(n_by_asset) & existing
    print(f"already-processed assets to SKIP: {len(overlap)}", flush=True)

    todo = [a for a in n_by_asset if a not in existing]
    now = int(time.time())
    n_faces_ins = n_proc_ins = n_skip = len(overlap)
    n_faces_ins = 0
    n_proc_ins = 0

    if args.dry_run:
        would_faces = sum(n_by_asset[a] for a in todo)
        print(f"[DRY-RUN] would insert processed rows={len(todo)} "
              f"faces={would_faces}; skip={len(overlap)}; NO writes", flush=True)
        # show a sample constructed row
        if todo:
            a0 = next(a for a in todo if faces_by_asset.get(a))
            bj, det, eb = faces_by_asset[a0][0]
            print(f"[DRY-RUN] sample asset {a0}: bbox_json={bj} det={det:.4f} "
                  f"emb_bytes={len(eb)}", flush=True)
        conn.close()
        return 0

    FACE_SQL = ("INSERT INTO faces(asset_id,bbox_json,det_score,embedding,"
                "created_at,embed_model,embed_det,embed_provider,embed_run) "
                "VALUES(?,?,?,?,?,?,?,?,?)")
    for lo in range(0, len(todo), BATCH_ASSETS):
        batch = todo[lo:lo + BATCH_ASSETS]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for a in batch:
                conn.execute(
                    "INSERT INTO processed(asset_id,n_faces,processed_at) "
                    "VALUES(?,?,?)", (a, n_by_asset[a], now))
                n_proc_ins += 1
                for bj, det, eb in faces_by_asset.get(a, []):
                    conn.execute(FACE_SQL, (a, bj, det, eb, now,
                                            EXP["embed_model"], EXP["embed_det"],
                                            EXP["embed_provider"], EXP["embed_run"]))
                    n_faces_ins += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if (lo // BATCH_ASSETS) % 10 == 0:
            print(f"  committed {lo + len(batch)}/{len(todo)} assets · "
                  f"{n_faces_ins} faces", flush=True)
    conn.close()
    print(f"DONE: inserted processed={n_proc_ins} faces={n_faces_ins} "
          f"skipped_assets={len(overlap)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
