#!/usr/bin/env python3
"""
seed_apple.py — PHASE 1, task 5. Seed faces.db `persons` from Apple's named people
and create source='apple', confirmed=0 ASSIGNMENTS as SUGGESTIONS only (they drive
nothing; phase 3 is where the user confirms). Idempotent / re-runnable: re-run after
the full detection pass to map assets that hadn't been detected yet.

Reads apple-enrichment.db + metadata.db mode=ro and loupe-settings.json (read-only,
for the is_protected DATA column — does NOT touch the protected-cut guard). Writes
ONLY faces.db.

Mapping rule (best-effort largest face): Apple ties names to an ASSET, not a face.
For an image asset that already has detected faces AND exactly one distinct Apple
name, attach that name to the asset's largest face. Multi-person assets (ambiguous)
and assets without detected faces (not yet processed / 0 faces / video) are left for
phase 2 and counted here.
"""
import json, os, sqlite3, time

from loupe_common import APP_DATA, METADATA_DB, VIDEO_EXT, ro
FACES_DB = os.path.join(APP_DATA, "faces.db")
ENRICH_DB = os.path.join(APP_DATA, "apple-enrichment.db")
SETTINGS = os.path.join(APP_DATA, "loupe-settings.json")


def main():
    now = int(time.time())
    try:
        protected = set(json.load(open(SETTINGS)).get("protected_people", []))
    except Exception:
        protected = set()

    db = sqlite3.connect(FACES_DB)

    # 1) persons — one row per distinct Apple-named person
    enr = ro(ENRICH_DB)
    names = [r[0] for r in enr.execute(
        "SELECT DISTINCT person FROM persons WHERE person IS NOT NULL AND person<>'' "
        "ORDER BY person")]
    for nm in names:
        db.execute(
            "INSERT OR IGNORE INTO persons(name,is_protected,source,created_at) "
            "VALUES(?,?, 'apple', ?)", (nm, 1 if nm in protected else 0, now))
    db.commit()
    pid = {r[0]: r[1] for r in db.execute("SELECT name,person_id FROM persons")}

    # Apple per-asset name sets
    asset_names = {}
    for aid, person in enr.execute("SELECT asset_id, person FROM persons"):
        asset_names.setdefault(aid, set()).add(person)
    enr.close()

    # which Apple-tagged assets are images?
    meta = ro(METADATA_DB)
    qids = list(asset_names)
    ext = {}
    for i in range(0, len(qids), 900):
        chunk = qids[i:i + 900]
        qm = ",".join("?" * len(chunk))
        for r in meta.execute(
                f"SELECT id, extension FROM assets WHERE id IN ({qm})", chunk):
            ext[r["id"]] = (r["extension"] or "").upper()
    meta.close()

    # faces present so far: asset_id -> (largest_face_id)
    largest = {}
    for face_id, asset_id, bbox in db.execute(
            "SELECT face_id, asset_id, bbox_json FROM faces"):
        try:
            b = json.loads(bbox); area = (b["x2"] - b["x1"]) * (b["y2"] - b["y1"])
        except Exception:
            area = 0
        cur = largest.get(asset_id)
        if cur is None or area > cur[1]:
            largest[asset_id] = (face_id, area)

    created = skipped_multi = pending_noface = video = 0
    for aid, nmset in asset_names.items():
        is_video = ext.get(aid) in VIDEO_EXT
        if is_video:
            video += 1
            continue
        if aid not in largest:
            pending_noface += 1            # not detected yet / 0 faces — revisit (phase 2 / re-run)
            continue
        if len(nmset) != 1:
            skipped_multi += 1             # ambiguous which face — phase 2 resolves
            continue
        nm = next(iter(nmset))
        face_id = largest[aid][0]
        cur = db.execute(
            "INSERT OR IGNORE INTO assignments"
            "(face_id,person_id,source,confidence,confirmed,updated_at) "
            "VALUES(?,?, 'apple', NULL, 0, ?)", (face_id, pid[nm], now))
        if cur.rowcount:
            created += 1
    db.commit()

    npersons = db.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    nassign = db.execute("SELECT COUNT(*) FROM assignments WHERE source='apple'").fetchone()[0]
    nconf = db.execute("SELECT COUNT(*) FROM assignments WHERE source='apple' AND confirmed=1").fetchone()[0]
    db.close()
    print(f"persons seeded (total): {npersons}")
    print(f"apple assignments created this run: {created} | total apple assignments: {nassign} "
          f"| confirmed=1 among them: {nconf} (must be 0)")
    print(f"pending — multi-person (ambiguous): {skipped_multi} | "
          f"image assets not yet detected/0-face: {pending_noface} | video-only (no faces): {video}")


if __name__ == "__main__":
    main()
