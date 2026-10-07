"""Step 4b: cross-host semantic proof. Embed queries on delta's CPU stack,
NN-search the transferred (charlie-built) embeddings db, compare top hits
against charlie's CUDA-side reference results."""

import sqlite3
import sys
import time

import numpy as np
import sqlite_vec

sys.path.insert(0, "/home/david/loupe/stage5")

from text_embed_cpu import embed_text, get_session

DB_PATH = "/home/david/loupe/stage5/embeddings_siglip2.db"
META_DB_PATH = "/home/david/loupe-pipeline/metadata.db"


def open_vec_db():
    con = sqlite3.connect(DB_PATH)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    return con


def resolve_filepath(meta_con, asset_id):
    row = meta_con.execute(
        "SELECT filepath FROM assets WHERE id = ?", (asset_id,)
    ).fetchone()
    return row[0] if row else None


def search(con, meta_con, query, k=5, candidate_pool=50):
    t0 = time.time()
    vec = embed_text(query)[0].astype("float32")
    t_embed = time.time()
    # Use the vec0 ANN index (MATCH, L2 metric -- fast) to pull a candidate
    # pool, then re-rank by true cosine distance in Python. For unit-norm
    # vectors L2^2 = 2*(1-cos_sim), a monotonic transform of cosine distance,
    # so the candidate pool from the L2 index is exact -- this only fixes up
    # the reported distance to be the cosine metric charlie's reference used.
    rows = con.execute(
        """
        SELECT asset_id, embedding, distance
        FROM vec_images
        WHERE embedding MATCH ?
        ORDER BY distance
        LIMIT ?
        """,
        (vec.tobytes(), candidate_pool),
    ).fetchall()
    t_search = time.time()

    reranked = []
    for asset_id, emb_blob, _l2dist in rows:
        v = np.frombuffer(emb_blob, dtype=np.float32)
        cos_dist = 1.0 - float(np.dot(vec, v))
        reranked.append((asset_id, cos_dist))
    reranked.sort(key=lambda r: r[1])
    reranked = reranked[:k]

    print(f"\nQuery: {query!r}")
    print(f"  embed_time={t_embed - t0:.3f}s  search_time={t_search - t_embed:.3f}s  total={t_search - t0:.3f}s")
    for rank, (asset_id, dist) in enumerate(reranked, start=1):
        fp = resolve_filepath(meta_con, asset_id)
        print(f"  #{rank}  asset_id={asset_id:<7} cos_distance={dist:.6f}  {fp}")
    return reranked, (t_search - t0)


def main():
    con = open_vec_db()
    meta_con = sqlite3.connect(META_DB_PATH)

    sess = get_session()
    print("providers actually used:", sess.get_providers())

    print("\n" + "=" * 70)
    print("CHECK 1: cross-host reference queries (compare vs charlie CUDA top hits)")
    print("=" * 70)

    ref_queries = ["a dog", "a screenshot of a phone or computer screen"]
    latencies = []
    for q in ref_queries:
        rows, elapsed = search(con, meta_con, q, k=5)
        latencies.append(elapsed)

    print("\n" + "=" * 70)
    print("CHECK 2: fresh queries + image inspection")
    print("=" * 70)

    fresh_queries = ["a birthday cake", "a mountain landscape"]
    for q in fresh_queries:
        rows, elapsed = search(con, meta_con, q, k=5)
        latencies.append(elapsed)

    print("\n" + "=" * 70)
    print(f"CHECK 3: latency — single-query times: {[f'{l:.3f}s' for l in latencies]}")
    print("=" * 70)

    con.close()
    meta_con.close()


if __name__ == "__main__":
    main()
