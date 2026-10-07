#!/usr/bin/env python3
"""Copy matched production files originals -> production/{year}/, verify SHA-256 vs DB.
Resumable: a file already present at dest with a matching hash is skipped.
Read-only w.r.t. /originals and metadata.db (we only READ the precomputed sha from JSON)."""
import json, os, shutil, hashlib, sys, time

PROD = "/mnt/nas2/photos/production"
MATCH = "/home/david/loupe-pipeline/culling/_match_results.json"
STATE = "/home/david/loupe-pipeline/culling/_copy_state.json"
LOG = "/home/david/loupe-pipeline/culling/_copy.log"
MAX_RETRIES = 3

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with open(LOG, "a") as f: f.write(line + "\n")
    print(line, flush=True)

def sha256(path, buf=8*1024*1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b: break
            h.update(b)
    return h.hexdigest()

res = json.load(open(MATCH))
state = json.load(open(STATE)) if os.path.exists(STATE) else {}

log(f"START copy+verify of {len(res)} files")
done = 0; failed = []
for i, r in enumerate(res, 1):
    uuid = r["uuid"]
    src = r["originals_path"]
    expect = r["file_sha256"] if "file_sha256" in r else r["sha256"]
    year = r["year"]
    fname = os.path.basename(src)
    ddir = os.path.join(PROD, str(year))
    dest = os.path.join(ddir, fname)

    st = state.get(uuid, {})
    if st.get("verified") and os.path.exists(dest):
        done += 1
        continue

    os.makedirs(ddir, exist_ok=True)
    ok = False; actual = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sz = os.path.getsize(src)
            log(f"[{i}/{len(res)}] copy {fname} ({sz/1e9:.2f} GB) attempt {attempt}")
            shutil.copyfile(src, dest)
            actual = sha256(dest)
            if actual == expect:
                ok = True
                break
            log(f"  HASH MISMATCH {fname}: got {actual[:16]} expect {expect[:16]} (retry)")
        except Exception as e:
            log(f"  ERROR {fname}: {e} (retry)")
    state[uuid] = {
        "dest": dest, "src": src, "sha256": expect,
        "actual": actual,
        "verified": ok, "size": os.path.getsize(src),
    }
    json.dump(state, open(STATE, "w"), indent=1)
    if ok:
        done += 1
        log(f"  OK verified {fname}")
    else:
        failed.append(fname)
        log(f"  FAILED {fname}")

log(f"DONE. verified={done}/{len(res)} failed={len(failed)}")
if failed:
    log("FAILED LIST: " + ", ".join(failed))
