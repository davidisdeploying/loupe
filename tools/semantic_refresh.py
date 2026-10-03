"""
Incremental SigLIP2 so400m refresh -- all current still images in Loupe's live
metadata database, resolve -> decode -> preprocess (via
recipe_siglip2) -> visual/model.onnx (CUDA or CPU) -> L2-norm -> write
(asset_id, vector) into embeddings_siglip2.db (sqlite-vec vec0, FLOAT[1152]).

Resume-safe: a `processed` marker table records asset_id + status so a
kill/restart skips already-embedded (and already-failed) rows. Commits every
COMMIT_EVERY images so a kill loses at most one partial batch.

Usage: /data/loupe-venv/bin/python embed_images_siglip2_full.py
"""

import os
import sqlite3
import struct
import time
import json
import sys
from collections import Counter

import numpy as np
import sqlite_vec

sys.path.insert(0, "/home/david/loupe/stage5")
import recipe_siglip2 as recipe

ONNX_PROVIDER = os.environ.get("LOUPE_ONNX_PROVIDER", "cuda").strip().lower()
if ONNX_PROVIDER == "cpu":
    from ort_env_cpu import make_session
elif ONNX_PROVIDER == "cuda":
    from ort_env import make_session
else:
    raise RuntimeError(
        "LOUPE_ONNX_PROVIDER must be 'cuda' or 'cpu', got %r" % ONNX_PROVIDER
    )

STAGE5_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_DB = os.environ.get("LOUPE_METADATA_DB", "/data/loupe/state/metadata.db")
EMBEDDINGS_DB = os.environ.get(
    "LOUPE_EMBEDDINGS_DB", "/home/david/loupe/stage5/embeddings_siglip2.db"
)
PROGRESS_FILE = os.environ.get(
    "LOUPE_SEMANTIC_PROGRESS", "/data/loupe/state/semantic-refresh.json"
)

COMMIT_EVERY = 75
TERMINAL_UNINDEXABLE_ERRORS = {
    "LibRawFileUnsupportedError",
    "UnidentifiedImageError",
}


def open_embeddings_db():
    con = sqlite3.connect(EMBEDDINGS_DB)
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_images USING vec0(
            asset_id INTEGER PRIMARY KEY,
            embedding FLOAT[{recipe.EMBED_DIM}]
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS processed (
            asset_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            error TEXT,
            ts REAL
        )
    """)
    con.commit()
    return con


def select_all_images():
    con = sqlite3.connect(SNAPSHOT_DB)
    con.row_factory = sqlite3.Row
    cur = con.execute("""
        SELECT id, filepath, mime_type FROM assets
        WHERE mime_type LIKE 'image/%'
        ORDER BY id
    """)
    rows = cur.fetchall()
    con.close()
    return rows


def vec_to_blob(vec):
    return struct.pack(f"{len(vec)}f", *vec.tolist())


def write_progress(done, total, ok, failed, t0):
    elapsed = time.time() - t0
    rate = done / elapsed if elapsed > 0 else 0.0
    remaining = total - done
    eta_s = remaining / rate if rate > 0 else None
    payload = {
        "done": done,
        "total": total,
        "ok": ok,
        "failed": failed,
        "elapsed_s": round(elapsed, 1),
        "rate_per_s": round(rate, 4),
        "eta_s": round(eta_s, 1) if eta_s is not None else None,
        "ts": time.time(),
    }
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, PROGRESS_FILE)


def main():
    all_images = select_all_images()
    total_all = len(all_images)
    print(f"snapshot has {total_all} images total")

    mime_counts = Counter(r["mime_type"] for r in all_images)
    print("mime breakdown:", dict(mime_counts))

    edb = open_embeddings_db()
    current_ids = {r["id"] for r in all_images}
    tracked = {r[0] for r in edb.execute("SELECT asset_id FROM processed")}
    already = {
        r[0]
        for r in edb.execute(
            "SELECT asset_id FROM processed WHERE status IN ('ok','unindexable')"
        )
    }
    stale = tracked - current_ids
    for asset_id in stale:
        edb.execute("DELETE FROM vec_images WHERE asset_id=?", (asset_id,))
        edb.execute("DELETE FROM processed WHERE asset_id=?", (asset_id,))
    edb.commit()
    already -= stale
    todo = [r for r in all_images if r["id"] not in already]
    print(f"{len(already)} current, {len(todo)} missing, {len(stale)} stale removed")

    # A daily refresh on an already-covered library should not load the 1.6 GB
    # visual model. This matters on Vector, where Loupe and Gallery share 8 GB RAM.
    if not todo:
        write_progress(total_all, total_all, total_all, 0, time.time())
        print("coverage already complete; no ONNX session loaded")
        edb.close()
        return

    sess = make_session(recipe.VISUAL_ONNX)
    input_name = sess.get_inputs()[0].name
    print(f"providers: {sess.get_providers()}")

    failures = Counter()
    ok_count = 0
    t0 = time.time()
    done_this_run = 0

    write_progress(len(already), total_all, len(already), 0, t0)

    for row in todo:
        asset_id = row["id"]
        filepath = row["filepath"]
        try:
            arr = recipe.load_and_preprocess_image(filepath)
            batch = arr[np.newaxis, ...]
            out = sess.run(None, {input_name: batch})[0][0]
            vec = recipe.l2_normalize(out)
            edb.execute(
                "INSERT OR REPLACE INTO vec_images(asset_id, embedding) VALUES (?, ?)",
                (asset_id, vec_to_blob(vec)),
            )
            edb.execute(
                "INSERT OR REPLACE INTO processed(asset_id, status, error, ts) VALUES (?, 'ok', NULL, ?)",
                (asset_id, time.time()),
            )
            ok_count += 1
        except Exception as e:
            cause = type(e).__name__
            failures[cause] += 1
            status = (
                "unindexable" if cause in TERMINAL_UNINDEXABLE_ERRORS else "fail"
            )
            edb.execute(
                "INSERT OR REPLACE INTO processed(asset_id, status, error, ts) VALUES (?, ?, ?, ?)",
                (asset_id, status, f"{cause}: {e}", time.time()),
            )

        done_this_run += 1
        if done_this_run % COMMIT_EVERY == 0:
            edb.commit()
            write_progress(
                len(already) + done_this_run, total_all,
                len(already) + ok_count, sum(failures.values()), t0,
            )
            print(
                f"[{len(already) + done_this_run}/{total_all}] "
                f"ok={ok_count} failed={sum(failures.values())} "
                f"rate={done_this_run / (time.time() - t0):.3f}/s",
                flush=True,
            )

    edb.commit()
    write_progress(
        len(already) + done_this_run, total_all,
        len(already) + ok_count, sum(failures.values()), t0,
    )
    elapsed = time.time() - t0

    print(f"\n=== full pass run complete ===")
    print(f"attempted: {len(todo)}  ok: {ok_count}  failed: {sum(failures.values())}")
    print(f"failures by cause: {dict(failures)}")
    print(f"wall-clock: {elapsed:.1f}s  ({elapsed / max(1, len(todo)):.3f}s/image)")
    print(f"providers used: {sess.get_providers()}")
    current_vectors = edb.execute(
        "SELECT count(*) FROM vec_images WHERE asset_id IN "
        "(SELECT asset_id FROM processed WHERE status='ok')"
    ).fetchone()[0]
    current_failures = edb.execute(
        "SELECT count(*) FROM processed WHERE status='fail'"
    ).fetchone()[0]
    unindexable = edb.execute(
        "SELECT count(*) FROM processed WHERE status='unindexable'"
    ).fetchone()[0]
    coverage_ok = (
        current_vectors + unindexable == total_all and current_failures == 0
    )
    print(
        f"coverage_ok={str(coverage_ok).lower()} current_vectors={current_vectors} "
        f"unindexable={unindexable} expected={total_all} "
        f"current_failures={current_failures}"
    )
    edb.close()
    if not coverage_ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
