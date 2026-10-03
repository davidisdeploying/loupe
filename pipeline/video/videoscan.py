#!/usr/bin/env python3
"""
videoscan.py — video-cull signal pipeline v2 (NVDEC + fused single-decode Pass A).

Runs on Worker5/charlie. Reads video bytes READ-ONLY from the NAS; writes ONLY to
~/loupe-ml/video/ (working dir + video_signals.db + frames/). Does NOT touch
metadata.db, the NAS files, or the Loupe repos.

v2 changes vs v1:
  1. NVDEC hardware decode (hevc_cuvid / h264_cuvid / ...) pinned to GPU0, CPU
     fallback for codecs cuvid can't handle (flagged).
  2. ONE decode per file produces BOTH the backbone signals (black/freeze/
     silence/signalstats) AND the adaptive 1/2/4/6 VLM frames (autorotate ON,
     longest edge 768 px, JPEG). Frames are persisted to frames/ in Pass A and
     consumed by Pass B — the NAS file is opened/read exactly once per file.
  3. Long/4K tail is seek-sampled (M windows) instead of full-decoded, killing
     the multi-GB 4K timeouts. ffmpeg -threads capped so N workers don't starve.

Stdlib only + external binaries ffprobe/ffmpeg 8.0.1 + local ollama (qwen2.5vl:7b).

Signal store is PATH-KEYED. Reconcile to metadata.db `asset_id` is a deferred join.

Subcommands:
    init-db                 create / migrate the sqlite schema
    passa   <file>          run fused Pass-A for one file, print JSON (debug)
    passb   <file> [n]      print Pass-B VLM tags for one file as JSON (debug)
"""
import sys, os, json, sqlite3, subprocess, hashlib, base64, time, re, glob, urllib.request

HOME = os.path.expanduser("~")
BASE = os.environ.get("LOUPE_VIDEO_BASE", os.path.join(HOME, "loupe-ml", "video"))
DB_PATH = os.path.join(BASE, "video_signals.db")
FRAME_DIR = os.path.join(BASE, "frames")

def _gpu_uuid() -> str:
    """Which card to pin decode/inference to.

    Machine configuration, not source: set LOUPE_GPU_UUID, or write the
    uuid to ~/.config/loupe/gpu-uuid. Empty means "let CUDA choose",
    which is the right default on a single-GPU box.
    """
    value = os.environ.get("LOUPE_GPU_UUID", "").strip()
    if value:
        return value
    conf = os.path.join(os.path.expanduser("~"), ".config", "loupe", "gpu-uuid")
    if os.path.exists(conf):
        with open(conf) as fh:
            return fh.read().strip()
    return ""


GPU_UUID = _gpu_uuid()
OLLAMA = "http://127.0.0.1:11434"
VLM_MODEL = "qwen2.5vl:7b"

ANALYZE_FPS = 3          # Pass-A filter sampling rate (backbone signals)
BLACK_THRESH = 0.9       # skip full VLM sampling above this black_ratio / frozen_ratio
FRAME_LONG_EDGE = 768    # VLM frame longest-edge px
FFTHREADS = 2            # per-worker ffmpeg -threads cap (8 workers must not starve)

# --- tail (seek-sample) routing + params ---
TAIL_DUR_S   = 300       # any clip longer than this -> seek-sample
TAIL_4K_DUR  = 90        # 4K clips longer than this -> seek-sample
SEEK_WINDOWS = 12        # M signal-sample windows across the clip
SEEK_WIN_S   = 2.0       # each window duration (decoded @ ANALYZE_FPS)

# codec_name (ffprobe) -> cuvid decoder. Anything not here -> CPU fallback (flagged).
CUVID = {"hevc": "hevc_cuvid", "h264": "h264_cuvid", "av1": "av1_cuvid",
         "vp9": "vp9_cuvid", "vp8": "vp8_cuvid", "mpeg4": "mpeg4_cuvid",
         "mpeg2video": "mpeg2_cuvid", "mpeg1video": "mpeg1_cuvid",
         "vc1": "vc1_cuvid", "mjpeg": "mjpeg_cuvid"}

# Pinned environment for every ffmpeg that may touch NVDEC. With no uuid
# configured, leave CUDA_VISIBLE_DEVICES alone — setting it to "" would
# hide every GPU rather than selecting one.
FF_ENV = (dict(os.environ, CUDA_VISIBLE_DEVICES=GPU_UUID) if GPU_UUID
          else dict(os.environ))

# storage-vs-decoder fallback (2026-07-04): the NAS is a soft CIFS/SMB mount
# (/mnt/nas2) that returns EIO/ESTALE FAST on a stall (it does not hang). Such a
# stall can fail an otherwise-healthy NVDEC decode. Those errors are retried on the
# SAME decoder a bounded number of times before giving up to a CPU fallback, so a
# transient network hiccup neither loses the clip nor masquerades as a codec fault.
# A genuine unsupported-codec error does NOT match this signature, so it skips the
# retry and falls to CPU immediately as before.
STORAGE_RETRIES   = 3      # bounded same-decoder retries on a fast EIO stall
STORAGE_BACKOFF_S = 2.0    # short backoff between retries (EIO returns fast, no hang)
_STORAGE_ERR_RE = re.compile(
    r"input/output error|i/o error|errno 5|\beio\b|stale file handle|estale|"
    r"not respond|timed? ?out|connection reset|host is down|broken pipe|"
    r"resource temporarily unavailable|transport endpoint|no such device",
    re.I)

def _is_storage_error(e):
    """True for a fast CIFS/EIO stall worth retrying on the same decoder. A hard
    timeout (process hung) is NOT retried — that would burn N× the timeout."""
    if isinstance(e, subprocess.TimeoutExpired):
        return False
    return bool(_STORAGE_ERR_RE.search(str(e)))

# ---------------------------------------------------------------- schema

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    path            TEXT PRIMARY KEY,
    size_bytes      INTEGER,
    duration_s      REAL,
    width           INTEGER,
    height          INTEGER,
    fps             REAL,
    vcodec          TEXT,
    container       TEXT,
    rotation        INTEGER,
    has_audio       INTEGER,
    acodec          TEXT,
    -- backbone flags --
    is_sub2s        INTEGER,
    black_ratio     REAL,
    frozen_ratio    REAL,
    silence_ratio   REAL,
    brightness_mean REAL,
    exact_dup_group INTEGER,
    -- vlm --
    vlm_caption     TEXT,
    vlm_setting     TEXT,
    vlm_scene       TEXT,
    vlm_people      TEXT,
    vlm_activities  TEXT,   -- json
    vlm_objects     TEXT,   -- json
    vlm_flags       TEXT,   -- json
    vlm_quality_note TEXT,
    vlm_model       TEXT,
    n_frames        INTEGER,
    vlm_ms          INTEGER,
    vlm_raw         TEXT,   -- raw model text kept only on parse failure
    parse_error     INTEGER DEFAULT 0,
    -- provenance --
    dup_partial_sha TEXT,
    passa_ms        INTEGER,
    passa_error     TEXT,
    passb_error     TEXT,
    -- v2 provenance --
    decoder         TEXT,   -- nvdec decoder used, or 'cpu'
    cpu_fallback    INTEGER DEFAULT 0,
    passa_method    TEXT,   -- 'full' | 'seek'
    sample          INTEGER DEFAULT 0,
    scanned_at      TEXT
);
"""

# columns added in v2 — migrate an existing v1 db in place
V2_COLS = {"decoder": "TEXT", "cpu_fallback": "INTEGER DEFAULT 0",
           "passa_method": "TEXT"}

def connect(path=DB_PATH):
    con = sqlite3.connect(path, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con

def init_db(path=DB_PATH):
    con = connect(path)
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(videos)")}
    for col, decl in V2_COLS.items():
        if col not in have:
            con.execute(f"ALTER TABLE videos ADD COLUMN {col} {decl}")
    con.commit()
    con.close()

# ---------------------------------------------------------------- ffprobe metadata

def ffprobe_meta(path):
    """Return technical metadata dict via ffprobe (stdlib subprocess)."""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_entries",
           "format=duration,size,format_name:stream=index,codec_type,codec_name,"
           "width,height,r_frame_rate,side_data_list:stream_tags=rotate",
           "-show_streams", path]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError("ffprobe failed: " + out.stderr.strip()[:200])
    d = json.loads(out.stdout)
    fmt = d.get("format", {})
    m = {"size_bytes": int(fmt.get("size", 0) or 0),
         "duration_s": float(fmt.get("duration", 0) or 0),
         "container": fmt.get("format_name"),
         "width": None, "height": None, "fps": None, "vcodec": None,
         "rotation": 0, "has_audio": 0, "acodec": None}
    for s in d.get("streams", []):
        ct = s.get("codec_type")
        if ct == "video" and m["vcodec"] is None:
            m["vcodec"] = s.get("codec_name")
            m["width"] = s.get("width")
            m["height"] = s.get("height")
            rf = s.get("r_frame_rate", "0/0")
            try:
                n, den = rf.split("/")
                m["fps"] = round(float(n) / float(den), 3) if float(den) else None
            except Exception:
                m["fps"] = None
            rot = 0
            for sd in s.get("side_data_list", []) or []:
                if "rotation" in sd:
                    try:
                        rot = int(round(float(sd["rotation"])))
                    except Exception:
                        pass
            tagrot = (s.get("tags", {}) or {}).get("rotate")
            if rot == 0 and tagrot is not None:
                try:
                    rot = int(round(float(tagrot)))
                except Exception:
                    pass
            m["rotation"] = rot
        elif ct == "audio" and not m["has_audio"]:
            m["has_audio"] = 1
            m["acodec"] = s.get("codec_name")
    return m

# ---------------------------------------------------------------- signal parsing

_FREEZE_START_RE = re.compile(r"freeze_start:\s*([0-9.]+)")
_FREEZE_DUR_RE   = re.compile(r"freeze_duration:\s*([0-9.]+)")
_SIL_START_RE    = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SIL_DUR_RE      = re.compile(r"silence_duration:\s*([0-9.]+)")

def _sum_durations(stderr, dur_re, start_re, clip_dur):
    """Sum interval durations; close an unterminated interval (runs to EOF) at clip_dur."""
    total = sum(float(x) for x in dur_re.findall(stderr))
    starts = start_re.findall(stderr)
    ends = dur_re.findall(stderr)
    if len(starts) > len(ends):
        try:
            open_start = float(starts[-1])
            if clip_dur and clip_dur > open_start:
                total += (clip_dur - open_start)
        except Exception:
            pass
    return total

def _parse_meta_text(text):
    """From a metadata=print dump: (total_frames, black_frames, [YAVG...])."""
    total = text.count("frame:")
    black = text.count("lavfi.blackframe.pblack")
    yavgs = [float(x) for x in re.findall(r"lavfi\.signalstats\.YAVG=([0-9.]+)", text)]
    return total, black, yavgs

# ---------------------------------------------------------------- Pass A (fused)

def frame_count_for(dur):
    if dur is None:            return 1
    if dur < 10:               return 1
    if dur < 60:               return 2
    if dur <= 300:             return 4
    return 6

def _frame_scale():
    return (f"scale='if(gt(iw,ih),{FRAME_LONG_EDGE},-2)':"
            f"'if(gt(iw,ih),-2,{FRAME_LONG_EDGE})'")

def _is_tail(meta):
    dur = meta.get("duration_s") or 0.0
    longedge = max(meta.get("width") or 0, meta.get("height") or 0)
    is4k = longedge >= 3840
    return dur > TAIL_DUR_S or (is4k and dur > TAIL_4K_DUR)

def _decoder_args(meta, force_cpu=False):
    """Return (ffmpeg input-decode args, decoder_label). NVDEC when supported."""
    dec = None if force_cpu else CUVID.get(meta.get("vcodec"))
    if dec:
        return (["-hwaccel", "cuda", "-hwaccel_device", "0", "-c:v", dec], dec)
    return ([], "cpu")

def _run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=FF_ENV)

def _key_for(path):
    return hashlib.sha1(path.encode()).hexdigest()[:16]

def _clear_frames(prefix):
    for fp in glob.glob(prefix + "_*.jpg"):
        try: os.remove(fp)
        except OSError: pass

# ---- full-decode fused pass (non-tail) ----

def _passa_full(path, meta, prefix, decode_args):
    """Single ffmpeg: decode once -> signals (null) + N frames (jpg) + silence.
    Returns (signals_dict, frame_paths)."""
    dur = meta.get("duration_s") or 0.0
    has_audio = bool(meta.get("has_audio"))
    n = frame_count_for(dur)
    metafile = prefix + ".meta"
    _clear_frames(prefix)
    if os.path.exists(metafile):
        try: os.remove(metafile)
        except OSError: pass

    sig_vf = (f"fps={ANALYZE_FPS},blackframe=amount=98:thresh=32,"
              f"freezedetect=n=0.001:d=0.5,signalstats,metadata=print:file={metafile}")
    frame_fps = max(n / dur, 1e-6) if dur else 1.0
    frame_vf = f"fps={frame_fps},{_frame_scale()}"

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
           "-threads", str(FFTHREADS), *decode_args, "-i", path,
           # output 1: backbone signals
           "-map", "0:v:0", "-an", "-vf", sig_vf, "-f", "null", "-",
           # output 2: adaptive VLM frames (autorotate ON via simple -vf)
           "-map", "0:v:0", "-vf", frame_vf, "-frames:v", str(n),
           "-vsync", "0", "-q:v", "3", "-y", prefix + "_%d.jpg"]
    if has_audio:
        cmd += ["-map", "0:a:0?", "-af", "silencedetect=n=-30dB:d=1.0", "-f", "null", "-"]

    p = _run(cmd, timeout=1800)
    stderr = p.stderr
    meta_txt = ""
    if os.path.exists(metafile):
        with open(metafile, errors="ignore") as f:
            meta_txt = f.read()
        try: os.remove(metafile)
        except OSError: pass

    total, black, yavgs = _parse_meta_text(meta_txt)
    black_ratio = (black / total) if total else None
    brightness_mean = (sum(yavgs) / len(yavgs)) if yavgs else None
    frozen_dur = _sum_durations(stderr, _FREEZE_DUR_RE, _FREEZE_START_RE, dur)
    frozen_ratio = min(frozen_dur / dur, 1.0) if dur else None
    if has_audio:
        sil_dur = _sum_durations(stderr, _SIL_DUR_RE, _SIL_START_RE, dur)
        silence_ratio = min(sil_dur / dur, 1.0) if dur else None
    else:
        silence_ratio = None

    frames = sorted(glob.glob(prefix + "_*.jpg"))
    if p.returncode != 0 and not frames and total == 0:
        raise RuntimeError("ffmpeg full-decode failed: " + stderr.strip()[-200:])

    sig = _mk_sig(dur, black_ratio, frozen_ratio, silence_ratio, brightness_mean,
                  total, "full")
    return sig, frames

# ---- seek-sample pass (tail: >5m or long 4K) ----

def _passa_seek(path, meta, prefix, decode_args):
    """M window-seeks for signals (+ frames at N selected windows). Reads a small
    fraction of the file instead of full-decoding multi-GB 4K. Returns (sig, frames)."""
    dur = meta.get("duration_s") or 0.0
    has_audio = bool(meta.get("has_audio"))
    n = frame_count_for(dur)
    M = SEEK_WINDOWS
    _clear_frames(prefix)
    # window centres, evenly spaced; frame slots = n evenly-spaced window indices
    centres = [dur * (i + 0.5) / M for i in range(M)]
    frame_idx = {int(round((j + 0.5) * M / n - 0.5)) for j in range(n)} if n else set()

    tot_frames = tot_black = 0
    yavgs, frozen_dur, sil_dur = [], 0.0, 0.0
    win_audio_s = 0.0
    fslot = 0
    for i, c in enumerate(centres):
        ss = max(0.0, c - SEEK_WIN_S / 2.0)
        metafile = f"{prefix}.w{i}.meta"
        sig_vf = (f"fps={ANALYZE_FPS},blackframe=amount=98:thresh=32,"
                  f"freezedetect=n=0.001:d=0.5,signalstats,metadata=print:file={metafile}")
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
               "-threads", str(FFTHREADS), "-ss", f"{ss:.3f}", "-t", f"{SEEK_WIN_S:.3f}",
               *decode_args, "-i", path,
               "-map", "0:v:0", "-an", "-vf", sig_vf, "-f", "null", "-"]
        if i in frame_idx:
            cmd += ["-map", "0:v:0", "-vf", _frame_scale(), "-frames:v", "1",
                    "-q:v", "3", "-y", f"{prefix}_{fslot}.jpg"]
            fslot += 1
        if has_audio:
            cmd += ["-map", "0:a:0?", "-af", "silencedetect=n=-30dB:d=1.0", "-f", "null", "-"]
        p = _run(cmd, timeout=300)
        mt = ""
        if os.path.exists(metafile):
            with open(metafile, errors="ignore") as f:
                mt = f.read()
            try: os.remove(metafile)
            except OSError: pass
        t, b, y = _parse_meta_text(mt)
        tot_frames += t; tot_black += b; yavgs += y
        frozen_dur += _sum_durations(p.stderr, _FREEZE_DUR_RE, _FREEZE_START_RE, SEEK_WIN_S)
        if has_audio:
            sil_dur += _sum_durations(p.stderr, _SIL_DUR_RE, _SIL_START_RE, SEEK_WIN_S)
            win_audio_s += SEEK_WIN_S

    black_ratio = (tot_black / tot_frames) if tot_frames else None
    brightness_mean = (sum(yavgs) / len(yavgs)) if yavgs else None
    frozen_ratio = min(frozen_dur / (M * SEEK_WIN_S), 1.0) if M else None
    silence_ratio = min(sil_dur / win_audio_s, 1.0) if win_audio_s else None

    frames = sorted(glob.glob(prefix + "_*.jpg"))
    sig = _mk_sig(dur, black_ratio, frozen_ratio, silence_ratio, brightness_mean,
                  tot_frames, "seek")
    return sig, frames

def _mk_sig(dur, black_ratio, frozen_ratio, silence_ratio, brightness_mean,
            total_frames, method):
    return {
        "is_sub2s": 1 if (dur and dur < 2.0) else 0,
        "black_ratio": round(black_ratio, 4) if black_ratio is not None else None,
        "frozen_ratio": round(frozen_ratio, 4) if frozen_ratio is not None else None,
        "silence_ratio": round(silence_ratio, 4) if silence_ratio is not None else None,
        "brightness_mean": round(brightness_mean, 3) if brightness_mean is not None else None,
        "total_frames_analyzed": total_frames,
        "passa_method": method,
    }

def passa_fused(path, meta):
    """Fused Pass A: one NVDEC decode per file -> backbone signals + persisted VLM
    frames. Returns signals dict incl. n_frames / frame_paths / decoder / cpu_fallback."""
    prefix = os.path.join(FRAME_DIR, _key_for(path))
    tail = _is_tail(meta)
    runner = _passa_seek if tail else _passa_full
    t0 = time.time()
    decode_args, decoder = _decoder_args(meta)
    cpu_fallback = 0
    fallback_error = None
    try:
        sig, frames = runner(path, meta, prefix, decode_args)
    except Exception as e:
        if decoder == "cpu":
            raise
        fallback_error = str(e)[:200]
        # A CIFS/EIO storage stall (soft /mnt/nas2 mount) can fail an otherwise-fine
        # NVDEC decode. Those return fast, so retry the SAME decoder a bounded number of
        # times before giving up to CPU (2026-07-04 storage-fix). A genuine unsupported-
        # codec error does NOT match _is_storage_error -> no retry, CPU fallback as before.
        recovered = False
        if _is_storage_error(e):
            for _ in range(STORAGE_RETRIES):
                time.sleep(STORAGE_BACKOFF_S)
                try:
                    sig, frames = runner(path, meta, prefix, decode_args)
                    recovered = True
                    break
                except Exception as e2:
                    fallback_error = str(e2)[:200]
                    if not _is_storage_error(e2):
                        break          # not a transient stall any more -> stop retrying
        if not recovered:
            # NVDEC still failing -> retry on CPU and flag it. cpu_fallback=1 is the signal
            # run_full's tripwire discriminates (storage-stall vs genuine decoder fault).
            cpu_fallback = 1
            decode_args, decoder = _decoder_args(meta, force_cpu=True)
            sig, frames = runner(path, meta, prefix, decode_args)
    sig["passa_ms"] = int((time.time() - t0) * 1000)
    sig["n_frames"] = len(frames)
    sig["frame_paths"] = frames
    sig["decoder"] = decoder
    sig["cpu_fallback"] = cpu_fallback
    sig["fallback_error"] = fallback_error if cpu_fallback else None
    return sig

def partial_sha(path, size_bytes):
    """sha256 of first+last 1 MB (whole file if <2 MB) — cheap exact-dup fingerprint."""
    h = hashlib.sha256()
    chunk = 1 << 20
    try:
        with open(path, "rb") as f:
            if size_bytes <= 2 * chunk:
                h.update(f.read())
            else:
                h.update(f.read(chunk))
                f.seek(-chunk, os.SEEK_END)
                h.update(f.read(chunk))
    except Exception as e:
        return "ERR:" + str(e)[:40]
    return h.hexdigest()

# ---------------------------------------------------------------- Pass B VLM

VLM_PROMPT = (
    "You are tagging a single video clip for a photo/video library cull tool. "
    "The images provided are frames sampled in time order from ONE clip. "
    "Describe the CLIP as a whole, not each frame separately. "
    "Respond with STRICT JSON only — no markdown, no commentary — using exactly these keys:\n"
    '{"caption": "one sentence describing the clip", '
    '"setting": "one of: indoor, outdoor, mixed, unknown", '
    '"scene": "short label e.g. kitchen, beach, car interior, screen", '
    '"people": "one of: none, one, few, many, crowd", '
    '"activities": ["short verb phrases"], '
    '"objects": ["salient objects visible"], '
    '"flags": {"screen_recording": false, "document_scan": false, "mostly_text": false, "low_quality": false}, '
    '"quality_note": "short note on visual quality / cull-usefulness"}\n'
    "Rules: each element of \"activities\" and \"objects\" must be a SINGLE item — "
    "one short phrase per array element, never a comma-joined string (e.g. use "
    '["sofa", "television", "coffee table"], NOT ["sofa, television, coffee table"]). '
    "Set \"low_quality\" true ONLY for genuinely degraded footage — heavy blur, "
    "severe noise/compression, near-black or blown-out exposure that hurts "
    "usefulness. Ordinary casual phone video, slight softness, or handheld shake is "
    "NOT low_quality; leave it false when the clip is still usable.\n"
    "Return only the JSON object."
)

def ollama_generate(images_b64, timeout=240):
    body = json.dumps({
        "model": VLM_MODEL,
        "prompt": VLM_PROMPT,
        "images": images_b64,
        "format": "json",
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0.1, "num_predict": 512},
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    wall_ms = int((time.time() - t0) * 1000)
    return payload, wall_ms

def _extract_json(text):
    if not text:
        return None
    s = text.find("{")
    if s < 0:
        return None
    depth = 0
    for i in range(s, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[s:i + 1])
                except Exception:
                    return None
    return None

def _split_joined(seq):
    """Defensive: if the model still comma-joins a single array element, split it."""
    out = []
    if not isinstance(seq, list):
        return out
    for el in seq:
        if isinstance(el, str) and "," in el:
            out += [p.strip() for p in el.split(",") if p.strip()]
        elif el is not None:
            out.append(el)
    return out

def passb_from_frames(frame_paths, meta, passa=None):
    """Run one ollama request over PRE-EXTRACTED frames (from fused Pass A).
    Does NOT touch the NAS file — frames were written by Pass A."""
    dur = meta.get("duration_s") or 0.0
    dead = False
    if passa:
        br, fr = passa.get("black_ratio"), passa.get("frozen_ratio")
        if (br is not None and br > BLACK_THRESH) or (fr is not None and fr > BLACK_THRESH):
            dead = True
    frames = [f for f in (frame_paths or []) if os.path.exists(f) and os.path.getsize(f) > 0]
    # dead clips: tag with a single frame only
    if dead and len(frames) > 1:
        frames = frames[:1]
    if not frames:
        return {"passb_error": "no frames available", "n_frames": 0,
                "vlm_model": VLM_MODEL, "dead_clip": dead}
    imgs = []
    for fp in frames:
        with open(fp, "rb") as f:
            imgs.append(base64.b64encode(f.read()).decode())
    try:
        payload, wall_ms = ollama_generate(imgs)
    except Exception as e:
        return {"passb_error": "ollama: " + str(e)[:120], "n_frames": len(frames),
                "vlm_model": VLM_MODEL, "dead_clip": dead}
    raw = payload.get("response", "")
    parsed = _extract_json(raw)
    res = {"n_frames": len(frames), "vlm_ms": wall_ms, "vlm_model": VLM_MODEL,
           "dead_clip": dead,
           "ollama_total_ms": int(payload.get("total_duration", 0) / 1e6),
           "ollama_eval_ms": int(payload.get("eval_duration", 0) / 1e6),
           "ollama_load_ms": int(payload.get("load_duration", 0) / 1e6)}
    if parsed is None:
        res.update({"parse_error": 1, "vlm_raw": raw[:2000]})
        return res
    flags = parsed.get("flags") if isinstance(parsed.get("flags"), dict) else {}
    res.update({
        "parse_error": 0,
        "vlm_caption": str(parsed.get("caption", ""))[:1000],
        "vlm_setting": str(parsed.get("setting", ""))[:40],
        "vlm_scene": str(parsed.get("scene", ""))[:120],
        "vlm_people": str(parsed.get("people", ""))[:40],
        "vlm_activities": json.dumps(_split_joined(parsed.get("activities", []))),
        "vlm_objects": json.dumps(_split_joined(parsed.get("objects", []))),
        "vlm_flags": json.dumps(flags),
        "vlm_quality_note": str(parsed.get("quality_note", ""))[:500],
    })
    return res

# ---------------------------------------------------------------- CLI (debug)

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "init-db":
        init_db()
        print("initialised", DB_PATH)
    elif cmd == "passa":
        f = sys.argv[2]
        m = ffprobe_meta(f)
        a = passa_fused(f, m)
        print(json.dumps({**m, **a}, indent=2))
    elif cmd == "passb":
        f = sys.argv[2]
        m = ffprobe_meta(f)
        a = passa_fused(f, m)
        b = passb_from_frames(a["frame_paths"], m, passa=a)
        for fp in a["frame_paths"]:
            try: os.remove(fp)
            except OSError: pass
        print(json.dumps(b, indent=2))
    else:
        print("unknown command", cmd)

if __name__ == "__main__":
    os.makedirs(FRAME_DIR, exist_ok=True)
    main()
