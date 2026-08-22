#!/usr/bin/env python3
"""
Duration backfill — the ONE approved DB write for this phase.

UPDATEs ONLY assets.duration_seconds, for video rows (MOV/MP4/M4V) where it
is NULL. Never touches any other column, never touches source files
(ffprobe header reads only).

Resumable: rows with duration_seconds already set are skipped on restart.
Batched commits every BATCH rows. Progress every 100. Failures logged and
left NULL (re-attempted on a future run; persistent failures listed in log).

Run inside tmux:
    tmux new -s durfill 'python3 ~/loupe/pipeline/backfill_duration.py'
"""

import os, sqlite3, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
# Portable roots — env-unset reproduces the historical layout (see ONBOARDING.md).
PROJ = os.environ.get("DATA_ROOT", HERE)
LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", os.path.join(os.sep, "mnt", "nas", "photos"))
DB = os.path.join(PROJ, "metadata.db")
LOG = os.path.join(PROJ, "logs", f"durfill-{time.strftime('%Y%m%d-%H%M')}.log")
# Optional CIFS-mount guard. OFF by default; LOUPE_REQUIRE_MOUNT=1 re-enables the abort.
MOUNT_SENTINEL = os.environ.get(
    "MOUNT_SENTINEL", os.path.join(LIBRARY_ROOT, "originals", ".mounted"))
REQUIRE_MOUNT = os.environ.get("LOUPE_REQUIRE_MOUNT", "") not in ("", "0", "false", "False")
WORKERS = int(os.environ.get("BACKFILL_WORKERS") or (os.cpu_count() or 4))
BATCH = 200
VIDEO_EXT = ("MOV", "MP4", "M4V")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def probe(job):
    rid, fp = job
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", fp],
            capture_output=True, text=True, timeout=120)
        out = r.stdout.strip()
        if r.returncode == 0 and out and out != "N/A":
            return rid, float(out), None
        return rid, None, f"ffprobe rc={r.returncode} out={out!r} err={r.stderr.strip()[:120]}"
    except Exception as e:
        return rid, None, f"{type(e).__name__}: {e}"


def main():
    if REQUIRE_MOUNT and not os.path.exists(MOUNT_SENTINEL):
        sys.exit("NAS mount sentinel missing — aborting before any work. "
                 "(Set LOUPE_REQUIRE_MOUNT=0 to disable this guard.)")

    con = sqlite3.connect(DB, timeout=60)
    cur = con.cursor()
    ph = ",".join("?" * len(VIDEO_EXT))
    # bind sorted(VIDEO_EXT) — sqlite rejects a frozenset as a param sequence; the values
    # are upper-case, matching the (upper-case) extension column.
    todo = cur.execute(
        f"SELECT id, filepath FROM assets WHERE extension IN ({ph}) "
        "AND duration_seconds IS NULL ORDER BY id", sorted(VIDEO_EXT)).fetchall()
    total = len(todo)
    log(f"start: {total} videos need duration (resumable; workers={WORKERS})")

    done = errs = 0
    pending = []
    t0 = time.time()

    def flush():
        nonlocal pending
        if pending:
            con.executemany(
                "UPDATE assets SET duration_seconds=? WHERE id=?", pending)
            con.commit()
            pending = []

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for rid, dur, err in ex.map(probe, todo, chunksize=8):
            done += 1
            if err is None:
                pending.append((dur, rid))
                if len(pending) >= BATCH:
                    flush()
            else:
                errs += 1
                log(f"ERR id={rid}: {err}")
            if done % 100 == 0:
                rate = done / (time.time() - t0)
                eta = (total - done) / rate if rate else 0
                log(f"progress {done}/{total} ({100*done/total:.1f}%) "
                    f"errors={errs} eta={int(eta//3600)}:{int(eta%3600//60):02d}")
            if REQUIRE_MOUNT and done % 1000 == 0 and not os.path.exists(MOUNT_SENTINEL):
                flush()
                sys.exit("NAS mount lost — committed work so far, exiting.")

    flush()
    n = cur.execute("SELECT COUNT(*) FROM assets WHERE duration_seconds "
                    "IS NOT NULL").fetchone()[0]
    log(f"DONE in {(time.time()-t0)/60:.1f} min — {done} probed, {errs} errors, "
        f"{n} rows now have duration")
    con.close()


if __name__ == "__main__":
    main()
