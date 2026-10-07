#!/usr/bin/env python3
"""people_clusters.py — batch face-cluster pass for the "name a person" surface.

Groups the faces.db embeddings into person-candidate clusters and persists the
scaffold to the app-owned CLUSTERS.DB SIDECAR (mirrors the pairs.db/renders.db/
edits.db idiom). faces.db is opened mode=ro and NEVER written: naming truth lives
in faces.db persons/assignments; this store is regenerable scaffolding for the
UNNAMED candidate set only — a recluster can never un-name anyone.

Method (design of record, sessions/2026-07-05-local-people-signal.md):
  top-15 cosine kNN + connected components at cosine >= 0.65 over the L2-normalized
  embeddings. 0.65 IS LOAD-BEARING — any looser chain-merges distinct people and dogs into
  one blob (label precision 0.02-0.13); at 0.65 every human evals 0.96-1.00 and the
  residual failure mode is benign SPLIT. Do not lower without re-running the eval.

STABLE cluster ids: components are matched to the previous generation by max
face-overlap (greedy, largest overlap first); matched components keep their old
cluster_id and its claimed_person_id, unmatched ones draw fresh ids from a
monotonic counter that is never reused. Re-runnable; ~80 s for 55.9k faces.

Recurrence stats per cluster: distinct DAYS (capture_timestamp fallback
file_mtime, UTC epoch-days — same metric the 2026-07-05 recon calibrated the
88/33 candidate bars with), distinct assets, face count.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

import numpy as np

from loupe_common import APP_DATA, METADATA_DB, ro

FACES_DB = os.path.join(APP_DATA, "faces.db")
CLUSTERS_DB = os.path.join(APP_DATA, "clusters.db")

K = 15            # kNN neighbors per face
THR = 0.65        # component threshold — load-bearing, see module docstring
CHUNK = 2048      # localworker chunk rows (~28 chunks, <1 GB RAM)
MIN_SIZE = 2      # persist components with >= this many faces (singletons are
                  # one-off/background noise; the candidate bar needs >=10 assets)
N_REPS = 4        # representative face ids stored per cluster (by cos-to-centroid)

BEAT = os.path.expanduser("~/bin/fleet_beat_inline")


def _beat(done, total, label="people-cluster"):
    try:
        subprocess.run([BEAT, "--done", str(done), "--total", str(total),
                        "--label", label], timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def load_embeddings():
    db = ro(FACES_DB)
    rows = db.execute(
        "SELECT face_id, asset_id, embedding, embed_run FROM faces "
        "WHERE embedding IS NOT NULL ORDER BY face_id").fetchall()
    db.close()
    n = len(rows)
    emb = np.empty((n, 512), dtype=np.float32)
    fid = np.empty(n, dtype=np.int64)
    aid = np.empty(n, dtype=np.int64)
    runs = set()
    for i, r in enumerate(rows):
        emb[i] = np.frombuffer(r["embedding"], dtype=np.float32)
        fid[i] = r["face_id"]
        aid[i] = r["asset_id"]
        runs.add(r["embed_run"] or "unstamped")
    del rows
    norms = np.linalg.norm(emb, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    emb /= norms
    return emb, fid, aid, sorted(runs)


def knn_components(emb):
    """Union-find components over the top-K cosine graph at THR."""
    n = emb.shape[0]
    parent = np.arange(n, dtype=np.int32)

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    nchunks = (n + CHUNK - 1) // CHUNK
    for c in range(nchunks):
        lo, hi = c * CHUNK, min((c + 1) * CHUNK, n)
        sims = emb[lo:hi] @ emb.T
        sims[np.arange(hi - lo), np.arange(lo, hi)] = -2.0   # drop self
        part = np.argpartition(-sims, K, axis=1)[:, :K]
        ps = np.take_along_axis(sims, part, axis=1)
        keep = ps >= THR
        for s, t in zip(*np.nonzero(keep)):
            rs, rt = find(lo + int(s)), find(int(part[s, t]))
            if rs != rt:
                parent[rs] = rt
        if c % 3 == 0 or c == nchunks - 1:
            _beat(hi, n)
            print(f"knn chunk {c + 1}/{nchunks}", flush=True)
    return np.array([find(i) for i in range(n)], dtype=np.int32)


def asset_days():
    """asset_id -> UTC epoch-day (capture_timestamp fallback file_mtime)."""
    mdb = ro(METADATA_DB)
    days = {}
    for a, cts, mt in mdb.execute(
            "SELECT id, capture_timestamp, file_mtime FROM assets"):
        t = cts if cts else mt
        if t:
            days[a] = int(t) // 86400
    mdb.close()
    return days


def open_store():
    conn = sqlite3.connect(CLUSTERS_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS clusters(
        cluster_id INTEGER PRIMARY KEY,
        size INTEGER NOT NULL,             /* faces */
        n_assets INTEGER NOT NULL,         /* distinct assets */
        n_days INTEGER NOT NULL,           /* distinct capture days */
        first_day TEXT,                    /* ISO date, display only */
        last_day TEXT,
        centroid BLOB NOT NULL,            /* float32 x 512, L2-normalized */
        rep_face_ids TEXT NOT NULL,        /* JSON [face_id,...] by cos desc */
        claimed_person_id INTEGER,         /* set by /api/person/create */
        dismissed INTEGER,                 /* 1 = "not a person" (candidates skip) */
        updated_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS cluster_faces(
        face_id INTEGER PRIMARY KEY,
        cluster_id INTEGER NOT NULL,
        cos_to_centroid REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_cf_cluster ON cluster_faces(cluster_id);
    CREATE TABLE IF NOT EXISTS cluster_meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(clusters)")}
    if "dismissed" not in cols:            # pre-dismiss store: additive migrate
        conn.execute("ALTER TABLE clusters ADD COLUMN dismissed INTEGER")
    conn.commit()
    return conn


def main():
    t0 = time.time()
    print(f"faces={FACES_DB}\nclusters={CLUSTERS_DB}", flush=True)
    emb, fid, aid, runs = load_embeddings()
    n = len(fid)
    print(f"loaded+normalized {n} embeddings (runs={runs}) "
          f"in {time.time() - t0:.1f}s", flush=True)

    comp = knn_components(emb)
    groups = {}
    for i, c in enumerate(comp.tolist()):
        groups.setdefault(c, []).append(i)
    kept = [rows for rows in groups.values() if len(rows) >= MIN_SIZE]
    kept.sort(key=len, reverse=True)
    print(f"components: {len(groups)} total, {len(kept)} with size>={MIN_SIZE} "
          f"in {time.time() - t0:.1f}s", flush=True)

    days = asset_days()
    store = open_store()

    # previous generation — for stable-id matching + claimed carry-forward
    prev_cluster_of = {}          # face_id -> cluster_id
    for f, c in store.execute("SELECT face_id, cluster_id FROM cluster_faces"):
        prev_cluster_of[f] = c
    prev_claimed = {c: p for c, p in store.execute(
        "SELECT cluster_id, claimed_person_id FROM clusters "
        "WHERE claimed_person_id IS NOT NULL")}
    prev_dismissed = {c for (c,) in store.execute(
        "SELECT cluster_id FROM clusters WHERE dismissed=1")}
    next_id = int((store.execute(
        "SELECT value FROM cluster_meta WHERE key='next_cluster_id'"
    ).fetchone() or [1])[0])

    # greedy overlap match, largest overlap first; each old id used once
    overlaps = []                 # (overlap, new_idx, old_cluster_id)
    for gi, rows in enumerate(kept):
        votes = {}
        for i in rows:
            oc = prev_cluster_of.get(int(fid[i]))
            if oc is not None:
                votes[oc] = votes.get(oc, 0) + 1
        for oc, v in votes.items():
            overlaps.append((v, gi, oc))
    overlaps.sort(reverse=True)
    id_of = {}
    used_old = set()
    for v, gi, oc in overlaps:
        if gi in id_of or oc in used_old:
            continue
        id_of[gi] = oc
        used_old.add(oc)
    for gi in range(len(kept)):
        if gi not in id_of:
            id_of[gi] = next_id
            next_id += 1

    now = int(time.time())
    store.execute("BEGIN IMMEDIATE")
    store.execute("DELETE FROM clusters")
    store.execute("DELETE FROM cluster_faces")
    for gi, rows in enumerate(kept):
        cid = id_of[gi]
        cen = emb[rows].mean(axis=0)
        cen /= max(float(np.linalg.norm(cen)), 1e-12)
        cos = emb[rows] @ cen
        order = np.argsort(-cos)
        reps = [int(fid[rows[int(o)]]) for o in order[:N_REPS]]
        assets = {int(aid[i]) for i in rows}
        dset = {days[a] for a in assets if a in days}
        first = last = None
        if dset:
            first = time.strftime("%Y-%m-%d", time.gmtime(min(dset) * 86400))
            last = time.strftime("%Y-%m-%d", time.gmtime(max(dset) * 86400))
        store.execute(
            "INSERT INTO clusters(cluster_id, size, n_assets, n_days, first_day,"
            " last_day, centroid, rep_face_ids, claimed_person_id, dismissed,"
            " updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (cid, len(rows), len(assets), len(dset), first, last,
             cen.astype(np.float32).tobytes(), json.dumps(reps),
             prev_claimed.get(cid), 1 if cid in prev_dismissed else None, now))
        store.executemany(
            "INSERT INTO cluster_faces(face_id, cluster_id, cos_to_centroid)"
            " VALUES(?,?,?)",
            [(int(fid[i]), cid, round(float(cos[j]), 4))
             for j, i in enumerate(rows)])
    meta = {
        "threshold": str(THR), "knn_k": str(K), "min_size": str(MIN_SIZE),
        "n_faces_clustered": str(n), "embed_runs": json.dumps(runs),
        "next_cluster_id": str(next_id), "last_run_at": str(now),
    }
    store.executemany(
        "INSERT INTO cluster_meta(key, value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", meta.items())
    store.commit()

    for a, d in (((10, 5), None), ((20, 10), None)):
        cnt = store.execute(
            "SELECT COUNT(*) FROM clusters WHERE n_assets>=? AND n_days>=?",
            a).fetchone()[0]
        print(f"candidate bar >={a[0]}a & >={a[1]}d: {cnt} clusters", flush=True)
    store.close()
    _beat(1, 1, "people-cluster done")
    print(f"done: {len(kept)} clusters persisted in {time.time() - t0:.1f}s",
          flush=True)


if __name__ == "__main__":
    sys.exit(main())
