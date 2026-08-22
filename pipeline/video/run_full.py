#!/usr/bin/env python3
"""
run_full.py — full 35,133-clip video-signal scan (pipelined, durable, resumable).

Reuses the VALIDATED videoscan.py pipeline UNCHANGED (locked NVDEC/fused Pass A +
qwen2.5vl:7b Pass B). Adds only the run harness the sample runner lacked:

  1. PIPELINING. A bounded producer/consumer overlaps decode(N+1) with VLM(N):
     PASSA_WORKERS decode+backbone threads (fused NVDEC Pass A, GPU0) feed a bounded
     queue; a SINGLE serial VLM consumer drains it (Pass B on GPU0), writes the row,
     and frees the frames. Pass A (~0.46 files/s @ P8) runs ahead of the serial VLM
     floor (~0.33 clips/s), so decode hides under the ~29 h VLM long pole and the
     queue applies natural backpressure. Target ~28-30 h vs ~50 h sequential.

  2. DURABLE / RESUMABLE / OBSERVABLE.
     - Durable: meant to be launched under setsid/nohup so it outlives the launcher.
     - Resumable/idempotent: only a COMPLETE row is ever written (INSERT OR REPLACE,
       path PRIMARY KEY, sample=0). On start we skip every path that already has a
       sample=0 row, so an interrupt (Ctrl-C / reboot / systemctl stop) resumes with
       no rescanning and no dup rows. In-flight (uncommitted) clips are simply redone.
       The 200 sample=1 rows are re-scanned as authoritative sample=0 rows.
     - Observable: a heartbeat progress.json is written into the vault run dir every
       ~200 clips (and at least every ~90 s) with scanned/total, windowed + cumulative
       rate/ETA, last_path, failure/fallback counts, tripwire state, status.

  3. HALT-NOT-GRIND TRIPWIRES (post-Xid-79, cpu_fallback DISCRIMINATING since
     2026-07-03). The 2026-07-01 run ground for hours at CPU speed after the GPU fell
     off the bus (ollama silently reloaded the model on CPU, 3 s -> 82 s/clip). The
     2026-07-03 01:02Z halt then showed the opposite failure: ONE degenerate ~0.07s
     stub clip broke the fused Pass-A graph under cuvid on a HEALTHY GPU, and the
     blunt "any cpu_fallback = dead GPU" rule halted a good run. Now:
       - rolling median vlm_ms over the last VLM_WINDOW calls > VLM_SICK_MS -> HALT
       - Pass-A cpu_fallback -> run a DISCRIMINATING probe before deciding:
         pinned GPU UUID visible + ollama model in VRAM + canary NVDEC decodes
         (h264_cuvid AND hevc_cuvid on known-good already-scanned files).
         Any probe FAILS -> HALT as degraded (reason names the failed probe).
       - STORAGE-AWARE (2026-07-04): when the canaries PASS the GPU/decoder is healthy,
         so the fallback was a CIFS/SMB EIO stall on the soft /mnt/nas2 mount, NOT a
         decoder fault. Classify as io_fallback, LOG (path + reason + errno), CONTINUE —
         it NEVER halts (a separate counter + soft WARN track sustained NAS trouble).
         This replaced the old 3-in-50 rate backstop, which counted every fallback and
         so mis-attributed a burst of storage stalls to the decoder, halting a healthy
         run at 25,080/35,133 on 2026-07-03 21:59Z.
       - periodic probe: pinned GPU UUID visible to nvidia-smi AND the ollama model
         resident in VRAM (/api/ps size_vram) -> failure -> HALT. The probe also
         records the PCIe "Replays Since Reset" counter (WATCH-ONLY, never trips).
     A trip stops the workers, writes heartbeat status=halted-tripwire + reason, and
     exits NONZERO. Sick work is never silently committed as trusted data.

  4. FAST SIGTERM. TERM/INT stops after the in-flight clip(s): the producer stops
     submitting, queued decodes are skipped, and the consumer drains the queue
     WITHOUT running the VLM (frames freed, rows not written — resume-safe).
     Target: exit within ~30 s of the signal.

  5. PERSISTED TELEMETRY. Additive columns per row: vlm_device_ok (rolling health at
     write time), windowed_rate (clips/min at write time), run_token.

Env overrides (smoke testing only — real run uses defaults):
  RUNFULL_DB / RUNFULL_LIST / RUNFULL_RUN_DIR / RUNFULL_PIDFILE
  RUNFULL_LIMIT=N   cap todo (smoke)
  RUNFULL_SMOKE=1   never write to the relay prompts lane (keeps lanes clean)

Target: /mnt/nas2/photos ONLY, all 35,133 videos (photos_video_list.txt).
Writes ONLY ~/loupe-ml/video/ + the vault heartbeat/report. Reads NAS bytes READ-ONLY.
"""
import os, sys, json, time, glob, queue, signal, threading, datetime, subprocess
import collections, statistics, urllib.request
from concurrent.futures import ThreadPoolExecutor

import videoscan as vs

# ---------------------------------------------------------------- config
DB_PATH        = os.environ.get("RUNFULL_DB", vs.DB_PATH)
LIST_CACHE     = os.environ.get("RUNFULL_LIST", os.path.join(vs.BASE, "photos_video_list.txt"))
PIDFILE        = os.environ.get("RUNFULL_PIDFILE", os.path.join(vs.BASE, "run_full.pid"))
LOCALLOG       = os.path.join(vs.BASE, "full_run.log")
SMOKE          = os.environ.get("RUNFULL_SMOKE") == "1"
LIMIT          = int(os.environ.get("RUNFULL_LIMIT", "0"))

PASSA_WORKERS  = 8      # fused NVDEC Pass-A threads (locked: 8 workers, -threads 2 each)
QUEUE_MAX      = 64     # bounded hand-off queue -> caps frames-on-disk + gives backpressure

VAULT          = os.path.expanduser("~/Vaults/loupe-vault")
# Which relay lane this host writes into. Deployment configuration.
RELAY_LANE     = "from-" + os.environ.get("LOUPE_RELAY_SEAT", "video")
RUN_TOKEN      = "FLEET-WORKER5-BUILD-20260704-delta-scan-5508"
RUN_DIR        = os.environ.get("RUNFULL_RUN_DIR",
                                os.path.join(VAULT, RELAY_LANE, "runs", RUN_TOKEN))
PROGRESS       = os.path.join(RUN_DIR, "progress.json")
DONE_MARKER    = os.path.join(RUN_DIR, "done")
PROMPTS_DIR    = os.path.join(VAULT, RELAY_LANE, "prompts")
LATEST_RESP    = os.path.join(PROMPTS_DIR, "latest_response.md")
RESPONSES      = os.path.join(PROMPTS_DIR, "responses.md")

HEARTBEAT_EVERY_CLIPS = 200
HEARTBEAT_EVERY_S     = 90

# windowed rate/ETA (fix 1)
WINDOW_CLIPS   = 200    # rolling window: last N clip completions ...
WINDOW_S       = 900    # ... or last 15 min, whichever is smaller

# tripwires (fix 2) — GPU-healthy vlm_ms is ~2,500-4,000 ms; CPU grind was ~82,000 ms
VLM_WINDOW       = 20     # rolling median over the last N VLM calls
VLM_MIN_SAMPLES  = 10     # don't judge before this many samples
VLM_SICK_MS      = 15000  # median above this -> the GPU is not doing the work
PROBE_EVERY_S    = 90     # nvidia-smi UUID + ollama /api/ps VRAM residency probe

# cpu_fallback discrimination (2026-07-03; storage-aware 2026-07-04): a fallback on a
# HEALTHY GPU is never a dead device. If the canaries PASS it is a storage stall (CIFS
# EIO on the soft /mnt/nas2 mount), NOT a decoder fault — it must not halt the run. Only
# a canary FAILURE (real decoder/GPU trouble) halts, immediately (halt-not-grind). The
# old 3-in-50 "rate backstop" mis-attributed a burst of storage stalls to the decoder
# and halted a healthy run (2026-07-03 21:59Z) — removed; storage now has its own
# never-halt counter + soft warn.
CANARY_CODECS          = ("h264", "hevc")  # canary NVDEC decode per codec on fallback
CANARY_TIMEOUT_S       = 120               # canary reads NAS bytes; generous but bounded
CIFS_MOUNT             = os.environ.get("LOUPE_CIFS_MOUNT", "/mnt/nas2")        # soft SMB mount; EIO on stall -> storage fallback
FALLBACK_WINDOW_CLIPS  = 50                # storage-fallback visibility window: last N clips ...
FALLBACK_WINDOW_S      = 1800              # ... or last 30 min, whichever is smaller
IO_FALLBACK_WARN_IN_WINDOW = 25            # loud WARNING (never halts) on sustained NAS trouble

# thermal gate (2026-07-04) — two-stage, LOCAL, zero external calls. Turns the
# 2026-07-03 watch-only note above read_thermal() into an actual self-defence gate.
# Evaluated once per heartbeat (reuses the heartbeat cadence — no second timer);
# NEVER acts on a single sample (THERM_SUSTAIN_N consecutive heartbeats required).
# The outbound pager (ntfy) lives ONLY in the separate thermal_sentinel.py sidecar —
# NO network call may live in this product code (07-03 local-only pivot).
THERM_SOFT_C       = 80    # °C: sustained >= this -> Stage-1 SOFT throttle (recoverable)
THERM_HARD_C       = 83    # °C: sustained >= this -> Stage-2 HARD graceful halt (resume-safe)
THERM_MARGIN_HARD  = 2     # T.Limit margin °C: sustained <= this -> HARD halt (the current architecture margin metric)
THERM_SUSTAIN_N    = 3     # consecutive heartbeats a condition must hold before acting (anti-spike)
THERM_COOLDOWN_S   = 20    # one-shot VLM pause injected when SOFT engages (lets the card breathe)
THERM_SOFT_WORKERS = 4     # Pass-A decode concurrency while SOFT-throttled (down from PASSA_WORKERS=8)
THERM_SOFT_CLEAR_C = THERM_SOFT_C - 3   # hysteresis: temp must fall below this (77) to recover

SENTINEL = object()
stop_event = threading.Event()

class AdjustableGate:
    """A semaphore whose capacity can be changed at runtime WITHOUT the setter ever
    blocking. acquire() waits while active >= limit (or until stop_event); set_limit()
    is O(1). This is how the thermal gate reduces live Pass-A decode concurrency mid-run
    (the ThreadPoolExecutor's own worker count is fixed and not resizable). The wait has a
    1 s timeout + stop_event check so a low limit can NEVER wedge the drain on stop."""
    def __init__(self, limit):
        self._cv = threading.Condition()
        self._limit = limit
        self._active = 0
    def set_limit(self, n):
        with self._cv:
            self._limit = n
            self._cv.notify_all()
    def acquire(self):
        with self._cv:
            while self._active >= self._limit and not stop_event.is_set():
                self._cv.wait(timeout=1.0)
            self._active += 1
    def release(self):
        with self._cv:
            self._active -= 1
            self._cv.notify()

DECODE_GATE = AdjustableGate(PASSA_WORKERS)   # live Pass-A decode-concurrency gate (thermal-adjustable)

# ---------------------------------------------------------------- helpers

def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")

def log(msg):
    line = f"[{now_iso()}] {msg}"
    print(line, flush=True)

def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def connect():
    return vs.connect(DB_PATH)

def load_todo():
    with open(LIST_CACHE) as f:
        allpaths = [l.strip() for l in f if l.strip()]
    con = connect()
    done = {r[0] for r in con.execute("SELECT path FROM videos WHERE sample=0")}
    con.close()
    todo = [p for p in allpaths if p not in done]
    if LIMIT:
        todo = todo[:LIMIT]
    return allpaths, todo, len(done)

# ---------------------------------------------------------------- tripwire (fix 2)

def probe_gpu_ollama():
    """Definitive device-health probe. Returns (ok, detail).
    Trips only on positive evidence of a sick device — pinned UUID gone from
    nvidia-smi (Xid-79 signature) or the ollama model resident but NOT in VRAM."""
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=20)
        if vs.GPU_UUID not in r.stdout:
            return False, "pinned GPU UUID missing from nvidia-smi -L (fell off bus?)"
    except Exception as e:
        return False, f"nvidia-smi probe failed: {str(e)[:120]}"
    try:
        with urllib.request.urlopen(vs.OLLAMA + "/api/ps", timeout=10) as resp:
            ps = json.loads(resp.read().decode())
        for mdl in ps.get("models", []):
            if str(mdl.get("name", "")).startswith("qwen2.5vl"):
                size, vram = mdl.get("size", 0) or 0, mdl.get("size_vram", 0) or 0
                if size and (vram / size) < 0.5:
                    return False, f"ollama model NOT in VRAM: size_vram={vram} / size={size}"
                return True, f"GPU visible; ollama resident in VRAM ({vram // (1 << 20)} MiB)"
        return True, "GPU visible; model not currently resident (no CPU evidence)"
    except Exception as e:
        return True, f"GPU visible; ollama /api/ps unreachable ({str(e)[:80]}) — not trip-worthy alone"

def pcie_replays():
    """WATCH-ONLY: PCIe 'Replays Since Reset' for the pinned GPU. Never trips —
    logged so a climb under load is visible (boot-era baseline ~28k is stale)."""
    try:
        r = subprocess.run(["nvidia-smi", "-q", "-i", vs.GPU_UUID],
                           capture_output=True, text=True, timeout=20)
        for line in r.stdout.splitlines():
            if "Replays Since Reset" in line:
                return int(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return None

def pick_canary(codec):
    """Most recent healthy GPU-decoded row for this codec — a KNOWN-GOOD NVDEC input."""
    try:
        con = connect()
        row = con.execute(
            """SELECT path FROM videos
               WHERE sample=0 AND decoder=? AND cpu_fallback=0 AND passa_error IS NULL
                 AND duration_s > 1
               ORDER BY scanned_at DESC LIMIT 1""", (codec + "_cuvid",)).fetchone()
        con.close()
        if row and os.path.exists(row[0]):
            return row[0]
    except Exception:
        pass
    return None

def canary_decode(codec):
    """(ok, detail) — decode a few frames of a known-good file with <codec>_cuvid.
    A KNOWN-GOOD input failing NVDEC is positive evidence the decoder is degraded.
    RUNFULL_CANARY_FAIL=1 (test hook) simulates a failed canary."""
    if os.environ.get("RUNFULL_CANARY_FAIL") == "1":
        return False, f"{codec}_cuvid canary: simulated failure (RUNFULL_CANARY_FAIL=1)"
    path = pick_canary(codec)
    if path is None:
        return True, f"{codec}_cuvid canary: no known-good file yet — skipped (not trip-worthy alone)"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
           "-hwaccel", "cuda", "-hwaccel_device", "0", "-c:v", codec + "_cuvid",
           "-i", path, "-frames:v", "3", "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=CANARY_TIMEOUT_S, env=vs.FF_ENV)
    except Exception as e:
        return False, f"{codec}_cuvid canary errored on known-good {path}: {str(e)[:120]}"
    if p.returncode != 0:
        return False, (f"{codec}_cuvid canary decode FAILED on known-good {path}: "
                       + (p.stderr or "").strip()[-200:])
    return True, f"{codec}_cuvid canary ok ({os.path.basename(path)})"

def discriminate_fallback():
    """(genuine, reason) — decide whether a Pass-A cpu_fallback means a degraded GPU
    or a genuine degenerate codec/file. genuine=True only if EVERY probe passes:
    pinned UUID visible + ollama in VRAM + h264/hevc cuvid canary decodes succeed."""
    ok, detail = probe_gpu_ollama()
    if not ok:
        return False, f"device probe failed: {detail}"
    details = [detail]
    for codec in CANARY_CODECS:
        ok, detail = canary_decode(codec)
        if not ok:
            return False, detail
        details.append(detail)
    return True, "; ".join(details)

class Tripwire:
    """Halt-not-grind watchdog. Any trip sets stop_event; main exits nonzero."""
    def __init__(self):
        self.vlm_window = collections.deque(maxlen=VLM_WINDOW)
        self.tripped = None           # reason string once tripped
        self.last_probe_ok = True
        self.last_probe_detail = "not yet probed"
        self.last_probe_t = 0.0
        self.io_fallbacks = 0                  # storage/CIFS stalls (canaries PASS) — never halt
        self.decoder_fallbacks = 0             # genuine decoder faults (canaries FAIL) — halt
        self.io_window = collections.deque()   # (t, clip_index) of recent storage fallbacks (soft warn)
        self.pcie_replays = None               # watch-only, refreshed by maybe_probe

    def trip(self, reason):
        if self.tripped is None:
            self.tripped = reason
            log(f"TRIPWIRE: {reason} -> halting run (halt-not-grind)")
            stop_event.set()

    def record_vlm_ms(self, ms):
        if ms is None:
            return
        self.vlm_window.append(ms)
        if len(self.vlm_window) >= VLM_MIN_SAMPLES:
            med = statistics.median(self.vlm_window)
            if med > VLM_SICK_MS:
                self.trip(f"rolling median vlm_ms={med:.0f} > {VLM_SICK_MS} "
                          f"over last {len(self.vlm_window)} VLM calls (CPU-grind signature)")

    def record_cpu_fallback(self, path, clip_index, fallback_error=None):
        """Classify a Pass-A cpu_fallback and act. Discriminate FIRST (probe + canary):

          - canaries FAIL  -> genuine DECODER/GPU fault -> HALT immediately (halt-not-grind,
            unchanged). This is the real-trouble path.
          - canaries PASS  -> the GPU/decoder is healthy, so the fallback was storage-induced
            (a CIFS/SMB EIO stall on the soft /mnt/nas2 mount). Classify as io_fallback:
            count it separately, log it distinctly, and CONTINUE. It must NEVER halt the run
            (the clip already succeeded on the CPU-decode retry inside passa_fused).

        This replaces the old 3-in-50 rate backstop, which counted EVERY fallback and so
        mis-attributed a burst of storage stalls to the decoder and halted a healthy run
        (2026-07-04 storage-fix). fallback_error, when captured, corroborates + is logged."""
        t = time.time()
        genuine, reason = discriminate_fallback()
        if not genuine:
            self.decoder_fallbacks += 1
            self.trip(f"Pass-A cpu_fallback on {path} + discriminating probe FAILED "
                      f"({reason}) -> GPU/decoder degraded"
                      + (f" [read/decode err: {fallback_error}]" if fallback_error else ""))
            return
        # canaries PASS -> storage stall, not a decoder fault. Never halts.
        self.io_fallbacks += 1
        self.io_window.append((t, clip_index))
        while self.io_window and (t - self.io_window[0][0] > FALLBACK_WINDOW_S
                                  or clip_index - self.io_window[0][1] > FALLBACK_WINDOW_CLIPS):
            self.io_window.popleft()
        loc = "under CIFS " + CIFS_MOUNT if path.startswith(CIFS_MOUNT) else "off-CIFS"
        errbit = f"; read/decode err: {fallback_error}" if fallback_error else ""
        log(f"io_fallback on {path} ({loc}) -> storage stall: canaries PASS, decoder "
            f"healthy -> CONTINUE (NOT counted toward decoder tripwire; probes: {reason}{errbit})")
        if len(self.io_window) >= IO_FALLBACK_WARN_IN_WINDOW:
            log(f"WARNING: {len(self.io_window)} io_fallbacks within rolling "
                f"{FALLBACK_WINDOW_CLIPS}-clip / {FALLBACK_WINDOW_S // 60}-min window — "
                f"sustained CIFS/NAS instability (storage, NOT decoder; NOT halting)")

    def maybe_probe(self):
        if time.time() - self.last_probe_t < PROBE_EVERY_S:
            return
        ok, detail = probe_gpu_ollama()
        self.pcie_replays = pcie_replays()
        self.last_probe_t = time.time()
        self.last_probe_ok = ok
        self.last_probe_detail = detail + (
            f"; PCIe Replays Since Reset={self.pcie_replays}"
            if self.pcie_replays is not None else "")
        if not ok:
            self.trip(f"device probe failed: {detail}")

    def median_recent(self):
        return statistics.median(self.vlm_window) if self.vlm_window else None

    def device_ok(self):
        return 0 if (self.tripped or not self.last_probe_ok) else 1

    def snapshot(self):
        med = self.median_recent()
        return {
            "armed": True,
            "mode": "discriminating-storage-aware",   # canary-pass=storage(no halt), canary-fail=decoder(halt)
            "tripped": self.tripped,
            "vlm_ms_median_recent": round(med) if med is not None else None,
            "vlm_window_n": len(self.vlm_window),
            "vlm_sick_ms": VLM_SICK_MS,
            "last_probe_ok": self.last_probe_ok,
            "last_probe": self.last_probe_detail,
            "pcie_replays_since_reset": self.pcie_replays,   # watch-only
            "io_fallbacks": self.io_fallbacks,              # storage/CIFS stalls — never halt
            "decoder_fallbacks": self.decoder_fallbacks,    # genuine decoder faults — halt
            "io_fallbacks_in_window": len(self.io_window),
            "io_fallback_window": f"{FALLBACK_WINDOW_CLIPS} clips / {FALLBACK_WINDOW_S // 60} min, "
                                  f"warn (no halt) at {IO_FALLBACK_WARN_IN_WINDOW}",
        }

TW = Tripwire()

# ---------------------------------------------------------------- Pass A worker

def do_passa(path):
    try:
        meta = vs.ffprobe_meta(path)
        a = vs.passa_fused(path, meta)
        sha = vs.partial_sha(path, meta["size_bytes"])
        return {"path": path, "ok": True, "meta": meta, "a": a, "sha": sha}
    except Exception as e:
        return {"path": path, "ok": False, "err": str(e)[:200]}

# ---------------------------------------------------------------- DB writer

# telemetry columns (fix 4) — additive, backward-compatible
TELEMETRY_COLS = {"vlm_device_ok": "INTEGER", "windowed_rate": "REAL", "run_token": "TEXT"}

def migrate_telemetry_cols():
    con = connect()
    have = {r[1] for r in con.execute("PRAGMA table_info(videos)")}
    for col, decl in TELEMETRY_COLS.items():
        if col not in have:
            con.execute(f"ALTER TABLE videos ADD COLUMN {col} {decl}")
            log(f"schema: added column {col} {decl}")
    con.commit()
    con.close()

INSERT_SQL = """INSERT OR REPLACE INTO videos
    (path,size_bytes,duration_s,width,height,fps,vcodec,container,rotation,
     has_audio,acodec,is_sub2s,black_ratio,frozen_ratio,silence_ratio,
     brightness_mean,dup_partial_sha,passa_ms,decoder,cpu_fallback,passa_method,
     vlm_caption,vlm_setting,vlm_scene,vlm_people,vlm_activities,vlm_objects,
     vlm_flags,vlm_quality_note,vlm_model,n_frames,vlm_ms,vlm_raw,parse_error,
     passb_error,sample,scanned_at,vlm_device_ok,windowed_rate,run_token)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)"""

def write_ok(con, r, b, when, device_ok, wrate):
    m, a = r["meta"], r["a"]
    con.execute(INSERT_SQL, (
        r["path"], m["size_bytes"], m["duration_s"], m["width"], m["height"], m["fps"],
        m["vcodec"], m["container"], m["rotation"], m["has_audio"], m["acodec"],
        a["is_sub2s"], a["black_ratio"], a["frozen_ratio"], a["silence_ratio"],
        a["brightness_mean"], r["sha"], a["passa_ms"], a["decoder"], a["cpu_fallback"],
        a["passa_method"],
        b.get("vlm_caption"), b.get("vlm_setting"), b.get("vlm_scene"), b.get("vlm_people"),
        b.get("vlm_activities"), b.get("vlm_objects"), b.get("vlm_flags"),
        b.get("vlm_quality_note"), b.get("vlm_model"), b.get("n_frames"), b.get("vlm_ms"),
        b.get("vlm_raw"), b.get("parse_error", 0), b.get("passb_error"), when,
        device_ok, wrate, RUN_TOKEN))

def write_passa_error(con, path, err, when, device_ok, wrate):
    con.execute("""INSERT OR REPLACE INTO videos
                   (path,passa_error,sample,scanned_at,vlm_device_ok,windowed_rate,run_token)
                   VALUES (?,?,0,?,?,?,?)""",
                (path, err, when, device_ok, wrate, RUN_TOKEN))

# ---------------------------------------------------------------- heartbeat

# thermal telemetry (2026-07-03) + GATE (2026-07-04). This card (the ML GPU/the current architecture)
# reports margin-based T.Limit, not absolute thresholds: ~5 C margin at 73-77 C edge
# temp under full scan load, HW slowdown at margin -2, shutdown at margin -5. The
# 2026-07-03 watch-only note ("HALT if temp_c >= ~83 C sustained, or T.Limit margin
# <= 2 C") is now the live ThermalGovernor gate below — THERM_HARD_C / THERM_MARGIN_HARD.
_THERMAL_THROTTLE_FIELDS = {
    "clocks_throttle_reasons.hw_slowdown":             "hw_slowdown",
    "clocks_throttle_reasons.hw_thermal_slowdown":     "hw_thermal_slowdown",
    "clocks_throttle_reasons.hw_power_brake_slowdown": "hw_power_brake",
    "clocks_throttle_reasons.sw_thermal_slowdown":     "sw_thermal_slowdown",
    "clocks_throttle_reasons.sw_power_cap":            "sw_power_cap",
}

def read_tlimit_margin():
    """GPU 'T.Limit Temp' margin in °C for the pinned card (the current architecture reports margin-to-
    slowdown, not an absolute limit). Returns None if unavailable. This is the metric the
    hard gate's THERM_MARGIN_HARD watches (danger at margin <= 2). Own try/timeout — a
    failure here degrades to None and never disturbs the temp/fan/power read."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "-q", "-d", "TEMPERATURE", "--id=" + vs.GPU_UUID],
            capture_output=True, text=True, timeout=20)
        for line in r.stdout.splitlines():
            # match ONLY the bare "GPU T.Limit Temp" (not Shutdown/Slowdown/Max Operating,
            # whose lines contain an extra word between "GPU" and "T.Limit")
            if "GPU T.Limit Temp" in line:
                return int(line.split(":", 1)[1].strip().split()[0])
    except Exception:
        pass
    return None

def read_thermal():
    """One nvidia-smi sample of the pinned GPU (temp/fan/power/throttle + T.Limit margin)."""
    fields = ["temperature.gpu", "temperature.memory", "fan.speed",
              "power.draw", "power.limit"] + list(_THERMAL_THROTTLE_FIELDS)
    r = subprocess.run(
        ["nvidia-smi", "--query-gpu=" + ",".join(fields),
         "--format=csv,noheader,nounits", "--id=" + vs.GPU_UUID],
        capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:120])
    vals = [v.strip() for v in r.stdout.strip().split(",")]
    def num(s):
        try:
            return float(s) if "." in s else int(s)
        except ValueError:
            return None                      # "[N/A]" etc.
    reasons = [n for n, v in zip(_THERMAL_THROTTLE_FIELDS.values(), vals[5:])
               if v == "Active"]
    return {
        "temp_c": num(vals[0]),
        "temp_mem_c": num(vals[1]),
        "fan_pct": num(vals[2]),
        "power_w": num(vals[3]),
        "power_limit_w": num(vals[4]),
        "margin_c": read_tlimit_margin(),      # T.Limit margin °C (None if unavailable)
        "throttle_active": bool(reasons),
        "throttle_reasons": ", ".join(reasons) if reasons else "none",
    }

def read_thermal_safe():
    """read_thermal() that never raises — telemetry failure must fail safe (keep scanning)."""
    try:
        return read_thermal()
    except Exception as e:
        return {"error": str(e)[:120], "temp_c": None, "margin_c": None, "source": "run_full"}

# ---------------------------------------------------------------- thermal gate (2026-07-04)

class ThermalGovernor:
    """Two-stage self-defence gate, driven once per heartbeat by tick().

    evaluate() is the PURE finite-state machine (state + streak counters; no hardware,
    no I/O) so it is unit-testable with synthetic temp sequences. tick() runs evaluate()
    and then applies the hardware side effects for whatever action it returned:
      Stage 1 SOFT  (temp >= THERM_SOFT_C, N sustained): shrink Pass-A decode concurrency
                     8 -> THERM_SOFT_WORKERS and inject a one-shot THERM_COOLDOWN_S VLM
                     pause. Recovers (temp < THERM_SOFT_CLEAR_C, N sustained) -> full workers.
      Stage 2 HARD  (temp >= THERM_HARD_C OR T.Limit margin <= THERM_MARGIN_HARD, N sustained):
                     trip the SAME graceful stop path SIGTERM uses (finish in-flight clip,
                     commit, drain queue without VLM) -> status 'thermal_halt'. Resume-safe.
    A single spike does nothing: a streak resets the moment its condition is not met."""

    def __init__(self):
        self.state = "nominal"          # nominal | soft | halted
        self.soft_events = 0            # count of nominal->soft transitions
        self.halt_reason = None
        self.hard_streak = 0
        self.soft_streak = 0
        self.cool_streak = 0
        self.cooldown_until = 0.0       # wall time until which the consumer pauses (one-shot)

    def evaluate(self, temp_c, margin_c):
        """One heartbeat step. Mutates state + streaks. Returns the action taken:
        None | 'soft' | 'recover' | 'halt:<reason>'. No hardware side effects."""
        if temp_c is None:
            return None                 # missing read: fail-safe no-op, streaks untouched
        hard_by_temp   = temp_c >= THERM_HARD_C
        hard_by_margin = (margin_c is not None) and (margin_c <= THERM_MARGIN_HARD)
        hard = hard_by_temp or hard_by_margin
        soft = temp_c >= THERM_SOFT_C
        cool = temp_c < THERM_SOFT_CLEAR_C
        self.hard_streak = self.hard_streak + 1 if hard else 0
        self.soft_streak = self.soft_streak + 1 if soft else 0
        self.cool_streak = self.cool_streak + 1 if cool else 0
        if self.state == "halted":
            return None                 # terminal — halt only fires once
        if self.hard_streak >= THERM_SUSTAIN_N:
            reason = f"temp_c={temp_c}>={THERM_HARD_C}C" if hard_by_temp else ""
            if hard_by_margin:
                reason += (" & " if reason else "") + f"T.Limit margin={margin_c}<={THERM_MARGIN_HARD}C"
            reason += f" for {THERM_SUSTAIN_N} consecutive heartbeats"
            self.state = "halted"
            self.halt_reason = reason
            return "halt:" + reason
        if self.state == "nominal" and self.soft_streak >= THERM_SUSTAIN_N:
            self.state = "soft"
            self.soft_events += 1
            return "soft"
        if self.state == "soft" and self.cool_streak >= THERM_SUSTAIN_N:
            self.state = "nominal"
            return "recover"
        return None

    def tick(self, cnt, thermal):
        """Feed one heartbeat's thermal reading through the FSM and apply side effects.
        Also updates cnt.temp_c_max and annotates the thermal dict with gate state."""
        t = thermal.get("temp_c")
        if t is not None and (cnt.temp_c_max is None or t > cnt.temp_c_max):
            cnt.temp_c_max = t
        action = self.evaluate(t, thermal.get("margin_c"))
        if action == "soft":
            DECODE_GATE.set_limit(THERM_SOFT_WORKERS)
            self.cooldown_until = time.time() + THERM_COOLDOWN_S
            log(f"THERMAL-SOFT WARN: temp_c={t}C >= {THERM_SOFT_C} sustained {THERM_SUSTAIN_N} "
                f"heartbeats -> throttle Pass-A workers {PASSA_WORKERS}->{THERM_SOFT_WORKERS} "
                f"+ {THERM_COOLDOWN_S}s cooldown (soft_event #{self.soft_events})")
        elif action == "recover":
            DECODE_GATE.set_limit(PASSA_WORKERS)
            log(f"THERMAL recovery: temp_c={t}C < {THERM_SOFT_CLEAR_C} sustained {THERM_SUSTAIN_N} "
                f"heartbeats -> Pass-A workers restored to {PASSA_WORKERS}, state nominal")
        elif action and action.startswith("halt:"):
            DECODE_GATE.set_limit(PASSA_WORKERS)   # unthrottle so the drain is not slowed
            log(f"THERMAL-HARD HALT: {self.halt_reason} (temp_c={t}C) -> graceful resume-safe "
                f"stop (finish in-flight clip, drain queue without VLM)")
            stop_event.set()
        self._annotate(thermal)

    def _annotate(self, thermal):
        thermal["thermal_state"] = self.state
        thermal["thermal_soft_events"] = self.soft_events
        if self.halt_reason:
            thermal["thermal_halt_reason"] = self.halt_reason

    def maybe_cooldown(self):
        """One-shot SOFT-stage cooldown: pause the VLM consumer to let the card cool.
        Stop-aware and self-clearing."""
        end = self.cooldown_until
        if end and time.time() < end and not stop_event.is_set():
            log(f"thermal cooldown: pausing VLM ~{end - time.time():.0f}s (soft throttle)")
            while time.time() < end and not stop_event.is_set():
                time.sleep(min(1.0, max(0.0, end - time.time())))
        self.cooldown_until = 0.0

GOV = ThermalGovernor()

class Counters:
    def __init__(self, total, already):
        self.total = total
        self.already = already          # sample=0 rows present at start (prior resume)
        self.scanned = 0                # clips processed THIS run
        self.ok = 0
        self.passa_err = 0
        self.passb_err = 0
        self.parse_err = 0
        self.cpu_fb = 0
        self.dead = 0
        self.seek = 0
        self.full = 0
        self.discarded = 0              # queue items dropped un-scanned on stop/trip
        self.last_path = None
        self.started_at = now_iso()
        self.t0 = time.time()
        self.recent = collections.deque()   # completion timestamps (fix 1)
        self.temp_c_max = None              # rolling max GPU temp since run start

    def mark(self):
        t = time.time()
        self.recent.append(t)
        while self.recent and (t - self.recent[0] > WINDOW_S or len(self.recent) > WINDOW_CLIPS):
            self.recent.popleft()

    def windowed_cpm(self):
        if len(self.recent) < 2:
            return None
        span = self.recent[-1] - self.recent[0]
        return ((len(self.recent) - 1) / span * 60.0) if span > 0 else None

    def snapshot(self, status, thermal=None):
        elapsed = max(time.time() - self.t0, 1e-6)
        cpm_cum = self.scanned / elapsed * 60.0
        cpm_win = self.windowed_cpm()
        remaining = self.total - (self.already + self.scanned)
        eta_cum_s = (remaining / (cpm_cum / 60.0)) if self.scanned else None
        eta_win_s = (remaining / (cpm_win / 60.0)) if cpm_win else None
        eta_s = eta_win_s if eta_win_s is not None else eta_cum_s
        # Running heartbeats pass the thermal dict already sampled + FSM-ticked by the
        # consumer (one read/heartbeat). Terminal/one-off snapshots sample fresh here and
        # annotate with the CURRENT gate state — WITHOUT running the FSM (no shutdown-time
        # spurious soft/halt actions). Telemetry never kills a heartbeat (read_thermal_safe).
        if thermal is None:
            thermal = read_thermal_safe()
            t = thermal.get("temp_c")
            if t is not None and (self.temp_c_max is None or t > self.temp_c_max):
                self.temp_c_max = t
            GOV._annotate(thermal)
        thermal["temp_c_max"] = self.temp_c_max
        thermal["source"] = "run_full"
        return {
            "run_token": RUN_TOKEN,
            "status": status,            # running | done | stopped | halted-tripwire | thermal_halt
            "total": self.total,
            "done_total": self.already + self.scanned,   # authoritative rows in DB
            "resumed_from": self.already,
            "scanned_this_run": self.scanned,
            "remaining": remaining,
            "clips_per_min": round(cpm_win, 2) if cpm_win is not None else None,  # windowed
            "clips_per_min_cumulative": round(cpm_cum, 2),
            "window": f"last {len(self.recent)} clips / <= {WINDOW_S // 60} min",
            "eta_hours": round(eta_s / 3600.0, 2) if eta_s is not None else None,  # windowed-preferred
            "eta_hours_cumulative": round(eta_cum_s / 3600.0, 2) if eta_cum_s is not None else None,
            "eta_finish": (datetime.datetime.now() +
                           datetime.timedelta(seconds=eta_s)).isoformat(timespec="seconds")
                          if eta_s is not None else None,
            "tripwire": TW.snapshot(),
            "thermal": thermal,
            "ok": self.ok,
            "passa_errors": self.passa_err,
            "passb_errors": self.passb_err,
            "parse_errors": self.parse_err,
            "cpu_fallbacks": self.cpu_fb,
            "dead_clips": self.dead,
            "method_full": self.full,
            "method_seek": self.seek,
            "discarded_on_stop": self.discarded,
            "last_path": self.last_path,
            "elapsed_hours": round(elapsed / 3600.0, 2),
            "started_at": self.started_at,
            "updated_at": now_iso(),
            "pid": os.getpid(),
        }

def write_heartbeat(cnt, status, thermal=None):
    try:
        atomic_write(PROGRESS, json.dumps(cnt.snapshot(status, thermal), indent=2))
    except Exception as e:
        log(f"heartbeat write failed: {e}")

# ---------------------------------------------------------------- producer / consumer

def producer(todo, q):
    with ThreadPoolExecutor(PASSA_WORKERS) as ex:
        sem = threading.Semaphore(PASSA_WORKERS + QUEUE_MAX)
        def task(path):
            if stop_event.is_set():        # fast TERM: skip queued decodes entirely
                sem.release()
                return
            DECODE_GATE.acquire()          # thermal-adjustable decode-concurrency gate (8 -> 4 on SOFT)
            try:
                r = do_passa(path)
            except Exception as e:
                r = {"path": path, "ok": False, "err": "worker:" + str(e)[:180]}
            finally:
                DECODE_GATE.release()      # release the decode permit BEFORE the (blocking) hand-off
            q.put(r)              # blocks under backpressure -> throttles decode to VLM rate
            sem.release()
        for path in todo:
            if stop_event.is_set():
                break
            sem.acquire()
            ex.submit(task, path)
    q.put(SENTINEL)

def discard(r):
    """Free the frames of an un-scanned queue item (resume-safe: row never written)."""
    if r.get("ok"):
        for fp in (r.get("a") or {}).get("frame_paths") or []:
            try: os.remove(fp)
            except OSError: pass

def consumer(q, cnt):
    con = connect()
    last_hb = time.time()
    while True:
        r = q.get()
        if r is SENTINEL:
            break
        if stop_event.is_set():           # fast TERM / trip: drain WITHOUT VLM (fix 3)
            discard(r)
            cnt.discarded += 1
            continue
        GOV.maybe_cooldown()              # one-shot SOFT-stage VLM pause (stop-aware, self-clearing)
        when = now_iso()
        path = r["path"]
        if r["ok"]:
            m, a = r["meta"], r["a"]
            frames = a.get("frame_paths") or []
            b = vs.passb_from_frames(frames, m, passa=a)
            # feed the watchdog BEFORE writing so this row's vlm_device_ok is honest
            TW.record_vlm_ms(b.get("vlm_ms"))
            if a.get("cpu_fallback"):
                cnt.cpu_fb += 1
                TW.record_cpu_fallback(path, cnt.scanned, a.get("fallback_error"))
            write_ok(con, r, b, when, TW.device_ok(), cnt.windowed_cpm())
            cnt.ok += 1
            if a.get("passa_method") == "seek": cnt.seek += 1
            else:                          cnt.full += 1
            if b.get("dead_clip"):         cnt.dead += 1
            if b.get("parse_error"):       cnt.parse_err += 1
            if b.get("passb_error"):       cnt.passb_err += 1
            for fp in frames:             # frames consumed -> free disk
                try: os.remove(fp)
                except OSError: pass
        else:
            write_passa_error(con, path, r["err"], when, TW.device_ok(), cnt.windowed_cpm())
            cnt.passa_err += 1
        con.commit()                      # per-clip commit: cheap under WAL, max durability
        cnt.scanned += 1
        cnt.mark()
        cnt.last_path = path
        TW.maybe_probe()
        if cnt.scanned % HEARTBEAT_EVERY_CLIPS == 0 or (time.time() - last_hb) >= HEARTBEAT_EVERY_S:
            thermal = read_thermal_safe()      # ONE thermal sample per heartbeat ...
            GOV.tick(cnt, thermal)             # ... fed through the two-stage gate FSM (may soft/halt) ...
            write_heartbeat(cnt, "running", thermal)   # ... and reused by the heartbeat (no double read)
            last_hb = time.time()
            wcpm = cnt.windowed_cpm()
            log(f"scanned {cnt.scanned} (db {cnt.already + cnt.scanned}/{cnt.total}) "
                f"ok={cnt.ok} passa_err={cnt.passa_err} passb_err={cnt.passb_err} "
                f"parse_err={cnt.parse_err} fb={cnt.cpu_fb} "
                + (f"win={wcpm:.2f}cpm" if wcpm is not None else "win=n/a")
                + (f" med_vlm={TW.median_recent():.0f}ms"
                   if TW.median_recent() is not None else "")
                + (f" therm={thermal.get('temp_c')}C/{GOV.state}"
                   if thermal.get('temp_c') is not None else ""))
    con.commit(); con.close()

# ---------------------------------------------------------------- finalize

def group_dups():
    con = connect()
    rows = con.execute("""SELECT path,size_bytes,duration_s,dup_partial_sha FROM videos
                          WHERE sample=0 AND dup_partial_sha IS NOT NULL""").fetchall()
    groups = {}
    for path, size, dur, sha in rows:
        if sha is None or str(sha).startswith("ERR:"):
            continue
        groups.setdefault((size, round(dur or 0, 1), sha), []).append(path)
    gid = ndup = 0
    con.execute("UPDATE videos SET exact_dup_group=NULL WHERE sample=0")
    for members in groups.values():
        if len(members) > 1:
            gid += 1; ndup += len(members)
            for p in members:
                con.execute("UPDATE videos SET exact_dup_group=? WHERE path=?", (gid, p))
    con.commit(); con.close()
    return gid, ndup

def write_final_report(cnt, ndup_groups, ndup_files):
    snap = cnt.snapshot("done")
    body = f"""# {RUN_TOKEN} — COMPLETE

Full 35k video-signal scan finished on Worker5/charlie (GPU0, NVDEC + qwen2.5vl:7b).

## Result
- authoritative rows (sample=0): **{snap['done_total']}** / {snap['total']}
- scanned this run: {snap['scanned_this_run']}  (resumed from {snap['resumed_from']})
- wall this run: {snap['elapsed_hours']} h   avg {snap['clips_per_min_cumulative']} clips/min
- passA errors: {snap['passa_errors']}   passB errors: {snap['passb_errors']}   parse errors: {snap['parse_errors']}
- cpu fallbacks: {snap['cpu_fallbacks']}   dead clips: {snap['dead_clips']}
- Pass-A method: full={snap['method_full']} seek={snap['method_seek']}
- exact-dup: {ndup_groups} groups covering {ndup_files} files
- started {snap['started_at']}  finished {snap['updated_at']}
- DB: ~/loupe-ml/video/video_signals.db

{RUN_TOKEN}
"""
    try:
        atomic_write(os.path.join(RUN_DIR, "final_report.md"), body)
        atomic_write(DONE_MARKER, snap["updated_at"] + "\n")
        if not SMOKE:                     # smoke runs must never touch the relay lanes
            atomic_write(LATEST_RESP, body)
            with open(RESPONSES, "a") as f:
                f.write("\n---\n" + body)
    except Exception as e:
        log(f"final report write failed: {e}")

# ---------------------------------------------------------------- main

def handle_signal(signum, frame):
    log(f"signal {signum} received -> fast stop: finish in-flight clip(s) only, "
        f"drain queue without VLM (resume-safe)")
    stop_event.set()
    DECODE_GATE.set_limit(PASSA_WORKERS)   # release any thermal throttle so the drain is not slowed

def main():
    os.makedirs(vs.FRAME_DIR, exist_ok=True)
    os.makedirs(RUN_DIR, exist_ok=True)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()) + "\n")

    vs.init_db(DB_PATH)
    migrate_telemetry_cols()
    # clear transient frames orphaned by any prior crash (frames are per-clip, in-flight only)
    for fp in glob.glob(os.path.join(vs.FRAME_DIR, "*.jpg")):
        try: os.remove(fp)
        except OSError: pass

    allpaths, todo, already = load_todo()
    total = len(allpaths)
    log(f"list={total} already_done(sample=0)={already} todo={len(todo)}"
        + (f" (LIMIT={LIMIT})" if LIMIT else ""))
    cnt = Counters(total, already)

    if not todo:
        log("nothing to do — all clips already scanned. Finalizing.")
        g, n = group_dups()
        write_final_report(cnt, g, n)
        write_heartbeat(cnt, "done")
        log(f"DONE (nothing to scan). dup groups={g} files={n}")
        return

    # warm the VLM so the first clips aren't cold-load-skewed
    log("warming qwen2.5vl:7b ...")
    try: vs.ollama_generate([], timeout=180)
    except Exception as e: log(f"warm call failed: {e}")

    # pre-flight device probe: refuse to start on a sick device
    ok, detail = probe_gpu_ollama()
    TW.last_probe_ok, TW.last_probe_detail, TW.last_probe_t = ok, detail, time.time()
    log(f"preflight probe: ok={ok} — {detail}")
    if not ok:
        TW.trip("preflight: " + detail)
        write_heartbeat(cnt, "halted-tripwire")
        log("HALTED before scan start (preflight probe failed). Exiting nonzero.")
        sys.exit(2)

    log(f"tripwires ARMED (DISCRIMINATING, STORAGE-AWARE): median vlm_ms over last {VLM_WINDOW} "
        f"calls > {VLM_SICK_MS} ms -> halt; Pass-A cpu_fallback -> probe (GPU-UUID + ollama VRAM "
        f"+ {'/'.join(CANARY_CODECS)} cuvid canary decode): canaries FAIL -> halt degraded, "
        f"canaries PASS -> classify io_fallback (storage/CIFS EIO on {CIFS_MOUNT}), log + CONTINUE "
        f"(NEVER halts; soft WARN at {IO_FALLBACK_WARN_IN_WINDOW} in {FALLBACK_WINDOW_CLIPS} clips / "
        f"{FALLBACK_WINDOW_S // 60} min); GPU-UUID + ollama VRAM probe every {PROBE_EVERY_S}s -> halt "
        f"(PCIe Replays Since Reset logged watch-only). "
        f"Halt = heartbeat status=halted-tripwire + nonzero exit. Never grind at CPU speed.")

    log(f"THERMAL GATE ARMED (two-stage, local, per-heartbeat, {THERM_SUSTAIN_N} sustained): "
        f"SOFT at temp_c>={THERM_SOFT_C}C -> Pass-A workers {PASSA_WORKERS}->{THERM_SOFT_WORKERS} "
        f"+ {THERM_COOLDOWN_S}s cooldown (recover <{THERM_SOFT_CLEAR_C}C); "
        f"HARD at temp_c>={THERM_HARD_C}C OR T.Limit margin<={THERM_MARGIN_HARD}C -> graceful "
        f"resume-safe stop (status=thermal_halt). Pager lives in the separate thermal_sentinel.py.")

    write_heartbeat(cnt, "running")
    q = queue.Queue(maxsize=QUEUE_MAX)
    prod = threading.Thread(target=producer, args=(todo, q), daemon=True)
    prod.start()
    consumer(q, cnt)               # runs in main thread until SENTINEL / stop
    prod.join(timeout=30)

    if TW.tripped:
        write_heartbeat(cnt, "halted-tripwire")
        log(f"HALTED by tripwire after {cnt.scanned} clips this run: {TW.tripped} "
            f"(db {cnt.already + cnt.scanned}/{cnt.total}, {cnt.discarded} discarded). "
            f"Exiting nonzero — do NOT trust rows with vlm_device_ok=0.")
        sys.exit(2)

    if GOV.halt_reason:                # thermal HARD halt: graceful + resume-safe (not a fault)
        write_heartbeat(cnt, "thermal_halt")
        log(f"THERMAL HALT after {cnt.scanned} clips this run: {GOV.halt_reason} "
            f"(db {cnt.already + cnt.scanned}/{cnt.total}, {cnt.discarded} discarded un-scanned). "
            f"Resume-safe — relaunch (same token) to continue from {cnt.already + cnt.scanned}.")
        return

    if stop_event.is_set():
        write_heartbeat(cnt, "stopped")
        log(f"STOPPED after {cnt.scanned} clips this run (resume-safe, "
            f"{cnt.discarded} queued clips discarded un-scanned). "
            f"db {cnt.already + cnt.scanned}/{cnt.total}")
        return

    log("scan pass complete — grouping exact dups ...")
    g, n = group_dups()
    log(f"exact-dup: {g} groups covering {n} files")
    write_final_report(cnt, g, n)
    write_heartbeat(cnt, "done")
    log(f"DONE. scanned_this_run={cnt.scanned} db_total={cnt.already + cnt.scanned}/{cnt.total} "
        f"ok={cnt.ok} passa_err={cnt.passa_err} passb_err={cnt.passb_err} parse_err={cnt.parse_err}")

if __name__ == "__main__":
    main()
