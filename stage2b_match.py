#!/usr/bin/env python3
"""stage2b_match.py — assign video faces to named people by centroid cosine.

Standalone batch matcher (NOT the per-face HTTP confirm route). Reversible:
writes only source='video-match' rows. Reversal:
    DELETE FROM assignments WHERE source='video-match';

Per eligible person (is_pet=0): centroid over _anchors_for(pid) = every live
assignment for that person (Apple seeds + cluster + propagated + David's naming),
L2-normalized. Per video face (embed_run LIKE 'video-%'): argmax cosine over
eligible centroids; assign iff cos >= ASSIGN_THR. Protect-band CSV for
PROTECT_LO <= cos < ASSIGN_THR.

Usage:
    stage2b_match.py --dry-run   # compute + report, NO writes, still emits CSV
    stage2b_match.py             # write assignments + CSV
"""
import argparse
import csv
import os
import sqlite3
import sys
import time

import numpy as np

FACES_DB = os.path.expanduser("~/loupe/faces.db")
ASSIGN_THR = 0.60
PROTECT_LO = 0.50


def l2(a):
    n = np.linalg.norm(a, axis=-1, keepdims=True)
    n[n == 0] = 1.0
    return a / n


def load_emb(conn, face_ids):
    """(K,512) f32 raw embeddings for the given face_ids, order preserved."""
    out = np.empty((len(face_ids), 512), dtype=np.float32)
    q = "SELECT embedding FROM faces WHERE face_id=?"
    for i, fid in enumerate(face_ids):
        (blob,) = conn.execute(q, (fid,)).fetchone()
        out[i] = np.frombuffer(blob, dtype="<f4")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(FACES_DB, timeout=20)
    conn.execute("PRAGMA busy_timeout=15000")

    # eligible persons (is_pet=0) with >=1 anchor assignment
    persons = {pid: name for pid, name in
               conn.execute("SELECT person_id, name FROM persons WHERE is_pet=0")}
    cents, cids, cnames, canchors = [], [], [], []
    for pid, name in sorted(persons.items()):
        anchors = [r[0] for r in conn.execute(
            "SELECT face_id FROM assignments WHERE person_id=?", (pid,))]
        if not anchors:
            print(f"  person {pid} {name}: 0 anchors — skipped", flush=True)
            continue
        cen = l2(load_emb(conn, anchors)).mean(axis=0)
        cen = cen / max(float(np.linalg.norm(cen)), 1e-12)
        cents.append(cen.astype(np.float32))
        cids.append(pid)
        cnames.append(name)
        canchors.append(len(anchors))
    C = np.vstack(cents)                       # (P,512)
    print(f"eligible centroids: {len(cids)} persons "
          f"({dict(zip(cnames, canchors))})", flush=True)

    # video faces
    vf = conn.execute(
        "SELECT face_id, asset_id, embedding FROM faces "
        "WHERE embed_run LIKE 'video-%' ORDER BY face_id").fetchall()
    n = len(vf)
    fids = np.array([r[0] for r in vf], dtype=np.int64)
    aids = np.array([r[1] for r in vf], dtype=np.int64)
    V = np.empty((n, 512), dtype=np.float32)
    for i, r in enumerate(vf):
        V[i] = np.frombuffer(r[2], dtype="<f4")
    V = l2(V)
    print(f"video faces: {n}", flush=True)

    sims = V @ C.T                             # (n,P) cosine (both unit)
    best = sims.argmax(axis=1)
    bestcos = sims[np.arange(n), best]

    assign_mask = bestcos >= ASSIGN_THR
    band_mask = (bestcos >= PROTECT_LO) & (bestcos < ASSIGN_THR)

    # per-person distribution
    dist = {}
    for j in np.nonzero(assign_mask)[0]:
        pid = cids[int(best[j])]
        dist[pid] = dist.get(pid, 0) + 1
    print(f"assignments (cos>={ASSIGN_THR}): {int(assign_mask.sum())}", flush=True)
    for pid in sorted(dist, key=lambda p: -dist[p]):
        print(f"    person {pid} {persons[pid]}: {dist[pid]}", flush=True)

    # protect-band CSV
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    csv_path = os.path.expanduser(f"~/loupe/video-match-protectband-{stamp}.csv")
    band_idx = np.nonzero(band_mask)[0]
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["asset_id", "person_id", "person_name", "cos"])
        for j in band_idx:
            pid = cids[int(best[j])]
            w.writerow([int(aids[j]), pid, persons[pid], f"{float(bestcos[j]):.4f}"])
    print(f"protect-band [{PROTECT_LO},{ASSIGN_THR}) rows: {len(band_idx)} -> "
          f"{csv_path}", flush=True)

    if args.dry_run:
        print("[DRY-RUN] no assignments written", flush=True)
        conn.close()
        return 0

    now = int(time.time())
    rows = [(int(fids[j]), cids[int(best[j])], float(bestcos[j]), now)
            for j in np.nonzero(assign_mask)[0]]
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany(
        "INSERT OR IGNORE INTO assignments"
        "(face_id,person_id,source,confidence,confirmed,updated_at) "
        "VALUES(?,?,'video-match',?,0,?)", rows)
    conn.commit()
    ins = conn.execute(
        "SELECT COUNT(*) FROM assignments WHERE source='video-match'").fetchone()[0]
    conn.close()
    print(f"DONE: wrote {ins} source='video-match' assignments "
          f"(attempted {len(rows)})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
