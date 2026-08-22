#!/usr/bin/env python3
"""
thumb_watchdog.py — supervise gen_thumbs.py: auto-rerun if failures spike / the
NAS blips. Read-only on the library; only ever (re)launches the resumable,
skip-existing thumbnail generator. Safe to kill anytime.

Behavior:
  * Adopts an already-running gen_thumbs (won't double-launch).
  * STALL = thumb count flat while a run is alive (failures spiking / NAS down)
    -> kill the run; next tick relaunches (resumable).
  * After a run that adds 0 new thumbs, backs off exponentially (60s -> 20min cap)
    since either the NAS is still down or the remainder is unreadable/corrupt.
  * Exits clean once every candidate has a thumbnail.
"""

import os
import subprocess
import sys
import time

import candidates as C

THUMBS = C.THUMBS
GENLOG = os.path.join(C.CULL, "contactsheets", "_genthumbs.log")
LOG = os.path.join(C.CULL, "contactsheets", "_watchdog.log")
CHECK = 60          # seconds between checks
STALL_LIMIT = 600   # flat-while-running this long -> kill & relaunch
BACKOFF_CAP = 1200  # 20 min


def log(m):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def have_count(target):
    try:
        h = {int(f[:-4]) for f in os.listdir(THUMBS) if f.endswith(".jpg")}
    except FileNotFoundError:
        h = set()
    return len(h & target)


def gen_running():
    out = subprocess.run(["pgrep", "-f", "gen_thumbs.py"],
                         capture_output=True, text=True).stdout
    return bool(out.strip())


def start_gen():
    f = open(GENLOG, "a")
    subprocess.Popen([sys.executable, "gen_thumbs.py"], cwd=C.HERE,
                     stdout=f, stderr=subprocess.STDOUT, start_new_session=True)


def main():
    _, _, by = C.load_all()
    target = set(by.keys())
    n = len(target)
    log(f"watchdog up; target={n} candidates; thumbs now={have_count(target)}")

    backoff = 60
    last_count = have_count(target)
    flat_since = time.time()
    run_start = last_count          # thumb count when the current run began
    ticks = 0

    if gen_running():
        log("adopting already-running gen_thumbs")
    else:
        start_gen(); run_start = last_count; log(f"launched gen_thumbs; missing={n-last_count}")

    while True:
        time.sleep(CHECK)
        ticks += 1
        cur = have_count(target)
        missing = n - cur
        if missing <= 0:
            log(f"ALL THUMBNAILS COMPLETE ({cur}/{n}). watchdog exiting.")
            return

        if gen_running():
            if cur > last_count:
                last_count = cur
                flat_since = time.time()
            elif time.time() - flat_since > STALL_LIMIT:
                log(f"STALL: {cur} thumbs flat {STALL_LIMIT}s (missing {missing}) — "
                    f"failures likely spiking / NAS blip; killing gen to relaunch")
                subprocess.run(["pkill", "-f", "gen_thumbs.py"])
                time.sleep(3)
            if ticks % 5 == 0:
                log(f"heartbeat: {cur}/{n} thumbs, missing {missing} (run active)")
        else:
            delta = cur - run_start
            if delta > 0:
                backoff = 60
                log(f"gen run ended: +{delta} thumbs, missing {missing} — relaunching")
            else:
                backoff = min(backoff * 2, BACKOFF_CAP)
                log(f"gen run ended with NO new thumbs, missing {missing} "
                    f"(NAS down or remainder unreadable) — retry in {backoff}s")
                time.sleep(backoff)
            start_gen()
            run_start = cur; last_count = cur; flat_since = time.time()
            log(f"relaunched gen_thumbs; missing={missing}")


if __name__ == "__main__":
    main()
