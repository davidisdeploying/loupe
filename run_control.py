#!/usr/bin/env python3
"""
run_control.py — the stage table + start()/status() for /setup-triggered compute runs.

Imported by server.py. Owns nothing at runtime except files on disk: each stage has a
durable marker (written by the detached stage_runner) and a log. The marker is the
single source of truth — status() READS it fresh every call (the runner is a separate
process; there is no in-memory mirror to drift).

Single-flight has two layers:
  1. setup_status._alive(needle) — the exact /proc cmdline scan the console uses (reused,
     not reimplemented), so a hand-run ingest is detected too.
  2. an O_EXCL <marker>.lock — closes the race between the _alive check and the spawned
     process actually appearing in /proc. A lock whose marker is non-terminal AND whose
     process is still alive is a real run; a lock with a dead/terminal run is stale and
     reclaimed.

This pass wires "develop" (ingest) ONLY, but STAGES is structured so thumbs/faces drop
in later as sibling rows.
"""
import os
import subprocess
import time

import setup_status                       # status_model() for the console's computed done/total
from setup_status import _alive          # REUSE the exact /proc scan — do not reimplement
from loupe_common import V2, APP_DATA, METADATA_DB, PIPELINE_DIR, ro

_DIR = os.path.dirname(os.path.abspath(__file__))          # ~/loupe
# Every pipeline stage runs under /data/loupe-venv (Python 3.12.13). This was
# "/usr/bin/python3" plus a separate .faces-venv while Loupe lived on delta,
# where system python WAS 3.12.3 and both resolved correctly. On charlie (moved
# 2026-08-07) /usr/bin/python3 is 3.14.4 and carries none of numpy, PIL,
# pillow_heif, cv2, imagehash, insightface, nudenet or onnxruntime; .faces-venv
# pins 3.12.3 in its pyvenv.cfg and was orphaned by the same version skew, so
# none of its packages imported either. /data/loupe-venv supplies the whole set
# including pillow-heif, so faces no longer needs a private venv.
PY = "/data/loupe-venv/bin/python"
# Kept as a distinct name because stage commands select their interpreter
# per-stage (argv[0]); it simply resolves to the same environment now.
FACES_PY = PY
SETTINGS_PATH = os.path.join(APP_DATA, "loupe-settings.json")
RUN_STATUS_DIR = os.path.join(APP_DATA, "run-status")
RUN_LOGS_DIR = os.path.join(APP_DATA, "run-logs")
STAGE_RUNNER = os.path.join(_DIR, "stage_runner.py")

_TERMINAL = ("done", "failed")


# --- portable library-root resolution -------------------------------------
def _settings():
    try:
        import json
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _library_root():
    """saved loupe-settings library_root → LIBRARY_ROOT env → /mnt/nas2/photos.
    Matches validate_library_path()'s convention: the saved root holds an originals/
    subdir, so ingest_root = <root>/originals."""
    saved = _settings().get("library_root")
    if saved:
        return saved
    return os.environ.get("LIBRARY_ROOT") or "/mnt/nas2/photos"


# --- stage table ----------------------------------------------------------
def _needles(sd):
    """A stage's liveness needles as a tuple (one OR many substrings)."""
    n = sd["needle"]
    return tuple(n) if isinstance(n, (list, tuple)) else (n,)


def _stage_def(stage):
    """Resolve a stage's spec at call time (library_root comes from settings, so a
    re-pointed library ports without code change). Returns None for unknown stages.
    Each entry's argv[0] is the per-stage interpreter (faces uses the venv python)."""
    lib = _library_root()
    ingest_root = os.path.join(lib, "originals")
    table = {
        # Develop — ingest.py: extract metadata for every original.
        "develop": {
            "stage": "develop",
            "needle": "ingest.py",
            "cwd": V2,                       # ~/loupe-pipeline (DATA: metadata.db + vendor/ live here)
            # SOURCE now runs from the pipeline/ subtree; --db is explicit and ingest derives
            # vendor/exiftool from the --db dir (V2), so cwd=V2 is preserved but not relied on.
            "argv": [PY, os.path.join(PIPELINE_DIR, "ingest.py"), "--root", ingest_root,
                     "--db", METADATA_DB, "--workers", "4"],
            "env_overlay": {"LOUPE_REQUIRE_MOUNT": "1", "LIBRARY_ROOT": lib},
            "marker": os.path.join(RUN_STATUS_DIR, "develop.status.json"),
            "log": os.path.join(RUN_LOGS_DIR, "develop.log"),
        },
        # Make contact prints — pregen.py (runs gen_thumbs internally). cwd=~/loupe.
        "contact_prints": {
            "stage": "contact_prints",
            "needle": ("pregen.py", "gen_thumbs.py"),
            "cwd": _DIR,                     # ~/loupe (pregen.py lives here)
            "argv": [PY, "pregen.py"],
            "env_overlay": {"PYTHONUNBUFFERED": "1"},
            "marker": os.path.join(RUN_STATUS_DIR, "contact_prints.status.json"),
            "log": os.path.join(RUN_LOGS_DIR, "contact_prints.log"),
        },
        # Spot the faces — faces_pipeline.py under the venv python (insightface lives there).
        "faces": {
            "stage": "faces",
            "needle": "faces_pipeline.py",
            "cwd": _DIR,                     # ~/loupe
            # --provider auto: faces_pipeline defaults to CPU, and this stage never
            # overrode it, so every in-app run embedded on CPU while a 16 GB card sat
            # idle -- ~0.63 s/asset. faces.db already holds both CUDA and CPU embeddings
            # of the same model, so preferring CUDA changes throughput, not semantics.
            # "auto" resolves to whatever onnxruntime really offers, so a host without
            # CUDA still runs.
            "argv": [FACES_PY, "-u", "faces_pipeline.py", "--all", "--provider", "auto"],
            "env_overlay": {},               # faces self-sets OMP_NUM_THREADS; no mount needed
            "marker": os.path.join(RUN_STATUS_DIR, "faces.status.json"),
            "log": os.path.join(RUN_LOGS_DIR, "faces.log"),
        },
        # NSFW/nudity scan — nsfw_pipeline.py under the venv python (nudenet lives there);
        # reads the local thumb cache, writes nsfw.db. Optional, on-device, images-only.
        "nsfw": {
            "stage": "nsfw",
            "needle": "nsfw_pipeline.py",
            "cwd": _DIR,                     # ~/loupe
            "argv": [FACES_PY, "-u", "nsfw_pipeline.py", "--all"],
            "env_overlay": {},               # nsfw self-sets OMP_NUM_THREADS; no mount needed
            "marker": os.path.join(RUN_STATUS_DIR, "nsfw.status.json"),
            "log": os.path.join(RUN_LOGS_DIR, "nsfw.log"),
        },
    }
    return table.get(stage)


def stages():
    return ["develop", "contact_prints", "faces", "nsfw"]


# --- marker + count helpers ----------------------------------------------
def _read_marker(path):
    try:
        import json
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _count():
    """The SAME signal setup_status uses for Develop: COUNT(*) FROM assets, mode=ro."""
    try:
        c = ro(METADATA_DB)
        try:
            row = c.execute("SELECT count(*) FROM assets").fetchone()
            return (row[0] if row else 0) or 0
        finally:
            c.close()
    except Exception:
        return 0


# --- public API -----------------------------------------------------------
def start(stage):
    """Single-flighted, detached spawn of a stage. Never spawns a second process for a
    stage already running (hand-run or ours). Returns a small dict for the HTTP layer."""
    sd = _stage_def(stage)
    if sd is None:
        return None                                  # caller maps to 400
    needles, marker = _needles(sd), sd["marker"]

    # Layer 1: already running (ours or a hand-run) → report, do not spawn.
    if _alive(*needles):
        return {"stage": stage, "state": "running", "note": "already running"}

    os.makedirs(RUN_STATUS_DIR, exist_ok=True)
    os.makedirs(RUN_LOGS_DIR, exist_ok=True)
    lock = marker + ".lock"

    # Layer 2: O_EXCL lock closes the _alive→/proc race.
    def _acquire():
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return True
        except FileExistsError:
            return False

    if not _acquire():
        m = _read_marker(marker)
        nonterminal = bool(m) and m.get("state") not in _TERMINAL
        if nonterminal and _alive(*needles):
            return status(stage)                     # a genuine in-flight run
        # stale lock: terminal/dead run — reclaim it
        try:
            os.unlink(lock)
        except OSError:
            pass
        if not _acquire():
            return status(stage)                     # lost a concurrent reclaim race

    # Re-check liveness now that we hold the lock (someone may have started between
    # the top check and the lock grab).
    if _alive(*needles):
        try:
            os.unlink(lock)
        except OSError:
            pass
        return {"stage": stage, "state": "running", "note": "already running"}

    now = int(time.time())
    # Initial running marker BEFORE the spawn, so a racing start sees a non-terminal
    # marker. The lock is intentionally LEFT in place; it is reclaimed on next start
    # once the run is terminal/dead, which keeps the marker the real guard.
    try:
        import json
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        tmp = marker + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"stage": stage, "state": "running", "pid": None, "attempt": 0,
                       "started_at": now, "finished_at": None, "error": None,
                       "raw_tail": None}, f)
        os.replace(tmp, marker)
    except Exception:
        pass

    # Spawn the runner in its OWN transient scope (a separate cgroup), NOT a bare setsid
    # child: loupe.service is KillMode=control-group, so a setsid child is still SIGTERM'd
    # when loupe restarts (e.g. the enrich-import worker's own restart). --scope detaches
    # into an independent cgroup that survives the restart; --collect auto-cleans the unit
    # on exit (no reset-failed needed); --quiet drops the "Running as unit …" line.
    # systemd-run consumes the first --; stage_runner then gets its flags and the wrapped
    # command after its own second --. (Survival across logout relies on linger, which is
    # already on for this user — loupe.service itself survives reboot.)
    cmd = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--",
           PY, STAGE_RUNNER, "--stage", stage, "--marker", marker,
           "--log", sd["log"], "--cwd", sd["cwd"], "--", *sd["argv"]]
    subprocess.Popen(cmd,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     env={**os.environ, **sd["env_overlay"]})
    return {"stage": stage, "state": "starting"}


# --- W14: stall detection -------------------------------------------------
# The marker records state/pid/started_at but nothing that ADVANCES, so a hung stage is
# indistinguishable from a working one -- that is the gap W14 names. The stage log is
# already an append-only progress stream, so its mtime is a free heartbeat: no new bytes
# for STALL_IDLE_SECONDS while the marker still claims "running" means stalled.
#
# Deliberately read-only. It never touches the marker, never signals the runner, and
# never raises: a monitor that crashes is worse than no monitor at all.
STALL_IDLE_SECONDS = int(os.environ.get("LOUPE_STAGE_STALL_SECONDS", "1800"))


def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def liveness(stage, max_idle=None):
    """Verdict for one stage: idle | running | stalled | crashed | done | failed.

    'crashed' and 'stalled' are kept apart on purpose. A dead pid with the marker still
    reading "running" means the runner died without recording it -- the marker is lying,
    and no amount of waiting fixes it. A live pid with a log that stopped advancing means
    the work is wedged. They want different responses.
    """
    idle_limit = STALL_IDLE_SECONDS if max_idle is None else int(max_idle)
    sd = _stage_def(stage)
    if sd is None:
        return None
    m = _read_marker(sd["marker"]) or {}
    state = m.get("state") or "idle"
    pid = m.get("pid")
    log = sd.get("log")
    try:
        log_age = time.time() - os.path.getmtime(log) if log and os.path.exists(log) else None
    except OSError:
        log_age = None

    out = {"stage": stage, "state": state, "pid": pid,
           "pid_alive": _pid_alive(pid), "log": log,
           "log_age_s": None if log_age is None else int(log_age),
           "idle_limit_s": idle_limit, "verdict": state, "reason": None}

    if state != "running":
        out["verdict"] = state
        return out
    if not out["pid_alive"]:
        out["verdict"] = "crashed"
        out["reason"] = ("marker still says running but pid %s is gone; the runner died "
                         "without recording it" % pid)
        return out
    if log_age is not None and log_age > idle_limit:
        out["verdict"] = "stalled"
        out["reason"] = ("pid %s is alive but %s has not advanced in %d s (limit %d)"
                         % (pid, os.path.basename(log or "?"), int(log_age), idle_limit))
        return out
    out["verdict"] = "running"
    return out


def liveness_all(max_idle=None):
    return [liveness(st, max_idle) for st in stages()]


def _cli_check(argv):
    """`python3 run_control.py --check-stalled [seconds]` -> exit 1 if anything is wrong.

    Exists so a monitor can ask a yes/no question without importing the module or
    parsing a marker, and so the answer is about progress rather than exit status --
    the same mistake that hid the ledger outage for three days."""
    limit = int(argv[1]) if len(argv) > 1 else None

    # Fail loudly rather than reporting "clean" from the wrong directory. Run without
    # DATA_ROOT set, RUN_STATUS_DIR resolves under ~/loupe instead of the state root,
    # every marker read misses, every stage reads "idle" and the check passes -- a
    # monitor that is reassuring because it is looking in an empty directory. That is
    # the same shape as the ledger unit reporting SUCCESS while producing no snapshot,
    # and it is worth refusing to run at all.
    if not os.path.isdir(RUN_STATUS_DIR):
        print("run-status directory does not exist: %s" % RUN_STATUS_DIR)
        print("DATA_ROOT is %s" % (os.environ.get("DATA_ROOT") or "UNSET"
                                   + " -- the service sets it; set it here too"))
        return 2
    markers = [f for f in os.listdir(RUN_STATUS_DIR) if f.endswith(".status.json")]
    if not markers:
        print("no stage markers in %s -- refusing to report 'clean' from an empty "
              "directory" % RUN_STATUS_DIR)
        return 2

    bad = 0
    for r in liveness_all(limit):
        if r is None:
            continue
        line = "  %-16s %-8s" % (r["stage"], r["verdict"])
        if r["log_age_s"] is not None:
            line += " log_idle=%ss" % r["log_age_s"]
        if r["reason"]:
            line += "\n      " + r["reason"]
        print(line)
        if r["verdict"] in ("stalled", "crashed", "failed"):
            bad += 1
    print()
    print("STAGE LIVENESS: %s" % ("clean" if not bad else "%d stage(s) need attention" % bad))
    return 1 if bad else 0


def status(stage):
    """Merge the durable marker with the live COUNT signal. Marker is read fresh from
    disk (the runner is a separate process). Returns None for unknown stages."""
    sd = _stage_def(stage)
    if sd is None:
        return None                                  # caller maps to 404
    needles = _needles(sd)
    m = _read_marker(sd["marker"])

    # done/total come from the console's ALREADY-computed model, not a fresh recompute:
    # status_model()["stages"]→develop carries done=assets and total=dev_total (the cached
    # originals-walk count), so the card's denominator matches the console and triggers NO
    # new CIFS walk. Fall back to the bare COUNT only if the model is unavailable.
    done = total = None
    try:
        by_id = {x["id"]: x for x in setup_status.status_model().get("stages", [])}
        dv = by_id.get(stage)
        if dv:
            p = dv.get("progress") or {}
            done, total = p.get("done"), p.get("total")
    except Exception:
        pass
    if done is None:
        # Fallback only if the model was unavailable. _count() is the develop signal
        # (COUNT assets); for thumbs/faces there's no cheap server-side count, so 0.
        done = _count() if stage == "develop" else 0
    if total is None:
        total = done
    pct = round(100.0 * done / total, 1) if total else 0.0

    # State precedence (highest first): count-done wins, then marker terminal/retry, then
    # liveness, then idle. (A no-marker box with a full library reads "done".)
    if (total and done >= total * 0.995) or (m and m.get("state") == "done"):
        state = "done"
    elif m and m.get("state") == "failed":
        state = "failed"
    elif m and m.get("state") == "retrying":
        state = "retrying"
    elif _alive(*needles) or (m and m.get("state") in ("running", "starting")):
        state = "running"
    else:
        state = "idle"

    out = {
        "stage": stage, "state": state, "done": done, "total": total, "pct": pct,
        "error": (m or {}).get("error"),
        "attempt": (m or {}).get("attempt"),
        "started_at": (m or {}).get("started_at"),
    }
    if state == "failed" and m and m.get("raw_tail"):
        out["raw_tail"] = m["raw_tail"]
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--check-stalled":
        sys.exit(_cli_check(sys.argv[1:]))
    print("usage: run_control.py --check-stalled [idle_seconds]")
    sys.exit(2)
