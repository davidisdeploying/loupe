import sqlite3
import numpy as np
import sqlite_vec
import json
import sys

EMB_DB = "/home/david/loupe/stage5/embeddings_siglip2.db"
SCORE_DB = "/home/david/loupe/apple-enrichment.db"

def open_ro(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return conn

print("loading sqlite_vec + reading embeddings...", flush=True)
emb_conn = open_ro(EMB_DB)
emb_conn.enable_load_extension(True)
sqlite_vec.load(emb_conn)
emb_conn.enable_load_extension(False)

cur = emb_conn.execute("SELECT asset_id, embedding FROM vec_images")
ids = []
embs = []
n = 0
for asset_id, emb_blob in cur:
    ids.append(asset_id)
    embs.append(np.frombuffer(emb_blob, dtype=np.float32))
    n += 1
    if n % 10000 == 0:
        print(f"  ...{n} embeddings read", flush=True)

emb_ids = np.array(ids, dtype=np.int64)
emb_mat = np.vstack(embs).astype(np.float32)
print(f"total embeddings: {emb_mat.shape}", flush=True)
emb_conn.close()

print("reading apple_score...", flush=True)
score_conn = open_ro(SCORE_DB)
score_rows = score_conn.execute(
    "SELECT asset_id, overall FROM apple_score WHERE overall IS NOT NULL"
).fetchall()
score_conn.close()
score_map = {aid: overall for aid, overall in score_rows}
print(f"total non-null apple_score rows: {len(score_map)}", flush=True)

id_to_idx = {aid: i for i, aid in enumerate(emb_ids)}
common_ids = [aid for aid in score_map.keys() if aid in id_to_idx]
common_ids.sort()

N = len(common_ids)
print(f"intersection N = {N}", flush=True)

X = np.zeros((N, emb_mat.shape[1]), dtype=np.float32)
y = np.zeros((N,), dtype=np.float32)
out_ids = np.zeros((N,), dtype=np.int64)

for i, aid in enumerate(common_ids):
    X[i] = emb_mat[id_to_idx[aid]]
    y[i] = score_map[aid]
    out_ids[i] = aid

np.savez(
    "/home/david/loupe/aesthetic/pairs.npz",
    X=X, y=y, ids=out_ids
)

norms = np.linalg.norm(X, axis=1)
sanity = {
    "N": N,
    "total_embeddings": int(emb_mat.shape[0]),
    "total_apple_scores_nonnull": len(score_map),
    "embedding_norm_mean": float(norms.mean()),
    "embedding_norm_median": float(np.median(norms)),
    "y_min": float(y.min()),
    "y_max": float(y.max()),
    "y_mean": float(y.mean()),
    "y_std": float(y.std()),
    "count_y_ge_0.6": int((y >= 0.6).sum()),
    "count_y_le_0.25": int((y <= 0.25).sum()),
}
with open("/home/david/loupe/aesthetic/sanity.json", "w") as f:
    json.dump(sanity, f, indent=2)

print(json.dumps(sanity, indent=2))
