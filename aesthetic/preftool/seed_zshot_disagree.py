"""Seed apple_zshot_disagree pairs into preferences.db: strong-gap conflicts
where Apple's score and the zero-shot AESTHETIC-A score rank two assets in
opposite directions. Additive only -- skips pairs that already exist, never
touches judgments. WAL + short txn (service is live)."""

import sqlite3
import json

import numpy as np

DB_PATH = "/home/david/loupe/aesthetic/preferences.db"

N_TARGET = 180
APPLE_GAP_THRESHOLD = 0.15   # percentile-rank gap (0..1)
ZSHOT_GAP_THRESHOLD = 0.15   # percentile-rank gap (0..1)


def percentile_ranks(values):
    order = np.argsort(values)
    ranks = np.empty_like(order, dtype=np.float64)
    n = len(values)
    ranks[order] = np.arange(n)
    return ranks / max(n - 1, 1)


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    rows = conn.execute(
        "SELECT asset_id, apple, zeroshot_aesthetic FROM assets_scored "
        "WHERE apple IS NOT NULL AND zeroshot_aesthetic IS NOT NULL"
    ).fetchall()
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    apple = np.array([r[1] for r in rows], dtype=np.float64)
    zshot = np.array([r[2] for r in rows], dtype=np.float64)
    print(f"candidate pool: {len(ids)}")

    apple_pct = percentile_ranks(apple)
    zshot_pct = percentile_ranks(zshot)
    gap = apple_pct - zshot_pct  # >0: apple ranks it well above zshot

    apple_pct_map = {int(a): float(p) for a, p in zip(ids, apple_pct)}
    zshot_pct_map = {int(a): float(p) for a, p in zip(ids, zshot_pct)}

    order_by_gap_desc = ids[np.argsort(-gap)]   # apple loves / zshot meh, first
    order_by_gap_asc = ids[np.argsort(gap)]     # apple meh / zshot loves, first

    existing_pairs = set(
        (min(a, b), max(a, b))
        for a, b in conn.execute("SELECT asset_a, asset_b FROM pairs")
    )

    new_pairs = []

    def add_pair(a, b, source):
        key = (min(a, b), max(a, b))
        if a == b or key in existing_pairs:
            return False
        existing_pairs.add(key)
        new_pairs.append((int(a), int(b), source))
        return True

    count = 0
    i = j = 0
    while count < N_TARGET and i < len(order_by_gap_desc) and j < len(order_by_gap_asc):
        a = int(order_by_gap_desc[i])   # apple-favors-A candidate
        b = int(order_by_gap_asc[j])    # zshot-favors-B candidate
        apple_gap = apple_pct_map[a] - apple_pct_map[b]
        zshot_gap = zshot_pct_map[b] - zshot_pct_map[a]
        if apple_gap > APPLE_GAP_THRESHOLD and zshot_gap > ZSHOT_GAP_THRESHOLD:
            if add_pair(a, b, "apple_zshot_disagree"):
                count += 1
            i += 1
            j += 1
        elif apple_gap <= APPLE_GAP_THRESHOLD:
            i += 1
        else:
            j += 1
    print(f"apple_zshot_disagree pairs found: {count}")

    conn.executemany(
        "INSERT INTO pairs(asset_a, asset_b, source) VALUES (?,?,?)",
        new_pairs,
    )
    conn.commit()

    counts = dict(conn.execute("SELECT source, COUNT(*) FROM pairs GROUP BY source").fetchall())
    total = conn.execute("SELECT COUNT(*) FROM pairs").fetchone()[0]
    print("pair counts by source:", json.dumps(counts, indent=2))
    print("total pairs:", total)

    conn.close()


if __name__ == "__main__":
    main()
