#!/usr/bin/env python3
"""
culling.py — generate per-rule delete-candidate CSVs for the photo library.

Nothing here mutates metadata.db or touches any photo. It only SELECTs and
writes CSVs into ~/loupe-pipeline/culling/, then prints a summary.

Run:  python3 culling.py

Design notes
------------
* PROTECTED SET — Ray-Ban Meta glasses footage is NEVER a delete candidate.
  Defined as the UNION of camera_model='Ray-Ban Meta Smart Glasses' (3,744 rows)
  and the Meta export filename signature '%_singular_display%' (which also
  catches 1,470 glasses rows whose EXIF was stripped to NULL/HSTN/2Q37S model).
  Total protected = 5,214 rows / ~245.7 GB.
* NULL-SAFE EXCLUSION — every rule excludes via  id NOT IN (protected_ids).
  Excluding with  NOT (camera_model='...')  would silently drop the 27,030
  NULL-camera_model rows (NOT NULL == NULL == false). We never do that.
"""

import csv
import os
import sqlite3
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
# W16 / P7: the original-media root is configurable, and the work-product exclusion below
# is derived from it rather than spelled out. server.py builds the same "production"
# prefix from the same environment variable (_prod_prefix); two hand-written copies of
# one concept silently disagree the moment the root moves, which is exactly what
# portability work is meant to prevent. Default matches server.py's default, so behaviour
# is unchanged when the variable is unset.
LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", os.path.join(os.sep, "mnt", "nas2", "photos"))
_LR = LIBRARY_ROOT.rstrip(os.sep) + os.sep
DB = os.path.join(HERE, "metadata.db")
OUT = os.path.join(HERE, "culling")
os.makedirs(OUT, exist_ok=True)

# Edit-linked assets (original<->edit variant pairs) live in loupe's app-owned
# edits.db (the loupe app dir, a sibling of this pipeline data dir). They are
# protected from nomination as a UNIT, mirroring the app's export choke. Read-only
# + guarded on existence: no edits.db => no extra protection, never an error.
# ZERO CIFS — edits.db and metadata.db are both LOCAL; no originals are touched.
EDITS_DB = os.environ.get("EDITS_DB")
if not EDITS_DB:
    for _c in (os.path.join(os.path.dirname(HERE), "loupe", "edits.db"),  # sibling app dir
               os.path.join(HERE, "edits.db")):
        if os.path.exists(_c):
            EDITS_DB = _c
            break


def edit_linked_ids():
    """Asset ids in any original<->edit variant group (both roles). Empty when edits.db is
    absent/unreadable — protection simply doesn't fire, never a crash."""
    if not EDITS_DB or not os.path.exists(EDITS_DB):
        return []
    try:
        edb = sqlite3.connect(f"file:{EDITS_DB}?mode=ro", uri=True)
        try:
            return [r[0] for r in edb.execute("SELECT asset_id FROM variant_members")]
        finally:
            edb.close()
    except Exception as e:
        print(f"  edits.db unreadable ({e}) — edit-linked protection skipped")
        return []

# Protected category 1 — Ray-Ban Meta glasses footage (single source of truth).
PROTECTED_SQL = r"""
    camera_model = 'Ray-Ban Meta Smart Glasses'
    OR filename LIKE '%\_singular\_display%' ESCAPE '\'
"""

# Protected category 2 — out-of-scope work-product, protected BY PATH on purpose.
# Path is the discriminator deliberately: these rendered/exported files have
# NULL EXIF/GPS/camera/duration, which previously misclassified them as junk.
WORKPRODUCT_SQL = (
    "\n    filepath LIKE '" + _LR + "production/%'"
    "\n    OR filepath LIKE '" + _LR + "long-video-elsewhere/%'\n"
)

# iPhone screen resolutions (portrait WxH); landscape (HxW) handled in code.
IPHONE_RES = {
    (640, 1136),   # 5/5s/5c/SE1
    (750, 1334),   # 6/7/8/SE2/SE3
    (828, 1792),   # XR/11
    (1080, 1920),  # Plus (rendered)
    (1242, 2208),  # Plus (native)
    (1125, 2436),  # X/XS/11Pro/12mini/13mini
    (1170, 2532),  # 12/12Pro/13/13Pro/14
    (1179, 2556),  # 14Pro/15/15Pro/16
    (1206, 2622),  # 16 Pro
    (1242, 2688),  # XSMax/11ProMax
    (1284, 2778),  # 12ProMax/13ProMax/14Plus
    (1290, 2796),  # 14ProMax/15Plus/15ProMax/16Plus
    (1320, 2868),  # 16 Pro Max
}
IPHONE_RES_BOTH = IPHONE_RES | {(h, w) for (w, h) in IPHONE_RES}

BURST_GAP_S = 5      # consecutive frames within this many seconds chain into one burst
BURST_MIN = 3        # cluster needs >= this many frames
BURST_KEEP = 3       # keep the N sharpest per cluster; the rest are candidates
BLUR_PCTILE = 10     # data-driven blurry threshold = this percentile of blur scores


def write_csv(name, rows, header):
    path = os.path.join(OUT, name)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)
    return path


def gb(nbytes):
    return (nbytes or 0) / 1e9


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row

    # --- protected sets (two SEPARATE categories) ---------------------------
    # g  = glasses footage (camera_model / naming signature)
    # wp = work-product folders (path-based)
    # prot = the union, used for the null-safe  id NOT IN (...)  exclusion that
    #        every rule applies. The two categories are reported separately.
    db.execute("DROP TABLE IF EXISTS temp.g")
    db.execute("DROP TABLE IF EXISTS temp.wp")
    db.execute("DROP TABLE IF EXISTS temp.ed")
    db.execute("DROP TABLE IF EXISTS temp.prot")
    db.execute(f"CREATE TEMP TABLE g  AS SELECT id FROM assets WHERE {PROTECTED_SQL}")
    db.execute(f"CREATE TEMP TABLE wp AS SELECT id FROM assets WHERE {WORKPRODUCT_SQL}")
    # ed = edit-linked ids (original<->edit variant pairs) from loupe's edits.db.
    # Empty when the sidecar is absent; folded into the null-safe prot exclusion below.
    _edit_ids = edit_linked_ids()
    db.execute("CREATE TEMP TABLE ed (id INTEGER PRIMARY KEY)")
    db.executemany("INSERT OR IGNORE INTO ed VALUES (?)", [(i,) for i in _edit_ids])
    db.execute("CREATE TEMP TABLE prot AS "
               "SELECT id FROM g UNION SELECT id FROM wp UNION SELECT id FROM ed")
    EXC = "id NOT IN (SELECT id FROM prot)"
    if _edit_ids:
        print(f"  edit-linked variant pairs protected       : {len(_edit_ids):,} ids")

    g_rows, g_bytes = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(file_size_bytes),0) FROM assets "
        "WHERE id IN (SELECT id FROM g)").fetchone()
    wp_rows, wp_bytes = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(file_size_bytes),0) FROM assets "
        "WHERE id IN (SELECT id FROM wp)").fetchone()
    overlap = db.execute(
        "SELECT COUNT(*) FROM g WHERE id IN (SELECT id FROM wp)").fetchone()[0]
    prot_rows = db.execute("SELECT COUNT(*) FROM prot").fetchone()[0]

    # (re)write glasses CSV (category 1)
    grows = db.execute("""
        SELECT id, filepath, filename, extension, year, month, file_size_bytes,
               camera_make, camera_model, capture_timestamp, gps_lat, gps_lon,
               duration_seconds, is_live_photo_video, is_shared_album,
               CASE WHEN camera_model='Ray-Ban Meta Smart Glasses'
                    THEN 'camera_model' ELSE 'naming_signature' END AS detection_method
        FROM assets WHERE id IN (SELECT id FROM g)
        ORDER BY detection_method, year, filename
    """).fetchall()
    write_csv("glasses-footage.csv", [tuple(r) for r in grows],
              grows[0].keys() if grows else [])

    # write work-product CSV (category 2) — distinct file
    wprows = db.execute("""
        SELECT id, filepath, filename, extension, duration_seconds, file_size_bytes
        FROM assets WHERE id IN (SELECT id FROM wp)
        ORDER BY filepath
    """).fetchall()
    write_csv("workproduct-folders.csv", [tuple(r) for r in wprows],
              ["id", "filepath", "filename", "extension",
               "duration_seconds", "file_size_bytes"])

    summary = []          # (rule, count, gb)
    candidate_ids = set()
    candidate_bytes = {}  # id -> bytes (for unique GB)

    def record(rows):
        for r in rows:
            candidate_ids.add(r["id"])
            candidate_bytes[r["id"]] = r["file_size_bytes"] or 0

    def total_gb(rows):
        return gb(sum(r["file_size_bytes"] or 0 for r in rows))

    # --- A1. exact SHA-256 duplicates ---------------------------------------
    a1 = db.execute(f"""
        WITH dups AS (
            SELECT file_sha256 FROM assets
            WHERE {EXC} AND file_sha256 IS NOT NULL
            GROUP BY file_sha256 HAVING COUNT(*) > 1
        ),
        ranked AS (
            SELECT a.id, a.filepath, a.filename, a.file_sha256, a.file_size_bytes,
                   a.capture_timestamp, a.year, a.month,
                   ROW_NUMBER() OVER (
                       PARTITION BY a.file_sha256
                       ORDER BY (a.capture_timestamp IS NOT NULL) DESC,
                                (a.filepath GLOB '*/[0-9][0-9][0-9][0-9]/[0-9][0-9]/*') DESC,
                                a.id ASC
                   ) AS keep_rank
            FROM assets a JOIN dups d ON d.file_sha256 = a.file_sha256
            WHERE a.{EXC}
        )
        SELECT id, filepath, filename, file_sha256, file_size_bytes,
               capture_timestamp, year, month, keep_rank
        FROM ranked WHERE keep_rank > 1
        ORDER BY file_sha256, keep_rank
    """).fetchall()
    write_csv("A1_sha256_dupes.csv", [tuple(r) for r in a1],
              ["id", "filepath", "filename", "file_sha256", "file_size_bytes",
               "capture_timestamp", "year", "month", "keep_rank"])
    record(a1)
    summary.append(("A1 sha256 dupes (extras)", len(a1), total_gb(a1)))

    # --- A2 orphan short MOVs — split by duration -----------------------------
    # A2a TIER A: sub-second accidental taps (auto-delete candidate).
    # A2b TIER B: 1-3s standalone clips (often intentional -> human review).
    a2_hdr = ["id", "filepath", "filename", "file_size_bytes", "duration_seconds",
              "capture_timestamp", "year", "month"]

    a2a = db.execute(f"""
        SELECT id, filepath, filename, file_size_bytes, duration_seconds,
               capture_timestamp, year, month
        FROM assets
        WHERE {EXC} AND extension='MOV' AND is_live_photo_video=0
          AND duration_seconds < 1
        ORDER BY duration_seconds, file_size_bytes DESC
    """).fetchall()
    write_csv("A2a_orphan_mov_sub1s.csv", [tuple(r) for r in a2a], a2_hdr)
    record(a2a)
    summary.append(("A2a orphan MOV <1s  [TIER A]", len(a2a), total_gb(a2a)))

    a2b = db.execute(f"""
        SELECT id, filepath, filename, file_size_bytes, duration_seconds,
               capture_timestamp, year, month
        FROM assets
        WHERE {EXC} AND extension='MOV' AND is_live_photo_video=0
          AND duration_seconds >= 1 AND duration_seconds < 3
        ORDER BY duration_seconds, file_size_bytes DESC
    """).fetchall()
    write_csv("A2b_orphan_mov_1to3s.csv", [tuple(r) for r in a2b], a2_hdr)
    record(a2b)
    summary.append(("A2b orphan MOV 1-3s  [TIER B]", len(a2b), total_gb(a2b)))

    # --- B1. genuinely-shared (expected EMPTY after glasses exclusion) -------
    b1 = db.execute(f"""
        SELECT id, filepath, filename, is_shared_album, camera_model, year, month,
               file_size_bytes
        FROM assets
        WHERE {EXC}
          AND (is_shared_album=1
               OR filename LIKE 'od\\_%' ESCAPE '\\'
               OR filename LIKE '%\\_singular\\_display%' ESCAPE '\\')
        ORDER BY year, month
    """).fetchall()
    write_csv("B1_shared_album.csv", [tuple(r) for r in b1],
              ["id", "filepath", "filename", "is_shared_album", "camera_model",
               "year", "month", "file_size_bytes"])
    record(b1)
    summary.append(("B1 genuinely-shared", len(b1), total_gb(b1)))

    # --- B2. screenshots (iPhone resolutions only) --------------------------
    b2_all = db.execute(f"""
        SELECT id, filepath, filename, width_pixels, height_pixels,
               year, month, file_size_bytes
        FROM assets WHERE {EXC} AND extension='PNG'
    """).fetchall()
    b2 = [r for r in b2_all if (r["width_pixels"], r["height_pixels"]) in IPHONE_RES_BOTH]
    write_csv("B2_screenshots.csv",
              [(r["id"], r["filepath"], r["filename"], r["width_pixels"], r["height_pixels"],
                r["year"], r["month"], r["file_size_bytes"]) for r in b2],
              ["id", "filepath", "filename", "width_pixels", "height_pixels",
               "year", "month", "file_size_bytes"])
    record(b2)
    b2_by_year = defaultdict(int)
    for r in b2:
        b2_by_year[r["year"]] += 1
    summary.append(("B2 screenshots", len(b2), total_gb(b2)))

    # --- B3. burst clusters (sequential, gap-based within rounded GPS) ------
    elig = db.execute(f"""
        SELECT id, filepath, filename, file_size_bytes, blur_laplacian,
               capture_timestamp, year, month,
               ROUND(gps_lat,4) AS glat, ROUND(gps_lon,4) AS glon
        FROM assets
        WHERE {EXC} AND capture_timestamp IS NOT NULL
          AND gps_lat IS NOT NULL AND gps_lon IS NOT NULL
        ORDER BY glat, glon, capture_timestamp, id
    """).fetchall()

    burst_member_ids = set()   # every id that belongs to a >=BURST_MIN cluster
    b3_extras = []             # (row, cluster_id, sharp_rank, cluster_size)
    cluster_id = 0
    i, n = 0, len(elig)
    while i < n:
        j = i + 1
        while (j < n
               and elig[j]["glat"] == elig[i]["glat"]
               and elig[j]["glon"] == elig[i]["glon"]
               and (elig[j]["capture_timestamp"] - elig[j - 1]["capture_timestamp"]) <= BURST_GAP_S):
            j += 1
        group = elig[i:j]
        if len(group) >= BURST_MIN:
            cluster_id += 1
            for r in group:
                burst_member_ids.add(r["id"])
            ordered = sorted(group, key=lambda r: (r["blur_laplacian"] is None,
                                                   -(r["blur_laplacian"] or 0)))
            for rank, r in enumerate(ordered, start=1):
                if rank > BURST_KEEP:
                    b3_extras.append((r, cluster_id, rank, len(group)))
        i = j

    write_csv("B3_burst_extras.csv",
              [(r["id"], r["filepath"], r["filename"], r["file_size_bytes"],
                r["blur_laplacian"], cid, rank, csize, r["glat"], r["glon"],
                r["capture_timestamp"], r["year"], r["month"])
               for (r, cid, rank, csize) in b3_extras],
              ["id", "filepath", "filename", "file_size_bytes", "blur_laplacian",
               "cluster_id", "sharp_rank", "cluster_size", "gps_lat_4dp", "gps_lon_4dp",
               "capture_timestamp", "year", "month"])
    b3_rows = [r for (r, _, _, _) in b3_extras]
    record(b3_rows)
    summary.append(("B3 burst extras", len(b3_extras), total_gb(b3_rows)))

    # --- B4. blurry images (data-driven threshold; cross-ref burst) ---------
    blur_rows = db.execute(f"""
        SELECT id, filepath, filename, file_size_bytes, blur_laplacian,
               capture_timestamp, gps_lat, gps_lon, year, month
        FROM assets WHERE {EXC} AND blur_laplacian IS NOT NULL
        ORDER BY blur_laplacian
    """).fetchall()
    vals = [r["blur_laplacian"] for r in blur_rows]

    def percentile(p):
        if not vals:
            return None
        k = min(len(vals) - 1, int(p / 100.0 * len(vals)))
        return vals[k]

    blur_dist = {p: percentile(p) for p in (1, 5, 10, 25, 50, 75, 90, 95, 99)}
    threshold = percentile(BLUR_PCTILE)
    b4 = [r for r in blur_rows if r["blur_laplacian"] < threshold]
    b4_in_burst = sum(1 for r in b4 if r["id"] in burst_member_ids)
    b4_isolated = len(b4) - b4_in_burst
    write_csv("B4_blurry.csv",
              [(r["id"], r["filepath"], r["filename"], r["file_size_bytes"],
                r["blur_laplacian"], (r["id"] in burst_member_ids),
                r["capture_timestamp"], r["gps_lat"], r["gps_lon"], r["year"], r["month"])
               for r in b4],
              ["id", "filepath", "filename", "file_size_bytes", "blur_laplacian",
               "in_burst", "capture_timestamp", "gps_lat", "gps_lon", "year", "month"])
    record(b4)
    summary.append((f"B4 blurry (<p{BLUR_PCTILE}={threshold:.1f})", len(b4), total_gb(b4)))

    # --- B5. junk imports ----------------------------------------------------
    b5 = db.execute(f"""
        SELECT id, filepath, filename, extension, file_size_bytes, year, month
        FROM assets
        WHERE {EXC} AND capture_timestamp IS NULL AND gps_lat IS NULL
          AND camera_make IS NULL AND camera_model IS NULL
        ORDER BY file_size_bytes DESC
    """).fetchall()
    write_csv("B5_junk_imports.csv", [tuple(r) for r in b5],
              ["id", "filepath", "filename", "extension", "file_size_bytes", "year", "month"])
    record(b5)
    summary.append(("B5 junk imports", len(b5), total_gb(b5)))

    # ---------------------------- SUMMARY -----------------------------------
    unique_bytes = sum(candidate_bytes.values())
    sum_counts = sum(c for _, c, _ in summary)
    line = "=" * 72
    print(line)
    print("CULLING CANDIDATE SUMMARY  (candidates only — nothing deleted)")
    print(line)
    print(f"{'RULE':<36}{'COUNT':>10}{'GB':>12}")
    print("-" * 58)
    for name, cnt, g in summary:
        print(f"{name:<36}{cnt:>10,}{g:>12.2f}")
    print("-" * 58)
    print(f"{'TOTAL UNIQUE CANDIDATES':<36}{len(candidate_ids):>10,}{gb(unique_bytes):>12.2f}")
    print(f"  (sum of per-rule counts = {sum_counts:,}; "
          f"overlap removed = {sum_counts - len(candidate_ids):,})")
    print()
    print("PROTECTED (NOT reclaimable):")
    print(f"  glasses-footage     (camera_model/naming) : {g_rows:,} rows / {gb(g_bytes):.2f} GB")
    print(f"  workproduct-folders (path-based)          : {wp_rows:,} rows / {gb(wp_bytes):.2f} GB")
    print(f"  combined unique protected                 : {prot_rows:,} rows"
          + (f"   (categories overlap by {overlap})" if overlap else "   (no overlap)"))
    print()
    print(f"Blur distribution (non-protected images with a score, n={len(vals):,}):")
    print("  " + "  ".join(f"p{p}={v:.1f}" for p, v in blur_dist.items()))
    print(f"  B4 threshold used: < p{BLUR_PCTILE} = {threshold:.1f}")
    print(f"  B4 blurry-in-burst: {b4_in_burst:,}   blurry-isolated: {b4_isolated:,}")
    print()
    print("B2 screenshots by year:")
    for y in sorted(b2_by_year):
        print(f"  {y}: {b2_by_year[y]:,}")
    print(f"B3 bursts: {cluster_id:,} clusters, {len(burst_member_ids):,} member frames, "
          f"{len(b3_extras):,} extras flagged")
    print(line)
    db.close()


if __name__ == "__main__":
    main()
