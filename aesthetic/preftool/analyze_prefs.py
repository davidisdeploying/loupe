import sqlite3
import math

DB_PATH = "/home/david/loupe/aesthetic/preferences.db"


def wilson_ci(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (p, max(0.0, center - half), min(1.0, center + half))


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(assets_scored)")]
    model_cols = [c for c in cols if c != "asset_id"]

    judgments = conn.execute(
        "SELECT winner_asset, loser_asset FROM judgments WHERE skipped=0"
    ).fetchall()

    n_total = len(judgments)
    n_skipped = conn.execute("SELECT COUNT(*) c FROM judgments WHERE skipped=1").fetchone()[0]

    scores = {}
    for row in conn.execute("SELECT * FROM assets_scored"):
        scores[row["asset_id"]] = {c: row[c] for c in model_cols}

    print(f"non-skip judgments: {n_total}   skipped: {n_skipped}")
    print()
    header = f"{'model':<20} {'agreement':>10} {'n':>6} {'95% CI':>18}"
    print(header)
    print("-" * len(header))

    for col in model_cols:
        agree = 0
        n = 0
        for j in judgments:
            wa, la = j["winner_asset"], j["loser_asset"]
            sw = scores.get(wa, {}).get(col)
            sl = scores.get(la, {}).get(col)
            if sw is None or sl is None or sw == sl:
                continue
            n += 1
            if sw > sl:
                agree += 1
        p, lo, hi = wilson_ci(agree, n)
        tag = "  <-- David vs Apple" if col == "apple" else ""
        print(f"{col:<20} {p*100:>9.1f}% {n:>6} [{lo*100:5.1f}%, {hi*100:5.1f}%]{tag}")

    conn.close()


if __name__ == "__main__":
    main()
