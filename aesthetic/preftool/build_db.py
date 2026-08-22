import sqlite3
import json
import random
import os
import numpy as np
import onnxruntime as ort

random.seed(1729)
np.random.seed(1729)

THUMBS = "/home/david/loupe-pipeline/culling/contactsheets/thumbs"
PAIRS_NPZ = "/home/david/loupe/aesthetic/pairs.npz"
ONNX_PATH = "/home/david/loupe/aesthetic/aesthetic_head.onnx"
DB_PATH = "/home/david/loupe/aesthetic/preferences.db"

N_QUANTILE_SPREAD = 180
N_RANDOM = 150
N_DISAGREE_MAX = 100


def has_thumb(asset_id):
    return os.path.exists(os.path.join(THUMBS, f"{asset_id}.jpg"))


def main():
    data = np.load(PAIRS_NPZ)
    X, y, ids = data["X"], data["y"], data["ids"]

    sess = ort.InferenceSession(ONNX_PATH, providers=["CPUExecutionProvider"])
    v1_pred = sess.run(["score"], {"embedding": X.astype(np.float32)})[0].squeeze(1)

    # keep only assets with a cached thumbnail
    keep_mask = np.array([has_thumb(int(i)) for i in ids])
    ids_k = ids[keep_mask]
    apple_k = y[keep_mask]
    v1_k = v1_pred[keep_mask]
    print(f"total pairs.npz assets: {len(ids)}, with cached thumb: {len(ids_k)}")

    asset_apple = {int(a): float(s) for a, s in zip(ids_k, apple_k)}
    asset_v1 = {int(a): float(s) for a, s in zip(ids_k, v1_k)}
    all_ids = list(asset_apple.keys())

    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS assets_scored(
        asset_id INTEGER PRIMARY KEY,
        apple REAL,
        v1 REAL
    );
    CREATE TABLE IF NOT EXISTS pairs(
        pair_id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_a INTEGER NOT NULL,
        asset_b INTEGER NOT NULL,
        source TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS judgments(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pair_id INTEGER NOT NULL,
        winner_asset INTEGER,
        loser_asset INTEGER,
        skipped INTEGER NOT NULL DEFAULT 0,
        shown_left_asset INTEGER,
        ts_utc TEXT NOT NULL
    );
    """)
    conn.executemany(
        "INSERT OR REPLACE INTO assets_scored(asset_id, apple, v1) VALUES (?,?,?)",
        [(a, asset_apple[a], asset_v1[a]) for a in all_ids],
    )
    conn.commit()

    existing_pairs = set(
        (min(a, b), max(a, b))
        for a, b in conn.execute("SELECT asset_a, asset_b FROM pairs")
    )

    new_pairs = []  # (asset_a, asset_b, source)

    def add_pair(a, b, source):
        key = (min(a, b), max(a, b))
        if a == b or key in existing_pairs:
            return False
        existing_pairs.add(key)
        new_pairs.append((a, b, source))
        return True

    # --- apple_v1_disagree ---
    # apple decile buckets
    bucket = {a: min(int(asset_apple[a] * 10), 9) for a in all_ids}
    high_pool = [a for a in all_ids if bucket[a] >= 7]
    low_pool = [a for a in all_ids if bucket[a] <= 2]

    v1_sorted_high = sorted(high_pool, key=lambda a: asset_v1[a])  # low v1 first
    v1_sorted_low = sorted(low_pool, key=lambda a: -asset_v1[a])   # high v1 first

    disagree_count = 0
    i = j = 0
    while disagree_count < N_DISAGREE_MAX and i < len(v1_sorted_high) and j < len(v1_sorted_low):
        a = v1_sorted_high[i]
        b = v1_sorted_low[j]
        if asset_apple[a] - asset_apple[b] > 0.15 and asset_v1[b] - asset_v1[a] > 0.03:
            if add_pair(a, b, "apple_v1_disagree"):
                disagree_count += 1
            i += 1
            j += 1
        elif asset_apple[a] - asset_apple[b] <= 0.15:
            j += 1
        else:
            i += 1
    print(f"apple_v1_disagree pairs found: {disagree_count}")

    # --- apple_quantile_spread ---
    by_bucket = {b: [a for a in all_ids if bucket[a] == b] for b in range(10)}
    spread_count = 0
    combos = [(9, 4), (8, 5), (9, 5), (8, 4), (4, 0), (5, 1), (4, 1), (5, 0)]
    ci = 0
    attempts = 0
    while spread_count < N_QUANTILE_SPREAD and attempts < N_QUANTILE_SPREAD * 20:
        hi_b, lo_b = combos[ci % len(combos)]
        ci += 1
        attempts += 1
        pool_hi = by_bucket.get(hi_b) or []
        pool_lo = by_bucket.get(lo_b) or []
        if not pool_hi or not pool_lo:
            continue
        a = random.choice(pool_hi)
        b = random.choice(pool_lo)
        if add_pair(a, b, "apple_quantile_spread"):
            spread_count += 1
    print(f"apple_quantile_spread pairs found: {spread_count}")

    # --- random ---
    random_count = 0
    attempts = 0
    while random_count < N_RANDOM and attempts < N_RANDOM * 20:
        attempts += 1
        a, b = random.sample(all_ids, 2)
        if add_pair(a, b, "random"):
            random_count += 1
    print(f"random pairs found: {random_count}")

    conn.executemany(
        "INSERT INTO pairs(asset_a, asset_b, source) VALUES (?,?,?)",
        new_pairs,
    )
    conn.commit()

    counts = dict(conn.execute("SELECT source, COUNT(*) FROM pairs GROUP BY source").fetchall())
    total = conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
    print("pair counts by source:", json.dumps(counts, indent=2))
    print("total pairs:", total)
    print("assets_scored rows:", conn.execute("SELECT COUNT(*) FROM assets_scored").fetchone()[0])

    conn.close()


if __name__ == "__main__":
    main()
