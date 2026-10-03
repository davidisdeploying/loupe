#!/usr/bin/env python3
"""
Phase 3 sample review — read-only candidate-quality gut-check.

For each non-empty rule it:
  1. Draws 40 reproducible random rows (fixed seed) from the rule's CSV.
  2. Prints a compact, rule-specific metadata table to stdout.
  3. Writes an HTML contact sheet of 256px thumbnails to
     ~/loupe-pipeline/culling/samples/<rule>.html (thumbs under samples/thumbs/).

Reads originals ONLY to make thumbnails. Writes ONLY under culling/samples/.
DB is opened read-only. No moves, no deletes, no schema changes.
"""

import csv, os, sqlite3, random, subprocess, html
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from PIL import Image, ImageOps
import pillow_heif
pillow_heif.register_heif_opener()

HOME   = os.path.expanduser("~")
PROJ   = os.path.join(HOME, "loupe-pipeline")
DB     = os.path.join(PROJ, "metadata.db")
CULL   = os.path.join(PROJ, "culling")
SAMP   = os.path.join(CULL, "samples")
THUMBS = os.path.join(SAMP, "thumbs")
SEED, N, TH = 1337, 40, 256
VIDEO_EXT = {"MOV", "MP4", "M4V"}

RULES = ["B1_shared_album", "B2_screenshots", "B3_burst_extras",
         "B4_blurry", "B5_junk_imports"]


# ---------------------------------------------------------------- helpers
def load_csv(rule):
    with open(os.path.join(CULL, f"{rule}.csv")) as fh:
        return list(csv.DictReader(fh))


def sample(rows, n=N):
    rng = random.Random(SEED)
    return rng.sample(rows, min(n, len(rows)))


def meta_for(con, ids):
    q = ("SELECT id, extension, mime_type, width_pixels, height_pixels, "
         "camera_make, camera_model, gps_lat, gps_lon, capture_timestamp, "
         "blur_laplacian, file_size_bytes, year, filename, filepath, "
         "is_live_photo_video FROM assets WHERE id IN (%s)"
         % ",".join("?" * len(ids)))
    cur = con.execute(q, list(ids))
    cols = [d[0] for d in cur.description]
    return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}


def mb(b):
    return (b or 0) / 1e6


def ts_str(ts):
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return str(ts)


def make_thumb(m, out_path):
    """Return (ok, note). Decodes images (incl HEIC) or pulls a video frame."""
    fp, ext = m["filepath"], (m["extension"] or "").upper()
    try:
        if ext in VIDEO_EXT:
            cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", fp,
                   "-frames:v", "1", "-vf", f"scale={TH}:-1", out_path]
            r = subprocess.run(cmd, capture_output=True, timeout=90)
            if r.returncode != 0 or not os.path.exists(out_path):
                # short clip: retry from the very start
                cmd[cmd.index("-ss") + 1] = "0"
                r = subprocess.run(cmd, capture_output=True, timeout=90)
            return (os.path.exists(out_path), "video-frame")
        else:
            with Image.open(fp) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")  # respect orientation
                im.thumbnail((TH, TH))
                im.save(out_path, "JPEG", quality=80)
            return (True, "")
    except Exception as e:
        return (False, type(e).__name__)


def build_html(rule, cells):
    """cells: list of (thumb_rel_or_None, caption_html)."""
    parts = [
        "<!doctype html><meta charset=utf-8>",
        f"<title>{rule}</title>",
        "<style>body{background:#111;color:#ddd;font:12px system-ui;margin:16px}"
        ".g{display:flex;flex-wrap:wrap;gap:10px}"
        ".c{width:256px}.c img{width:256px;height:256px;object-fit:contain;"
        "background:#000;border:1px solid #333}"
        ".x{width:256px;height:256px;display:flex;align-items:center;"
        "justify-content:center;background:#300;border:1px solid #533;color:#f99}"
        ".cap{margin-top:4px;line-height:1.35;word-break:break-word}</style>",
        f"<h2>{rule} — {len(cells)} random samples (seed {SEED})</h2><div class=g>",
    ]
    for thumb, cap in cells:
        img = (f"<img src='{thumb}'>" if thumb
               else "<div class=x>decode failed</div>")
        parts.append(f"<div class=c>{img}<div class=cap>{cap}</div></div>")
    parts.append("</div>")
    with open(os.path.join(SAMP, f"{rule}.html"), "w") as fh:
        fh.write("".join(parts))


# ---------------------------------------------------------------- per-rule
def caption(rule, m, row):
    fn = html.escape(m["filename"] or "")
    if rule == "B1_shared_album":
        kind = "VIDEO" if (m["extension"] or "").upper() in VIDEO_EXT else "img"
        cam = m["camera_model"] or "no-cam"
        gps = "gps✓" if m["gps_lat"] is not None else "gps✗"
        return f"<b>{fn}</b><br>{kind} · {mb(m['file_size_bytes']):.1f}MB · {m['year']}<br>{html.escape(str(cam))} · {gps}"
    if rule == "B2_screenshots":
        return f"<b>{fn}</b><br>{m['width_pixels']}×{m['height_pixels']} · {m['year']} · {mb(m['file_size_bytes']):.2f}MB"
    if rule == "B3_burst_extras":
        return f"<b>{fn}</b><br>burst n={row['cluster_size']} rank {row['sharp_rank']} (FLAG)<br>blur {float(row['blur_laplacian']):.0f} · {m['year']}"
    if rule == "B4_blurry":
        return f"<b>{fn}</b><br>blur {float(row['blur_laplacian']):.0f} · {'in-burst' if row['in_burst']=='1' else 'isolated'}<br>{m['width_pixels']}×{m['height_pixels']} · {m['year']}"
    if rule == "B5_junk_imports":
        return f"<b>{fn}</b><br>{m['extension']} · {m['width_pixels']}×{m['height_pixels']}<br>{m['year']} · {mb(m['file_size_bytes']):.2f}MB · cap {ts_str(m['capture_timestamp'])}"
    return fn


def print_table(rule, sampled, meta):
    print(f"\n{'='*78}\n{rule} — 40 random samples (seed {SEED})\n{'='*78}")
    if rule == "B1_shared_album":
        print(f"{'filename':<34}{'kind':<7}{'MB':>7}{'yr':>6}  {'camera':<16}{'gps':>4}")
        for r in sampled:
            m = meta[int(r["id"])]
            kind = "VIDEO" if (m["extension"] or "").upper() in VIDEO_EXT else "img"
            cam = (m["camera_model"] or "—")[:15]
            gps = "✓" if m["gps_lat"] is not None else "✗"
            print(f"{(m['filename'] or '')[:33]:<34}{kind:<7}{mb(m['file_size_bytes']):>7.1f}{m['year']:>6}  {cam:<16}{gps:>4}")
    elif rule == "B2_screenshots":
        print(f"{'filename':<40}{'WxH':>12}{'yr':>6}{'MB':>8}")
        for r in sampled:
            m = meta[int(r["id"])]
            print(f"{(m['filename'] or '')[:39]:<40}{str(m['width_pixels'])+'x'+str(m['height_pixels']):>12}{m['year']:>6}{mb(m['file_size_bytes']):>8.2f}")
    elif rule == "B4_blurry":
        print(f"{'filename':<34}{'blur':>8}{'in_burst':>9}{'yr':>6}{'WxH':>12}")
        for r in sampled:
            m = meta[int(r["id"])]
            print(f"{(m['filename'] or '')[:33]:<34}{float(r['blur_laplacian']):>8.0f}{('yes' if r['in_burst']=='1' else 'no'):>9}{m['year']:>6}{str(m['width_pixels'])+'x'+str(m['height_pixels']):>12}")
    elif rule == "B5_junk_imports":
        print(f"{'filename':<34}{'ext':>5}{'WxH':>12}{'MB':>8}{'yr':>6}{'capture':>12}")
        for r in sampled:
            m = meta[int(r["id"])]
            print(f"{(m['filename'] or '')[:33]:<34}{m['extension']:>5}{str(m['width_pixels'])+'x'+str(m['height_pixels']):>12}{mb(m['file_size_bytes']):>8.2f}{m['year']:>6}{ts_str(m['capture_timestamp']):>12}")


# ---- B3 full-cluster reconstruction (all members of selected clusters) ----
BURST_FULL_SQL = """
WITH base AS (
    SELECT id, filename, year, blur_laplacian,
           ROUND(gps_lat,4) glat, ROUND(gps_lon,4) glon, capture_timestamp ts
    FROM assets WHERE extension IN ('JPG','HEIC')
      AND gps_lat IS NOT NULL AND capture_timestamp IS NOT NULL
),
flagged AS (SELECT *, CASE WHEN ts-LAG(ts) OVER (PARTITION BY glat,glon ORDER BY ts,id)<=5 THEN 0 ELSE 1 END nc FROM base),
clustered AS (SELECT *, SUM(nc) OVER (PARTITION BY glat,glon ORDER BY ts,id ROWS UNBOUNDED PRECEDING) cid FROM flagged),
sized AS (SELECT *, COUNT(*) OVER (PARTITION BY glat,glon,cid) csize,
                 ROW_NUMBER() OVER (PARTITION BY glat,glon,cid ORDER BY blur_laplacian DESC,id) rank
          FROM clustered)
SELECT id, filename, year, blur_laplacian, glat, glon, cid, csize, rank, ts
FROM sized WHERE csize>=3 ORDER BY glat,glon,cid,rank;
"""


def b3_clusters(con, sampled_ids):
    rows = con.execute(BURST_FULL_SQL).fetchall()
    clusters = {}
    id2key = {}
    for (i, fn, yr, blur, glat, glon, cid, csize, rank, ts) in rows:
        key = (glat, glon, cid)
        clusters.setdefault(key, []).append(
            dict(id=i, fn=fn, yr=yr, blur=blur, csize=csize, rank=rank, ts=ts))
        id2key[i] = key
    # pick clusters that contain sampled flagged ids, spanning a range of sizes
    keys = []
    for sid in sampled_ids:
        k = id2key.get(sid)
        if k and k not in keys:
            keys.append(k)
    keys.sort(key=lambda k: clusters[k][0]["csize"])
    # spread: smallest, largest, and a few in between
    pick = []
    if keys:
        idxs = sorted(set(int(x) for x in
                      [0, len(keys)*0.25, len(keys)*0.5, len(keys)*0.75, len(keys)-1, len(keys)-2]
                      if 0 <= x < len(keys)))
        pick = [keys[i] for i in idxs][:6]
    return clusters, pick


def print_b3_clusters(con, sampled):
    sampled_ids = [int(r["id"]) for r in sampled]
    clusters, pick = b3_clusters(con, sampled_ids)
    print(f"\n{'-'*78}\nB3 — {len(pick)} FULL burst clusters (all members; KEEP top-3 sharpest)\n{'-'*78}")
    for key in pick:
        members = sorted(clusters[key], key=lambda x: x["rank"])
        g = f"@({key[0]},{key[1]})"
        print(f"\ncluster {g}  size={members[0]['csize']}  year={members[0]['yr']}")
        print(f"  {'rank':>4} {'blur':>8}  {'decision':<8} filename")
        for x in members:
            dec = "KEEP" if x["rank"] <= 3 else "flag"
            print(f"  {x['rank']:>4} {x['blur']:>8.0f}  {dec:<8} {x['fn']}")


# ---------------------------------------------------------------- main
def main():
    os.makedirs(THUMBS, exist_ok=True)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    # ---- B1 overall image/video + GB split (full candidate set) ----
    b1_all = load_csv("B1_shared_album")
    b1_meta = meta_for(con, [int(r["id"]) for r in b1_all])
    img_n = img_b = vid_n = vid_b = 0
    for r in b1_all:
        m = b1_meta[int(r["id"])]
        if (m["extension"] or "").upper() in VIDEO_EXT:
            vid_n += 1; vid_b += m["file_size_bytes"] or 0
        else:
            img_n += 1; img_b += m["file_size_bytes"] or 0
    print("="*78)
    print("B1 OVERALL SPLIT (all 5,214 shared-album candidates):")
    print(f"  images: {img_n:,} / {img_b/1e9:.1f} GB    videos: {vid_n:,} / {vid_b/1e9:.1f} GB")

    # ---- per-rule processing ----
    samples = {}
    for rule in RULES:
        rows = load_csv(rule)
        sm = sample(rows)
        samples[rule] = sm
        meta = meta_for(con, [int(r["id"]) for r in sm])
        print_table(rule, sm, meta)
        if rule == "B3_burst_extras":
            print_b3_clusters(con, sm)
        # thumbnails + html
        outdir = os.path.join(THUMBS, rule)
        os.makedirs(outdir, exist_ok=True)
        thumbs = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {}
            for r in sm:
                m = meta[int(r["id"])]
                out = os.path.join(outdir, f"{m['id']}.jpg")
                futs[ex.submit(make_thumb, m, out)] = (m["id"], out)
            for f, (mid, out) in futs.items():
                ok, note = f.result()
                thumbs[mid] = (f"thumbs/{rule}/{os.path.basename(out)}" if ok else None, note)
        cells = []
        fails = 0
        for r in sm:
            m = meta[int(r["id"])]
            th, note = thumbs[m["id"]]
            if th is None:
                fails += 1
            cells.append((th, caption(rule, m, r)))
        build_html(rule, cells)
        print(f"  -> wrote samples/{rule}.html  ({len(sm)} thumbs, {fails} decode failures)")

    # ---- B5 full by-year histogram ----
    b5_all = load_csv("B5_junk_imports")
    by_year = {}
    for r in b5_all:
        by_year[r["year"]] = by_year.get(r["year"], 0) + 1
    print(f"\n{'='*78}\nB5 — FULL by-year histogram (all {len(b5_all):,} junk candidates)\n{'='*78}")
    pre2010 = 0
    for y in sorted(by_year, key=lambda x: int(x) if x else 0):
        bar = "#" * (by_year[y] // 20)
        print(f"  {y}: {by_year[y]:>5}  {bar}")
        if y and int(y) < 2010:
            pre2010 += by_year[y]
    print(f"  --> pre-2010 total: {pre2010:,}")

    con.close()


if __name__ == "__main__":
    main()
