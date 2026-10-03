#!/usr/bin/env python3
"""
Step 4 — location/duration report + production tagging + A2 re-run.

Read-only on the DB (mode=ro). Outputs:
  - culling/production_videos.csv   (>5min at either home, in-era; these are
    work product and are EXCLUDED from B-rule culling by culling.py)
  - culling/A2_orphan_short_mov.csv (re-run now that durations exist)
  - report to stdout

Home eras are personal (GPS boxes + dates) — loaded from a gitignored local file,
never published. Set STEP4_HOMES_JSON or drop step4-homes.local.json next to this
script. Format: [[name, lat_lo, lat_hi, lon_lo, lon_hi, ts_start, ts_end_exclusive], ...]
No DB writes here. No new columns — tagging lives in CSVs only.
"""

import csv, json, os, sqlite3

PROJ = os.path.expanduser("~/loupe-pipeline")
DB = os.path.join(PROJ, "metadata.db")
CULL = os.path.join(PROJ, "culling")

# Home GPS/era boxes are personal — loaded from a gitignored local file, never committed.
_HOMES_FILE = os.environ.get("STEP4_HOMES_JSON", os.path.join(PROJ, "step4-homes.local.json"))
try:
    with open(_HOMES_FILE) as _f:
        HOMES = [tuple(h) for h in json.load(_f)]
except FileNotFoundError:
    HOMES = []
    print(f"note: no home config at {_HOMES_FILE} — production tagging finds 0 home videos.", flush=True)

BUCKETS = [("<1min", 0, 60), ("1-5min", 60, 300),
           ("5-20min", 300, 1200), (">20min", 1200, None)]
PRODUCTION_MIN_S = 300  # >5 min


def home_rows(con, h):
    name, la0, la1, lo0, lo1, t0, t1 = h
    return con.execute("""
        SELECT id, filepath, filename, year, file_size_bytes, duration_seconds,
               CAST(strftime('%Y', capture_timestamp, 'unixepoch') AS INT) cap_year
        FROM assets
        WHERE extension IN ('MOV','MP4','M4V')
          AND gps_lat BETWEEN ? AND ? AND gps_lon BETWEEN ? AND ?
          AND capture_timestamp >= CAST(strftime('%s', ?) AS INTEGER)
          AND capture_timestamp <  CAST(strftime('%s', ?) AS INTEGER)
        ORDER BY capture_timestamp""", (la0, la1, lo0, lo1, t0, t1)).fetchall()


def bucket_of(dur):
    if dur is None:
        return "no-dur"
    for label, lo, hi in BUCKETS:
        if dur >= lo and (hi is None or dur < hi):
            return label
    return "no-dur"


def fmt_table(rows):
    # rows: {(year, bucket): [n, bytes]}
    years = sorted({y for y, _ in rows})
    labels = [b[0] for b in BUCKETS] + ["no-dur"]
    out = [f"  {'year':<6}" + "".join(f"{l:>16}" for l in labels) + f"{'TOTAL':>16}"]
    for y in years:
        cells, tn, tb = [], 0, 0
        for l in labels:
            n, b = rows.get((y, l), (0, 0))
            tn += n; tb += b
            cells.append(f"{n:>6,}/{b/1e9:>7.1f}G" if n else f"{'—':>15}")
        out.append(f"  {y:<6}" + " ".join(cells) + f"  {tn:>5,}/{tb/1e9:>7.1f}G")
    return "\n".join(out)


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    no_dur = con.execute("SELECT COUNT(*) FROM assets WHERE extension IN "
                         "('MOV','MP4','M4V') AND duration_seconds IS NULL").fetchone()[0]
    print(f"videos still missing duration: {no_dur:,}\n")

    production = []  # (home, id, filepath, filename, cap_year, bytes, dur)
    for h in HOMES:
        name = h[0]
        rows = home_rows(con, h)
        personal = {}   # (year,bucket) -> [n, bytes]
        prod = {}
        for (rid, fp, fn, _y, sz, dur, cy) in rows:
            b = bucket_of(dur)
            is_prod = dur is not None and dur > PRODUCTION_MIN_S
            tgt = prod if is_prod else personal
            n, tb = tgt.get((cy, b), (0, 0))
            tgt[(cy, b)] = (n + 1, tb + (sz or 0))
            if is_prod:
                production.append((name, rid, fp, fn, cy, sz, dur))
        print(f"=== {name}  ({h[5]} .. {h[6]} exclusive)  — {len(rows)} videos ===")
        print("PERSONAL (<=5min or no duration):")
        print(fmt_table(personal) if personal else "  none")
        print("PRODUCTION (>5min):")
        print(fmt_table(prod) if prod else "  none")
        print()

    # production CSV (consumed by culling.py as an exclusion list)
    os.makedirs(CULL, exist_ok=True)
    ppath = os.path.join(CULL, "production_videos.csv")
    with open(ppath, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "file_size_bytes", "filepath", "filename",
                    "home", "cap_year", "duration_seconds"])
        for (home, rid, fp, fn, cy, sz, dur) in sorted(production, key=lambda r: r[1]):
            w.writerow([rid, sz, fp, fn, home, cy, round(dur, 1)])
    tot_b = sum(r[5] or 0 for r in production)
    print(f"PRODUCTION TOTAL: {len(production):,} videos / {tot_b/1e9:.1f} GB "
          f"-> {ppath}")

    # Production-SUSPECT: >5min, NO GPS, captured in the HOME-1 era (where
    # the 2020 GPS hole lives). Could be home-recorded work product — or
    # someone else's video saved from messages. Reported + CSV only; NOT
    # added to the culling exclusion list (needs human eyes first).
    suspects = con.execute("""
        SELECT id, file_size_bytes, filepath, filename, duration_seconds,
               CAST(strftime('%Y', capture_timestamp, 'unixepoch') AS INT) cap_year
        FROM assets
        WHERE extension IN ('MOV','MP4','M4V')
          AND gps_lat IS NULL
          AND duration_seconds > ?
          AND capture_timestamp >= CAST(strftime('%s','2020-06-01') AS INTEGER)
          AND capture_timestamp <  CAST(strftime('%s','2023-01-01') AS INTEGER)
        ORDER BY capture_timestamp""", (PRODUCTION_MIN_S,)).fetchall()
    sus_tbl = {}
    for (rid, sz, fp, fn, dur, cy) in suspects:
        b = bucket_of(dur)
        n, tb = sus_tbl.get((cy, b), (0, 0))
        sus_tbl[(cy, b)] = (n + 1, tb + (sz or 0))
    print("\n=== PRODUCTION-SUSPECT (>5min, NULL gps, captured in HOME-1 era) ===")
    print("NOT excluded from culling — needs human review (could be saved/")
    print("shared videos from others, not just untagged home recordings).")
    print(fmt_table(sus_tbl) if sus_tbl else "  none")
    spath = os.path.join(CULL, "production_suspect.csv")
    with open(spath, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "file_size_bytes", "filepath", "filename",
                    "cap_year", "duration_seconds"])
        for (rid, sz, fp, fn, dur, cy) in suspects:
            w.writerow([rid, sz, fp, fn, cy, round(dur, 1)])
    print(f"suspect total: {len(suspects):,} videos / "
          f"{sum(r[1] or 0 for r in suspects)/1e9:.1f} GB -> {spath}")

    # A2 re-run: orphan short MOVs, now unlocked. Production can't overlap
    # (<3s vs >5min) but exclude defensively anyway.
    prod_ids = {r[1] for r in production}
    a2 = con.execute("""
        SELECT id, file_size_bytes, filepath, filename, duration_seconds, year
        FROM assets
        WHERE extension='MOV' AND is_live_photo_video=0
          AND duration_seconds IS NOT NULL AND duration_seconds < 3.0
        ORDER BY file_size_bytes DESC""").fetchall()
    a2 = [r for r in a2 if r[0] not in prod_ids]
    with open(os.path.join(CULL, "A2_orphan_short_mov.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "file_size_bytes", "filepath", "filename",
                    "duration_seconds", "year"])
        w.writerows(a2)
    print(f"\nA2 orphan short MOVs (<3s, non-live): {len(a2):,} candidates / "
          f"{sum(r[1] or 0 for r in a2)/1e9:.2f} GB -> culling/A2_orphan_short_mov.csv")

    con.close()


if __name__ == "__main__":
    main()
