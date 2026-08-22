#!/usr/bin/env python3
"""
video_face_pass.py — Stage 2a: portable video-face embedding pass (Worker5 / charlie).
TOKEN: FLEET-WORKER5-BUILD-20260705-video-face-pass

Produces PORTABLE buffalo_l face embeddings for the ~26,983 NON-Live-Photo videos
so protected people can be identified in videos without Apple. Writes NOTHING to
faces.db — emits a portable npz+jsonl export bundle for the Stage-2b importer.

Scope   : metadata.db video assets with is_live_photo_video=0, joined to the
          charlie-accessible NAS paths in video_signals.db by file_sha256 (robust
          against mount-path drift; the join key is content, not location).
Model   : insightface buffalo_l, CUDAExecutionProvider, ctx_id=0, det_size=1024.
Sampling: SEEK-based (-ss per point, NOT the fps filter). 1 frame / 5s, cap 6,
          floor 1; 1 frame / 4s for clips < 30s. NVDEC decode (cuvid), CPU fallback.
Contract: every visited asset recorded (incl 0-face). Embeddings RAW un-normalized
          exactly as insightface emits. NO face_ids (the importer autoincrements).

Resume-safe: progress.db (asset_id PK) is the done-ledger; a batch is flushed
atomically (npz shard -> jsonl append -> progress mark), so done <=> in a shard.
"""
import os, sys, json, time, glob, sqlite3, hashlib, subprocess, threading, queue, math
import numpy as np

# ---------------------------------------------------------------- config
HOME       = os.path.expanduser("~")
BASE       = os.environ.get("LOUPE_VIDEO_BASE", os.path.join(HOME, "loupe-ml", "video"))
OUT_DIR    = os.environ.get("VF_OUT_DIR", os.path.join(BASE, "video-faces"))
EXPORT_DIR = os.path.join(OUT_DIR, "export")
PROG_DB    = os.path.join(OUT_DIR, "progress.db")
VS_DB      = os.path.join(BASE, "video_signals.db")
# Read-only, and only for (id, file_sha256) -- paths come from VS_DB via sha, so this
# is content-addressed. Was a 2026-07-05 snapshot of delta's metadata.db, which
# had drifted 64 assets (24 of them videos) behind live and outlived that host.
META_DB    = os.environ.get("LOUPE_VIDEO_META_DB", "/data/loupe/state/metadata.db")
LOG_PATH   = os.path.join(OUT_DIR, "video_face_pass.log")
PROGRESS_FILE = os.path.join(OUT_DIR, "progress.count")   # fleet_run --progress-file
METRICS_FILE  = os.path.join(OUT_DIR, "metrics.json")     # fleet_run --metrics-file

GPU_UUID   = "GPU-00000000-0000-0000-0000-000000000000"   # the ML GPU (GPU0)
MODEL_ROOT = os.environ.get("LOUPE_INSIGHTFACE_ROOT", "/data/loupe/models/insightface")
DET_SIZE   = 1024
LONG_EDGE  = 1920                # cap frame long edge (never upscale) for detection
N_EXTRACT_WORKERS = int(os.environ.get("VF_WORKERS", "3"))   # NAS-capped decode workers
BATCH_ASSETS = 250              # flush a shard every N processed assets
FFTHREADS  = 2
SEEK_TIMEOUT = 120              # per-frame extraction timeout (s)

# provenance stamp (verbatim into the export — must match Worker1's importer)
PROV = dict(embed_model="buffalo_l", embed_det=DET_SIZE,
            embed_provider="CUDAExecutionProvider", embed_run="video-2026-07-05")

# thermal safety net (card had an Xid-79 fall-off-bus on 2026-07-01; decode-bound
# pass sits cold ~44C, so this is a backstop only)
SOFT_C, HARD_C = 80, 83

CUVID = {"hevc": "hevc_cuvid", "h264": "h264_cuvid", "av1": "av1_cuvid",
         "vp9": "vp9_cuvid", "vp8": "vp8_cuvid", "mpeg4": "mpeg4_cuvid",
         "mpeg2video": "mpeg2_cuvid", "mpeg1video": "mpeg1_cuvid",
         "vc1": "vc1_cuvid", "mjpeg": "mjpeg_cuvid"}
FF_ENV = dict(os.environ, CUDA_VISIBLE_DEVICES=GPU_UUID)

_log_lock = threading.Lock()
def log(msg):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_PATH, "a") as f: f.write(line + "\n")
        except OSError: pass

# ---------------------------------------------------------------- worklist / progress
def init_progress():
    os.makedirs(EXPORT_DIR, exist_ok=True)
    c = sqlite3.connect(PROG_DB)
    c.execute("""CREATE TABLE IF NOT EXISTS assets(
        asset_id INTEGER PRIMARY KEY, sha TEXT, src_path TEXT, dur REAL,
        vcodec TEXT, status TEXT, n_faces INTEGER, shard INTEGER, ts TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_status ON assets(status)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_sha ON assets(sha)")
    c.commit()
    return c

def build_worklist(c):
    """One-time: join metadata(non-live videos) x video_signals by sha -> todo rows."""
    already = c.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    if already:
        log(f"worklist: {already} assets already present, skipping build")
        return
    vs = sqlite3.connect(f"file:{VS_DB}?mode=ro", uri=True)
    sha_meta = {}   # sha -> (path, dur, vcodec) representative from video_signals
    for path, sha, dur, vc in vs.execute(
            "SELECT path,file_sha256,duration_s,vcodec FROM videos "
            "WHERE file_sha256 IS NOT NULL"):
        if sha not in sha_meta:
            sha_meta[sha] = (path, dur, vc)
    md = sqlite3.connect(f"file:{META_DB}?mode=ro", uri=True)
    rows, miss = [], 0
    for aid, sha in md.execute(
            "SELECT id,file_sha256 FROM assets "
            "WHERE lower(extension) IN ('mov','mp4','m4v') "
            "AND COALESCE(is_live_photo_video,0)=0 "
            "AND file_sha256 IS NOT NULL"):
        m = sha_meta.get(sha)
        if not m:
            miss += 1; continue
        path, dur, vc = m
        rows.append((aid, sha, path, dur, vc, "todo", None, None, None))
    c.executemany("INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?,?,?,?)", rows)
    c.commit()
    log(f"worklist built: {len(rows)} non-Live video assets (sha-unmatched={miss})")

# ---------------------------------------------------------------- sampling
def sample_times(dur):
    if not dur or dur <= 0:
        return [0.0]
    interval = 4.0 if dur < 30 else 5.0
    n = min(6, max(1, math.ceil(dur / interval)))
    return [round(dur * (i + 0.5) / n, 3) for i in range(n)]

# ---------------------------------------------------------------- frame extraction
_SCALE = (f"scale=w='min({LONG_EDGE},iw)':h='min({LONG_EDGE},ih)'"
          f":force_original_aspect_ratio=decrease")

def _extract_one(path, t, decode_args):
    """Seek to t, decode+download one autorotated BGR frame. Returns np.ndarray or None."""
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "error",
           "-threads", str(FFTHREADS), "-ss", f"{t:.3f}", *decode_args, "-i", path,
           "-map", "0:v:0", "-frames:v", "1", "-vf", _SCALE,
           "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "-"]
    p = subprocess.run(cmd, capture_output=True, timeout=SEEK_TIMEOUT, env=FF_ENV)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError((p.stderr[-180:] or b"empty").decode("utf-8", "ignore"))
    import cv2
    arr = cv2.imdecode(np.frombuffer(p.stdout, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise RuntimeError("cv2.imdecode returned None")
    return arr

def extract_frames(path, dur, vcodec):
    """Return (frames[list of BGR arrays], err). NVDEC first, CPU fallback."""
    times = sample_times(dur)
    dec = CUVID.get(vcodec)
    for decode_args in ([["-hwaccel", "cuda", "-hwaccel_device", "0", "-c:v", dec]]
                        if dec else []) + [[]]:
        frames, err = [], None
        try:
            for t in times:
                frames.append((_extract_one(path, t, decode_args), t))
            return frames, None
        except Exception as e:
            err = str(e)[:200]
            frames = None
            continue
    return None, err

# ---------------------------------------------------------------- pipeline
def read_temp():
    try:
        out = subprocess.run(["nvidia-smi", f"--id={GPU_UUID}",
                              "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        return int(out.splitlines()[0])
    except Exception:
        return None

def main():
    t_start = time.time()
    log(f"=== video_face_pass start (workers={N_EXTRACT_WORKERS}, batch={BATCH_ASSETS}) ===")
    c = init_progress()
    build_worklist(c)

    todo = c.execute("SELECT asset_id,sha,src_path,dur,vcodec FROM assets "
                     "WHERE status IS NULL OR status='todo' OR status='error' "
                     "ORDER BY sha").fetchall()
    total_all = c.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
    done_already = c.execute("SELECT COUNT(*) FROM assets WHERE status='done'").fetchone()[0]
    log(f"scope: {total_all} assets, {done_already} already done, {len(todo)} to process")

    # group by sha so byte-identical duplicates decode once (34 dup shas fleet-wide)
    groups = {}   # sha -> {"meta":(path,dur,vcodec), "aids":[...]}
    for aid, sha, path, dur, vc in todo:
        g = groups.setdefault(sha, {"meta": (path, dur, vc), "aids": []})
        g["aids"].append(aid)
    work = list(groups.items())
    _lim = int(os.environ.get("VF_LIMIT", "0"))
    if _lim:
        work = work[:_lim]
        log(f"VF_LIMIT set — restricting to {len(work)} sha-groups (SMOKE TEST)")
    log(f"{len(work)} distinct sha-groups to decode")

    # existing shard high-water mark (resume)
    shard_no = len(glob.glob(os.path.join(EXPORT_DIR, "faces_shard_*.npz")))
    processed_file = os.path.join(EXPORT_DIR, "assets_processed.jsonl")

    q = queue.Queue(maxsize=N_EXTRACT_WORKERS * 4)
    stop = threading.Event()

    def producer(items):
        for sha, g in items:
            if stop.is_set(): break
            path, dur, vc = g["meta"]
            frames, err = extract_frames(path, dur, vc)
            q.put((sha, g["aids"], path, dur, frames, err))

    # split groups across producer threads
    workers = []
    for w in range(N_EXTRACT_WORKERS):
        chunk = work[w::N_EXTRACT_WORKERS]
        th = threading.Thread(target=producer, args=(chunk,), daemon=True)
        th.start(); workers.append(th)

    # GPU consumer (this thread) — one resident model, serialized inference
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_l", root=MODEL_ROOT,
                       providers=["CUDAExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(DET_SIZE, DET_SIZE))
    log(f"buffalo_l ready on {app.models['detection'].session.get_providers()[0]} "
        f"det_size={DET_SIZE} (VRAM warm)")

    # batch buffers
    b_aid, b_emb, b_bbox, b_wh, b_t, b_score = [], [], [], [], [], []
    b_processed = []           # per-asset processed records (incl 0-face)
    n_in_batch = 0
    done_count = done_already
    faces_total = 0
    err_count = 0

    def flush_batch():
        nonlocal shard_no, n_in_batch, done_count
        if not b_processed:
            return
        if b_aid:
            shard_path = os.path.join(EXPORT_DIR, f"faces_shard_{shard_no:04d}.npz")
            np.savez(shard_path,
                     asset_id=np.array(b_aid, dtype=np.int64),
                     embedding=np.array(b_emb, dtype=np.float32),
                     bbox=np.array(b_bbox, dtype=np.float32),      # x1,y1,x2,y2
                     img_wh=np.array(b_wh, dtype=np.int32),        # img_w,img_h
                     t=np.array(b_t, dtype=np.float32),
                     det_score=np.array(b_score, dtype=np.float32),
                     **{k: PROV[k] for k in PROV})
        # append processed manifest (every visited asset, incl 0-face)
        with open(processed_file, "a") as pf:
            for rec in b_processed:
                pf.write(json.dumps(rec) + "\n")
        # mark done in progress db (one txn) — done <=> in a flushed shard
        c.executemany(
            "UPDATE assets SET status='done',n_faces=?,shard=?,ts=? WHERE asset_id=?",
            [(r["n_faces"], shard_no if b_aid else None,
              time.strftime('%Y-%m-%dT%H:%M:%S'), r["asset_id"]) for r in b_processed])
        c.commit()
        done_count += len(b_processed)
        with open(PROGRESS_FILE, "w") as f: f.write(str(done_count))
        with open(METRICS_FILE, "w") as f:
            json.dump({"done": done_count, "total": total_all, "faces": faces_total,
                       "errors": err_count, "shard": shard_no}, f)
        log(f"flush shard {shard_no:04d}: +{len(b_processed)} assets "
            f"({sum(1 for r in b_processed if r['n_faces']>0)} w/faces), "
            f"done={done_count}/{total_all} faces_total={faces_total} err={err_count}")
        shard_no += (1 if b_aid else 0)
        for buf in (b_aid, b_emb, b_bbox, b_wh, b_t, b_score, b_processed): buf.clear()
        n_in_batch = 0

    seen = 0
    n_groups = len(work)
    while seen < n_groups:
        try:
            sha, aids, path, dur, frames, err = q.get(timeout=300)
        except queue.Empty:
            if all(not th.is_alive() for th in workers):
                break
            continue
        seen += 1
        if frames is None:
            # decode failed after NVDEC+CPU — leave as 'error' for a future resume
            err_count += len(aids)
            log(f"decode-error [{os.path.basename(path)}] {err}")
            continue
        # detect+embed across sampled frames
        asset_faces = []
        for arr, t in frames:
            h, w = arr.shape[:2]
            for f in app.get(arr):
                x1, y1, x2, y2 = [float(v) for v in f.bbox]
                asset_faces.append((f.embedding.astype(np.float32),
                                    (x1, y1, x2, y2), (w, h), float(t),
                                    float(f.det_score)))
        # emit for every asset_id sharing this sha (dedup decode, per-asset records)
        for aid in aids:
            nf = len(asset_faces)
            for emb, bbox, wh, t, sc in asset_faces:
                b_aid.append(aid); b_emb.append(emb); b_bbox.append(bbox)
                b_wh.append(wh); b_t.append(t); b_score.append(sc)
            faces_total += nf
            b_processed.append({"asset_id": aid, "n_faces": nf, "sha": sha})
            n_in_batch += 1
        if n_in_batch >= BATCH_ASSETS:
            if read_temp() and read_temp() >= HARD_C:
                log(f"thermal HARD>={HARD_C}C — pausing 60s"); time.sleep(60)
            flush_batch()

    flush_batch()
    stop.set()
    dt = time.time() - t_start
    # final manifest
    manifest = dict(token="FLEET-WORKER5-BUILD-20260705-video-face-pass",
                    provenance=PROV, sampling="1fr/5s cap6 floor1; 1fr/4s <30s; seek-based",
                    total_assets=total_all, done=done_count, faces_total=faces_total,
                    errors=err_count, shards=shard_no,
                    finished_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    wall_seconds=round(dt, 1))
    with open(os.path.join(EXPORT_DIR, "MANIFEST.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    log(f"=== DONE done={done_count}/{total_all} faces={faces_total} err={err_count} "
        f"shards={shard_no} wall={dt/3600:.2f}h ===")

if __name__ == "__main__":
    main()
