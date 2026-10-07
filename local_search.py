"""Local semantic search (stage 5, step 4c): text query -> SigLIP2 text embedding ->
sqlite-vec ANN candidate pool -> cosine rerank -> ordered asset_id list.

The query recipe (recipe_siglip2.py / text_embed_cpu.py) is single-sourced from
stage5/ via sys.path, NEVER vendored here -- it must stay byte-identical to the
validated recipe (see stage5/validate_step4b.py, the proven reference for this
candidate-pool + cosine-rerank pattern). Only the NN/glue code below is new.

Lazy: the ~2.7GB textual model loads on the first search() call, never at import.
"""

import os
import sys
import sqlite3
import threading

import numpy as np
import sqlite_vec

_embeddings_db = None
_embed_text = None  # set on first use; import triggers the lazy session load
_reset_text_session = None
_idle_timer = None
_idle_lock = threading.Lock()
_idle_seconds = int(os.environ.get("LOUPE_SEARCH_IDLE_SECONDS", "600"))


def init(embeddings_db, stage5_dir):
    """Wire paths only -- cheap, safe to call at server import time. Does NOT load
    the model or open the embeddings db."""
    global _embeddings_db
    _embeddings_db = embeddings_db
    if stage5_dir not in sys.path:
        sys.path.insert(0, stage5_dir)


def _get_embed_text():
    global _embed_text, _reset_text_session
    if _embed_text is None:
        from text_embed_cpu import embed_text, reset_session
        _embed_text = embed_text
        _reset_text_session = reset_session
    return _embed_text


def _unload_if_idle():
    global _idle_timer
    with _idle_lock:
        _idle_timer = None
        if _reset_text_session is not None:
            _reset_text_session()


def _touch_idle_timer():
    global _idle_timer
    with _idle_lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
        _idle_timer = threading.Timer(_idle_seconds, _unload_if_idle)
        _idle_timer.daemon = True
        _idle_timer.start()


def _open_vec_db():
    con = sqlite3.connect(_embeddings_db)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    return con


def similar(asset_id, k=60, candidate_pool=None):
    """Nearest neighbours of an asset, by its OWN embedding (audit 9.5 / C4).

    Returns [(asset_id, similarity)] ordered nearest-first, similarity in 0..1 as
    1 - cosine distance, with the query asset itself removed.

    Note this never touches the text model: the query vector is read straight out of
    vec_images, so "more like this" costs one extra row read rather than the ~2.7GB
    onnxruntime session that a text query needs. Same L2-pool-then-cosine-rerank as
    search() -- exact for unit-norm vectors.
    """
    try:
        asset_id = int(asset_id)
    except (TypeError, ValueError):
        return []
    pool = candidate_pool or max(4 * k, 200)

    con = _open_vec_db()
    try:
        row = con.execute(
            "SELECT embedding FROM vec_images WHERE asset_id = ?", (asset_id,)
        ).fetchone()
        if not row:
            return []
        vec = np.frombuffer(row[0], dtype=np.float32)
        rows = con.execute(
            """
            SELECT asset_id, embedding
            FROM vec_images
            WHERE embedding MATCH ?
            ORDER BY distance
            LIMIT ?
            """,
            (vec.tobytes(), pool + 1),      # +1: the asset matches itself first
        ).fetchall()
    finally:
        con.close()

    out = []
    for aid, emb_blob in rows:
        if aid == asset_id:
            continue
        v = np.frombuffer(emb_blob, dtype=np.float32)
        # vectors are unit-norm, so the dot product IS the cosine similarity
        out.append((aid, float(np.dot(vec, v))))
    out.sort(key=lambda r: -r[1])
    return out[:k]


def search(query, k=60, candidate_pool=None):
    """Returns an ordered list of asset_id ints (nearest first) for a text query.
    candidate_pool defaults to 4x k (floor 200) -- the vec0 MATCH/L2 index over-fetches
    a pool, then re-ranks it by true cosine distance in Python (see validate_step4b.py
    for why the L2 pool is exact for unit-norm vectors)."""
    query = (query or "").strip()
    if not query:
        return []
    embed_text = _get_embed_text()
    vec = embed_text(query)[0].astype("float32")
    _touch_idle_timer()
    pool = candidate_pool or max(4 * k, 200)

    con = _open_vec_db()
    try:
        rows = con.execute(
            """
            SELECT asset_id, embedding, distance
            FROM vec_images
            WHERE embedding MATCH ?
            ORDER BY distance
            LIMIT ?
            """,
            (vec.tobytes(), pool),
        ).fetchall()
    finally:
        con.close()

    reranked = []
    for asset_id, emb_blob, _l2dist in rows:
        v = np.frombuffer(emb_blob, dtype=np.float32)
        cos_dist = 1.0 - float(np.dot(vec, v))
        reranked.append((asset_id, cos_dist))
    reranked.sort(key=lambda r: r[1])
    return [asset_id for asset_id, _dist in reranked[:k]]


def health(metadata_db, video_extensions):
    """Bounded coverage receipt; never loads either model tower."""
    meta = sqlite3.connect(f"file:{metadata_db}?mode=ro", uri=True)
    try:
        eligible = meta.execute(
            "SELECT count(*) FROM assets WHERE mime_type LIKE 'image/%'",
        ).fetchone()[0]
    finally:
        meta.close()
    vec = _open_vec_db()
    try:
        vectors = vec.execute("SELECT count(*) FROM vec_images").fetchone()[0]
        failed = vec.execute(
            "SELECT count(*) FROM processed WHERE status='fail'"
        ).fetchone()[0]
        unindexable = vec.execute(
            "SELECT count(*) FROM processed WHERE status='unindexable'"
        ).fetchone()[0]
    finally:
        vec.close()
    missing = max(0, int(eligible) - int(vectors) - int(unindexable))
    return {
        "ok": missing == 0 and int(failed) == 0,
        "eligible_assets": int(eligible),
        "vector_rows": int(vectors),
        "missing_vectors": missing,
        "failed_assets": int(failed),
        "unindexable_assets": int(unindexable),
        "idle_unload_seconds": _idle_seconds,
    }
