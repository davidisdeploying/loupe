"""Populate assets_scored.zeroshot_aesthetic / zeroshot_tech in preferences.db.

Reuses zeroshot_probe.py's pinned embed+dot logic (AESTHETIC_A, TECH_SHARP axes)
for exactly the asset_ids already in assets_scored (the ~40,400 pairs.npz assets
with a cached thumb). Read-only w.r.t. the embeddings DB / zeroshot module;
writes only preferences.db (additive ALTER + UPDATE, WAL, short txn).
"""

import sys
import sqlite3

sys.path.insert(0, "/home/david/loupe/aesthetic/zeroshot")
from zeroshot_probe import (  # noqa: E402
    PROMPT_AXES,
    embed_prompt,
    load_embeddings,
    softmax_score,
    TEMPERATURE,
)

DB_PATH = "/home/david/loupe/aesthetic/preferences.db"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    target_ids = [r[0] for r in conn.execute("SELECT asset_id FROM assets_scored")]
    print(f"assets_scored target ids: {len(target_ids)}")

    print("embedding prompts (AESTHETIC_A, TECH_SHARP) via pinned recipe...", flush=True)
    aes_pos, aes_neg = PROMPT_AXES["AESTHETIC_A"]
    tech_pos, tech_neg = PROMPT_AXES["TECH_SHARP"]
    aes_pos_vec, aes_neg_vec = embed_prompt(aes_pos), embed_prompt(aes_neg)
    tech_pos_vec, tech_neg_vec = embed_prompt(tech_pos), embed_prompt(tech_neg)

    print("loading image embeddings...", flush=True)
    emb_ids, emb_mat = load_embeddings()
    print(f"  {emb_mat.shape[0]} embeddings", flush=True)

    id_to_idx = {int(aid): i for i, aid in enumerate(emb_ids)}
    missing = [aid for aid in target_ids if aid not in id_to_idx]
    if missing:
        print(f"  WARNING: {len(missing)} assets_scored ids have no embedding (e.g. {missing[:5]})")

    cos_aes_pos = emb_mat @ aes_pos_vec
    cos_aes_neg = emb_mat @ aes_neg_vec
    aes_scores = softmax_score(cos_aes_pos, cos_aes_neg, TEMPERATURE)

    cos_tech_pos = emb_mat @ tech_pos_vec
    cos_tech_neg = emb_mat @ tech_neg_vec
    tech_scores = softmax_score(cos_tech_pos, cos_tech_neg, TEMPERATURE)

    rows = []
    for aid in target_ids:
        idx = id_to_idx.get(aid)
        if idx is None:
            continue
        rows.append((float(aes_scores[idx]), float(tech_scores[idx]), aid))
    print(f"computed scores for {len(rows)} assets", flush=True)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(assets_scored)")}
    if "zeroshot_aesthetic" not in cols:
        conn.execute("ALTER TABLE assets_scored ADD COLUMN zeroshot_aesthetic REAL")
    if "zeroshot_tech" not in cols:
        conn.execute("ALTER TABLE assets_scored ADD COLUMN zeroshot_tech REAL")
    conn.commit()

    conn.executemany(
        "UPDATE assets_scored SET zeroshot_aesthetic=?, zeroshot_tech=? WHERE asset_id=?",
        rows,
    )
    conn.commit()

    n_populated = conn.execute(
        "SELECT COUNT(*) FROM assets_scored WHERE zeroshot_aesthetic IS NOT NULL"
    ).fetchone()[0]
    print(f"zeroshot_aesthetic populated rows: {n_populated}")
    conn.close()


if __name__ == "__main__":
    main()
