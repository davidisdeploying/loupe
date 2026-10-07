#!/usr/bin/env python3
"""
faces_pipeline.py — PHASE 1 face/embedding substrate for loupe (INERT: no server
changes, no routes, no UI). Detects faces + computes 512-d ArcFace embeddings and
writes ONLY ~/loupe/faces.db.

Reads originals READ-ONLY (open(fp,'rb')) using metadata.db's filepath — exactly how
server.py resolves originals; never modifies them. metadata.db + apple-enrichment.db
opened mode=ro. Resumable: assets already in `processed` are skipped; 0-face assets
are recorded as processed so resume is clean. Downscales to 1024px long edge.

Usage:
  faces_pipeline.py --ids-file <file>   # pilot: one asset id per line
  faces_pipeline.py --all               # full library image pass
Options: --commit-every N (default 25), --model buffalo_l|buffalo_s,
         --provider <onnxruntime EP> (default CPUExecutionProvider),
         --run-label <tag> (embed provenance stamp, default run-YYYY-MM-DD)
"""
import argparse, io, json, os, sqlite3, sys, time

# Leave one core for loupe + the backup; host-aware, overridable via OMP_NUM_THREADS env.
os.environ.setdefault("OMP_NUM_THREADS", str(max(1, (os.cpu_count() or 4) - 1)))

from PIL import Image, UnidentifiedImageError
from loupe_common import HERE, APP_DATA, METADATA_DB, EXCLUDE_SQL, VIDEO_EXT, ro
FACES_DB = os.path.join(APP_DATA, "faces.db")
MODEL_ROOT = os.path.join(HERE, ".insightface")   # model weights ship with the install
LONG_EDGE = 1024

try:
    import rawpy
    RAW_SUPPORTED = True
except ImportError:
    RAW_SUPPORTED = False


def _load_raw_image(fp):
    """Decode a camera raw file (CR2/CR3/DNG/...) via rawpy. Prefers the
    embedded JPEG preview for speed; falls back to a full raw postprocess."""
    if not RAW_SUPPORTED:
        raise RuntimeError("rawpy not installed -- cannot decode raw file")
    with rawpy.imread(fp) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                return Image.open(io.BytesIO(thumb.data))
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            pass
        rgb = raw.postprocess()
    return Image.fromarray(rgb)


def init_faces_db():
    db = sqlite3.connect(FACES_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS faces(
        face_id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER NOT NULL,
        bbox_json TEXT,
        det_score REAL,
        embedding BLOB,            /* float32 x 512 */
        created_at INTEGER NOT NULL,
        embed_model TEXT,          /* provenance: which pack produced the embedding */
        embed_det INTEGER,         /* det_size / long-edge used at inference */
        embed_provider TEXT,       /* onnxruntime execution provider */
        embed_run TEXT);           /* run label, e.g. 'stills-2026-06-16' */
    CREATE TABLE IF NOT EXISTS persons(
        person_id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE,
        is_protected INTEGER DEFAULT 0,
        source TEXT,
        created_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS assignments(
        face_id INTEGER PRIMARY KEY,
        person_id INTEGER NOT NULL,
        source TEXT,               /* 'manual' | 'propagated' | 'apple' */
        confidence REAL,
        confirmed INTEGER DEFAULT 0,
        updated_at INTEGER NOT NULL);
    /* resume bookkeeping: every visited asset lands here, incl. 0-face ones */
    CREATE TABLE IF NOT EXISTS processed(
        asset_id INTEGER PRIMARY KEY,
        n_faces INTEGER NOT NULL,
        processed_at INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_faces_asset ON faces(asset_id);
    CREATE INDEX IF NOT EXISTS idx_assign_person ON assignments(person_id);
    """)
    # Pre-provenance DBs lack the embed_* columns; ADD COLUMN is additive.
    have = {r[1] for r in db.execute("PRAGMA table_info(faces)")}
    for col, typ in (("embed_model", "TEXT"), ("embed_det", "INTEGER"),
                     ("embed_provider", "TEXT"), ("embed_run", "TEXT")):
        if col not in have:
            db.execute(f"ALTER TABLE faces ADD COLUMN {col} {typ}")
    db.commit()
    return db


def resolve_provider(requested):
    """Turn "auto" into the best execution provider actually available.

    The stamped value goes into faces.embed_provider as provenance, so it must be the
    provider really used, never the one asked for. faces.db already holds a mix --
    CUDA and CPU embeddings of the same buffalo_l model side by side -- so the two are
    treated as interchangeable for matching; "auto" is about not idling a GPU, not about
    changing the model.

    Falls back to CPU rather than failing: a slow run beats no run.
    """
    if requested != "auto":
        return requested
    try:
        import onnxruntime as ort
        available = ort.get_available_providers()
    except Exception:
        return "CPUExecutionProvider"
    for candidate in ("CUDAExecutionProvider", "CPUExecutionProvider"):
        if candidate in available:
            return candidate
    return "CPUExecutionProvider"


def load_model(name, provider):
    import pillow_heif
    pillow_heif.register_heif_opener()
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name=name, root=MODEL_ROOT, providers=[provider])
    app.prepare(ctx_id=0 if provider != "CPUExecutionProvider" else -1,
                det_size=(LONG_EDGE, LONG_EDGE))
    return app


def asset_list(args, processed):
    conn = ro(METADATA_DB)
    if args.ids_file:
        ids = [int(x) for x in open(args.ids_file).read().split() if x.strip()]
        qm = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, filepath, extension FROM assets WHERE id IN ({qm})", ids).fetchall()
        order = {i: n for n, i in enumerate(ids)}
        rows = sorted(rows, key=lambda r: order.get(r["id"], 0))
    else:
        # VIDEO_EXT is a frozenset (loupe_common); bind it via placeholders rather than
        # interpolating the container repr into the SQL. sorted() yields a list (sqlite
        # rejects a set as params) of upper-case exts, matching upper(extension).
        _vq = ",".join("?" * len(VIDEO_EXT))
        rows = conn.execute(
            f"SELECT id, filepath, extension FROM assets "
            f"WHERE upper(extension) NOT IN ({_vq}) AND {EXCLUDE_SQL} "
            f"ORDER BY id", sorted(VIDEO_EXT)).fetchall()
    conn.close()
    out = []
    for r in rows:
        if r["id"] in processed:
            continue
        if (r["extension"] or "").upper() in VIDEO_EXT:
            continue                       # images only this phase
        out.append((r["id"], r["filepath"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids-file")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--commit-every", type=int, default=25)
    ap.add_argument("--model", default="buffalo_l")
    ap.add_argument("--provider", default="CPUExecutionProvider",
                    help="onnxruntime execution provider, or 'auto' to prefer CUDA when "
                         "it is really available (also stamped as provenance)")
    ap.add_argument("--run-label", default=time.strftime("run-%Y-%m-%d"),
                    help="embed_run provenance label stamped on every inserted face")
    args = ap.parse_args()
    # Resolve BEFORE the model loads, so the stamped provenance is what actually ran.
    args.provider = resolve_provider(args.provider)

    import numpy as np

    db = init_faces_db()
    processed = {r[0] for r in db.execute("SELECT asset_id FROM processed")}
    todo = asset_list(args, processed)
    print(f"[faces] model={args.model} todo={len(todo)} (already processed={len(processed)})",
          flush=True)

    app = load_model(args.model, args.provider)
    print(f"[faces] model ready (provider={args.provider} run={args.run_label})", flush=True)

    t_start = time.time()
    done = 0
    n_faces_total = 0
    for idv, fp in todo:
        n = 0
        try:
            with open(fp, "rb") as f:           # READ-ONLY original
                data = f.read()
            try:
                im = Image.open(io.BytesIO(data))
            except UnidentifiedImageError:
                im = _load_raw_image(fp)
            im = im.convert("RGB")
            im.thumbnail((LONG_EDGE, LONG_EDGE))
            w, h = im.size
            arr = np.asarray(im)[:, :, ::-1]    # RGB -> BGR for insightface
            faces = app.get(arr)
            now = int(time.time())
            for fc in faces:
                x1, y1, x2, y2 = [float(v) for v in fc.bbox]
                bbox = json.dumps({"x1": round(x1, 1), "y1": round(y1, 1),
                                   "x2": round(x2, 1), "y2": round(y2, 1),
                                   "img_w": w, "img_h": h})
                emb = np.asarray(fc.embedding, dtype=np.float32).tobytes()
                db.execute(
                    "INSERT INTO faces(asset_id,bbox_json,det_score,embedding,created_at,"
                    "embed_model,embed_det,embed_provider,embed_run) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (idv, bbox, float(fc.det_score), emb, now,
                     args.model, LONG_EDGE, args.provider, args.run_label))
                n += 1
        except Exception as e:
            print(f"[faces] WARN id={idv}: {type(e).__name__}: {e}", flush=True)
        db.execute("INSERT OR REPLACE INTO processed(asset_id,n_faces,processed_at) "
                   "VALUES(?,?,?)", (idv, n, int(time.time())))
        n_faces_total += n
        done += 1
        if done % args.commit_every == 0:
            db.commit()
            rate = (time.time() - t_start) / done
            print(f"[faces] {done}/{len(todo)} assets · {n_faces_total} faces · "
                  f"{rate:.2f}s/asset · ETA {rate*(len(todo)-done)/3600:.1f}h", flush=True)
    db.commit()
    elapsed = time.time() - t_start
    print(f"[faces] DONE {done} assets · {n_faces_total} faces · "
          f"{elapsed:.0f}s · {elapsed/max(done,1):.2f}s/asset", flush=True)
    db.close()


if __name__ == "__main__":
    main()
