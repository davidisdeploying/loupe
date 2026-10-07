#!/usr/bin/env python3
"""MOVE matched 'elsewhere' originals -> /mnt/nas2/photos/long-video-elsewhere/{year}/
via os.rename (same-volume rename, NOT copy+delete). os.rename raises EXDEV on a
cross-device move, so a successful sub-second rename proves same-volume.

Usage:
  move_elsewhere.py --one    # move ONLY the first file, record timing, STOP
  move_elsewhere.py --rest   # move all remaining (skips ones already done)
"""
import json, os, sys, time

RES   = "/home/david/loupe-pipeline/culling/_match_results_elsewhere.json"
DESTROOT = "/mnt/nas2/photos/long-video-elsewhere"
STATE = "/home/david/loupe-pipeline/culling/_move_state_elsewhere.json"
LOG   = "/home/david/loupe-pipeline/culling/_move_elsewhere.log"
TIMING = "/home/david/loupe-pipeline/culling/_step1_timing_elsewhere.json"
INSTANT_THRESHOLD = 1.0  # seconds; a same-volume rename is ~instant, copy+delete is not

def dest_for(r):
    return os.path.join(DESTROOT, str(r["year"]), os.path.basename(r["originals_path"]))

def load_state():
    if os.path.exists(STATE):
        return json.load(open(STATE))
    return {}

def save_state(s):
    json.dump(s, open(STATE, "w"), indent=1)

def logline(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def move_one(r, state):
    src = r["originals_path"]
    dst = dest_for(r)
    if not os.path.exists(src):
        raise FileNotFoundError(f"source missing: {src}")
    if os.path.exists(dst):
        raise FileExistsError(f"dest already exists, refusing to overwrite: {dst}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    t0 = time.perf_counter()
    os.rename(src, dst)          # raises OSError(EXDEV) if cross-device
    dt = time.perf_counter() - t0
    # verify landed + size preserved
    sz = os.path.getsize(dst)
    assert sz == r["db_size"], f"size changed after rename {sz} != {r['db_size']} for {dst}"
    assert not os.path.exists(src), f"source still present after rename: {src}"
    state[r["uuid"]] = {"dest": dst, "seconds": dt, "size": sz}
    return dst, dt, sz

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    res = json.load(open(RES))
    assert all(r["status"] == "OK" for r in res), "non-OK match present; aborting"
    state = load_state()

    if mode == "--one":
        r = res[0]
        dst, dt, sz = move_one(r, state)
        save_state(state)
        json.dump({"moved_uuid": r["uuid"], "dest": dst, "seconds": dt, "size": sz},
                  open(TIMING, "w"), indent=1)
        logline(f"[ONE] MOVED {r['originals_path']} -> {dst} ({sz/1e9:.2f} GB) in {dt:.4f}s")
        if dt < INSTANT_THRESHOLD:
            logline(f"[ONE] INSTANT ({dt:.4f}s < {INSTANT_THRESHOLD}s) -> same-volume rename PROVEN")
            sys.exit(0)
        else:
            logline(f"[ONE] NOT INSTANT ({dt:.4f}s) -> STOP")
            sys.exit(3)

    elif mode == "--rest":
        todo = [r for r in res if r["uuid"] not in state]
        logline(f"START --rest: {len(todo)} to move, {len(state)} already done")
        moved = 0
        total_bytes = 0
        for i, r in enumerate(todo, 1):
            dst, dt, sz = move_one(r, state)
            total_bytes += sz
            moved += 1
            if i % 25 == 0 or i == len(todo):
                save_state(state)
            logline(f"[{i}/{len(todo)}] MOVED {os.path.basename(r['originals_path'])} -> {dst} ({sz/1e9:.2f} GB, {dt:.4f}s)")
        save_state(state)
        logline(f"DONE --rest moved={moved} total={total_bytes/1e9:.2f} GB")
        sys.exit(0)

    else:
        print(__doc__)
        sys.exit(2)

if __name__ == "__main__":
    main()
