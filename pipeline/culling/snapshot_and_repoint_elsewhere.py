#!/usr/bin/env python3
"""STEP 4: snapshot metadata.db to a NEW dated NAS file (validate), THEN repoint the
209 'elsewhere' rows' filepath originals -> long-video-elsewhere. Each UPDATE guarded
by id + matching OLD originals filepath; auto-rollback if updated count != matched count.
"""
import json, os, sqlite3, sys, shutil

DB    = "/home/david/loupe-pipeline/metadata.db"
RES   = "/home/david/loupe-pipeline/culling/_match_results_elsewhere.json"
SNAP  = "/home/david/loupe-archive/metadata-backups/metadata-backup-20260615-pre-elsewhere-repoint.db"
LOCAL_SNAP = "/home/david/loupe-pipeline/culling/_snap_20260615.db"  # fast local staging
DESTROOT = "/mnt/nas2/photos/long-video-elsewhere"

def dest_for(r):
    return os.path.join(DESTROOT, str(r["year"]), os.path.basename(r["originals_path"]))

res = json.load(open(RES))
assert all(r["status"] == "OK" for r in res), "non-OK match present; aborting"
matched = len(res)
print(f"matched rows to repoint: {matched}")

# ---------- STEP 4a: SNAPSHOT FIRST ----------
if os.path.exists(SNAP):
    print(f"ERROR: snapshot path already exists, refusing to overwrite: {SNAP}")
    sys.exit(2)
if os.path.exists(LOCAL_SNAP):
    os.remove(LOCAL_SNAP)

src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
src_assets = src.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
print(f"source assets row count: {src_assets}")

# 1) consistent copy via online backup API to LOCAL disk (fast; folds WAL in -> single .db)
dst = sqlite3.connect(LOCAL_SNAP)
with dst:
    src.backup(dst)
dst.close()
src.close()
print(f"local snapshot written: {LOCAL_SNAP} ({os.path.getsize(LOCAL_SNAP)} bytes)")

# validate the LOCAL copy
loc = sqlite3.connect(f"file:{LOCAL_SNAP}?mode=ro", uri=True)
integ = loc.execute("PRAGMA integrity_check").fetchone()[0]
loc_assets = loc.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
loc.close()
print(f"local snapshot integrity_check: {integ}")
print(f"local snapshot assets row count: {loc_assets}")
if integ != "ok":
    print("ERROR: integrity_check failed; aborting before any DB write"); sys.exit(3)
if loc_assets != src_assets:
    print(f"ERROR: row count mismatch source={src_assets} local={loc_assets}; aborting"); sys.exit(3)

# 2) stream-copy the single validated file sequentially to the NAS
shutil.copyfile(LOCAL_SNAP, SNAP)
local_bytes = os.path.getsize(LOCAL_SNAP)
snap_bytes = os.path.getsize(SNAP)
print(f"copied to NAS: {SNAP} ({snap_bytes} bytes)")
if snap_bytes != local_bytes:
    print(f"ERROR: NAS copy size {snap_bytes} != local {local_bytes}; aborting"); sys.exit(3)

# 3) re-validate the NAS copy (reopen, integrity_check + row count)
snap = sqlite3.connect(f"file:{SNAP}?mode=ro", uri=True)
snap_integ = snap.execute("PRAGMA integrity_check").fetchone()[0]
snap_assets = snap.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
snap.close()
print(f"NAS snapshot integrity_check: {snap_integ}")
print(f"NAS snapshot assets row count: {snap_assets}")
print(f"NAS snapshot size: {snap_bytes} bytes ({snap_bytes/1e6:.1f} MB)")
if snap_integ != "ok" or snap_assets != src_assets:
    print("ERROR: NAS snapshot validation failed; aborting before any DB write"); sys.exit(3)
os.remove(LOCAL_SNAP)
print("snapshot VALID on NAS -> proceeding to repoint")

# ---------- STEP 4b: GUARDED REPOINT ----------
con = sqlite3.connect(DB)
con.isolation_level = None  # explicit txn control
cur = con.cursor()
cur.execute("BEGIN IMMEDIATE")
updated = 0
try:
    for r in res:
        old = r["originals_path"]
        new = dest_for(r)
        cur.execute(
            "UPDATE assets SET filepath=? WHERE id=? AND filepath=?",
            (new, r["chosen_id"], old))
        if cur.rowcount != 1:
            raise RuntimeError(
                f"guard failed: id={r['chosen_id']} expected 1 row updated, got {cur.rowcount} "
                f"(old={old})")
        updated += cur.rowcount
    if updated != matched:
        raise RuntimeError(f"updated {updated} != matched {matched}; rolling back")
    cur.execute("COMMIT")
    print(f"COMMIT ok: rows updated = {updated}")
except Exception as e:
    cur.execute("ROLLBACK")
    print(f"ROLLBACK ({e})")
    con.close()
    sys.exit(4)

# post-commit verification
n_orig_left = con.execute(
    "SELECT COUNT(*) FROM assets WHERE id IN ({}) AND filepath LIKE '%/originals/%'".format(
        ",".join(str(r["chosen_id"]) for r in res))).fetchone()[0]
n_new = con.execute(
    "SELECT COUNT(*) FROM assets WHERE id IN ({}) AND filepath LIKE ?".format(
        ",".join(str(r["chosen_id"]) for r in res)),
    (DESTROOT + "/%",)).fetchone()[0]
con.close()
print(f"post-commit: of the {matched} ids, {n_new} now point under long-video-elsewhere, {n_orig_left} still under originals")
if n_orig_left != 0 or n_new != matched:
    print("ERROR: post-commit verification unexpected"); sys.exit(5)
print("DONE")
