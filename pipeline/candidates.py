#!/usr/bin/env python3
"""
candidates.py — shared loader for the review tool (v2) and the thumbnail generator.

Read-only on metadata.db (opened mode=ro). Reads the candidate CSVs in culling/.
EXCLUDES the protected sets (glasses-footage, workproduct-folders) defensively.

v2 returns a unified by-id view (one entry per unique candidate, merging the rules
it matched + their metrics + capture time) for the time-based IA, alongside the
per-set lists used for thumbnail generation / per-rule counts.
"""

import csv
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
# Portable roots — env-unset reproduces the historical layout (see ONBOARDING.md).
DATA_ROOT = os.environ.get("DATA_ROOT", HERE)
LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", os.path.join(os.sep, "mnt", "nas", "photos"))
CULL = os.path.join(DATA_ROOT, "culling")
DB = os.path.join(DATA_ROOT, "metadata.db")
THUMBS = os.path.join(CULL, "contactsheets", "thumbs")

VIDEO_EXT = {"MP4", "MOV", "M4V"}

# set key -> (csv filename, label, description)
SETS = [
    ("B4",  "B4_blurry_reviewed.csv",   "B4 · blurry",
     "low global Laplacian (blur < p10 = 93.6), low-texture guarded"),
    ("B2",  "B2_screenshots.csv",       "B2 · screenshots",
     "PNG at iPhone screen resolutions"),
    ("B5",  "B5_junk_imports.csv",      "B5 · junk imports",
     "no EXIF / GPS / camera make+model"),
    ("A2b", "A2b_orphan_mov_1to3s.csv", "A2b · 1-3s clips",
     "standalone MOV 1-3s (often intentional — review)"),
    ("B3",  "B3_burst_extras.csv",      "B3 · burst extras",
     "non-sharpest frames in a >=3 burst cluster"),
    ("A2a", "A2a_orphan_mov_sub1s.csv", "A2a · <1s taps",
     "standalone MOV < 1s (accidental)"),
]
RULE_PRIORITY = ["B4", "B3", "B2", "A2b", "A2a", "B5"]

_DETAIL_COLS = ("id, filepath, filename, file_size_bytes, file_sha256, extension, "
                "mime_type, year, month, capture_timestamp, gps_lat, gps_lon, "
                "camera_make, camera_model, lens_model, iso, shutter_speed, aperture, "
                "width_pixels, height_pixels, blur_laplacian, duration_seconds, "
                "is_live_photo_still, is_live_photo_video, live_photo_partner_id, "
                "is_shared_album, phash")


def _read_csv(name):
    # Cold start: a rule CSV may not exist yet (the pipeline writes it after culling).
    # Treat an absent CSV as zero candidates rather than raising at boot.
    p = os.path.join(CULL, name)
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return list(csv.DictReader(f))


def _protected_ids():
    ids = set()
    for name in ("glasses-footage.csv", "workproduct-folders.csv"):
        p = os.path.join(CULL, name)
        if os.path.exists(p):
            for r in _read_csv(name):
                ids.add(int(r["id"]))
    return ids


def _ro():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def load_all():
    """Return (sets_meta, items_by_set, by_id).
       items_by_set[key] = list of dicts (lean, for thumbnail gen / counts).
       by_id[id]         = unified dict: id, year, month, ts, size, ext, path,
                           fpath, is_video, dur, has_gps, rules[], m{rule:{...}}."""
    protected = _protected_ids()
    raw = {key: {int(r["id"]): r for r in _read_csv(csvf)} for key, csvf, _, _ in SETS}

    all_ids = set()
    for key in raw:
        all_ids |= set(raw[key])
    all_ids -= protected

    meta = {}
    idlist = list(all_ids)
    # Cold start: metadata.db is created by the develop/ingest step, not by us. If it's
    # absent, skip the lookup (meta stays empty -> 0 candidates) instead of raising on a
    # mode=ro open. No-op when the db exists (the normal path).
    if os.path.exists(DB):
        conn = _ro()
        for i in range(0, len(idlist), 900):
            chunk = idlist[i:i + 900]
            q = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT id, filepath, file_size_bytes, extension, year, month, "
                f"capture_timestamp, gps_lat, gps_lon, duration_seconds, "
                f"width_pixels, height_pixels FROM assets WHERE id IN ({q})", chunk):
                meta[row["id"]] = row
        conn.close()

    by_id = {}
    items_by_set = {}
    sets_meta = []
    for key, csvf, label, desc in SETS:
        items = []
        for idv, r in raw[key].items():
            if idv in protected or idv not in meta:
                continue
            m = meta[idv]
            ext = (m["extension"] or "").upper()
            # unified entry (create once)
            it = by_id.get(idv)
            if it is None:
                it = by_id[idv] = {
                    "id": idv,
                    "year": m["year"], "month": m["month"],
                    "ts": m["capture_timestamp"],
                    "size": m["file_size_bytes"] or 0,
                    "ext": ext,
                    "path": (m["filepath"] or "").replace(LIBRARY_ROOT.rstrip(os.sep) + os.sep, ""),
                    "fpath": m["filepath"],
                    "is_video": ext in VIDEO_EXT,
                    "dur": m["duration_seconds"],
                    "has_gps": m["gps_lat"] is not None,
                    "rules": [], "m": {},
                }
            it["rules"].append(key)
            metric = {}
            if key == "B4":
                metric = {"g": _f(r.get("blur_global")), "c": _f(r.get("blur_centercrop")),
                          "fp": 1 if r.get("fp_suspect") == "1" else 0,
                          "burst": 1 if r.get("in_burst") == "1" else 0}
            elif key == "B2":
                metric = {"w": m["width_pixels"], "h": m["height_pixels"]}
            elif key == "B3":
                metric = {"blur": _f(r.get("blur_laplacian")), "cluster": _i(r.get("cluster_id")),
                          "rank": _i(r.get("sharp_rank")), "csize": _i(r.get("cluster_size"))}
            it["m"][key] = metric
            items.append({"id": idv})
        items_by_set[key] = items
        sets_meta.append({"key": key, "label": label, "desc": desc, "total": len(items)})

    # B2 width/height need a dedicated read (not in lean meta) — fold from CSV
    for r in raw.get("B2", {}).values():
        idv = int(r["id"])
        if idv in by_id:
            by_id[idv]["m"].setdefault("B2", {})
            by_id[idv]["m"]["B2"]["w"] = _i(r.get("width_pixels"))
            by_id[idv]["m"]["B2"]["h"] = _i(r.get("height_pixels"))

    return sets_meta, items_by_set, by_id


def fetch_detail(idv):
    """Full read-only EXIF row for one id (focus-mode metadata). dict or None."""
    conn = _ro()
    row = conn.execute(f"SELECT {_DETAIL_COLS} FROM assets WHERE id=?", (idv,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _f(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
