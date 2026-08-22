#!/usr/bin/env python3
"""
make_review_sheets.py — build browser contact sheets for the three human-review
buckets (B4 blurry, B2 screenshots, B5 junk), plus the B4 low-texture
false-positive guard.

READ-ONLY w.r.t. the photo library and metadata.db. The ONLY data file written
is culling/B4_blurry_reviewed.csv; everything else is review artifacts
(thumbnails + HTML) under culling/contactsheets/.

NAS reality: every file open() on the mount costs ~36s of pure wait regardless
of size, but concurrency scales linearly. So all reads go through a big thread
pool (latency-bound, not CPU-bound).
"""

import csv
import io
import os
import sys
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import cv2
from PIL import Image
import pillow_heif
pillow_heif.register_heif_opener()

HERE = os.path.dirname(os.path.abspath(__file__))
CULL = os.path.join(HERE, "culling")
CS = os.path.join(CULL, "contactsheets")
THUMBS = os.path.join(CS, "thumbs")
os.makedirs(THUMBS, exist_ok=True)

WORKERS = 200
BLUR_LONG_EDGE = 1024          # match ingest.py's normalization for comparability
THUMB_LONG_EDGE = 400
P10 = 93.6                     # the "not blurry" bar (10th pctile) used by rule B4
FP_RATIO = 2.0                 # center must be >= this x global to be a suspect
LOG = open(os.path.join(CS, "_render.log"), "w")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.write(line + "\n"); LOG.flush()


# W16 / P7: display-path stripping follows the configured library root instead of one
# host's mount point. Default matches server.py, so output is unchanged when unset.
LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", os.path.join(os.sep, "mnt", "nas", "photos"))


def short_path(fp):
    return (fp or "").replace(LIBRARY_ROOT.rstrip(os.sep) + os.sep, "")


def decode_rgb(data):
    """Bytes -> PIL RGB image, downscaled toward BLUR_LONG_EDGE during decode."""
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (BLUR_LONG_EDGE, BLUR_LONG_EDGE))  # JPEG fast path; no-op else
    return im.convert("RGB")


def blur_scores(rgb):
    """Return (global_var, centercrop_var) on the 1024-normalized grayscale."""
    gray = rgb.convert("L")
    w, h = gray.size
    s = BLUR_LONG_EDGE / max(w, h)
    if s < 1.0:
        gray = gray.resize((max(1, round(w * s)), max(1, round(h * s))))
    arr = np.asarray(gray, dtype=np.float64)
    g = float(cv2.Laplacian(arr, cv2.CV_64F).var())
    H, W = arr.shape
    cc = arr[int(H * 0.25):int(H * 0.75), int(W * 0.25):int(W * 0.75)]
    c = float(cv2.Laplacian(cc, cv2.CV_64F).var()) if cc.size else 0.0
    return g, c


def write_thumb(rgb, idv):
    t = rgb.copy()
    t.thumbnail((THUMB_LONG_EDGE, THUMB_LONG_EDGE))
    t.save(os.path.join(THUMBS, f"{idv}.jpg"), "JPEG", quality=80)


def read_bytes(fp):
    with open(fp, "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# load rule outputs (already glasses/workproduct-excluded)
# ---------------------------------------------------------------------------
def load(name):
    with open(os.path.join(CULL, name)) as f:
        return list(csv.DictReader(f))


# ===========================================================================
# PHASE 1 — B4 guard (reads ALL B4 candidates) + thumbnails
# ===========================================================================
def phase_b4():
    rows = load("B4_blurry.csv")
    log(f"B4: {len(rows)} candidates — reading all for the low-texture guard")
    results = {}
    fails = []

    def work(r):
        idv = int(r["id"]); fp = r["filepath"]
        try:
            data = read_bytes(fp)
            rgb = decode_rgb(data)
            g, c = blur_scores(rgb)
            write_thumb(rgb, idv)
            return idv, g, c, None
        except Exception as e:
            return idv, None, None, f"{type(e).__name__}: {e}"

    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for idv, g, c, err in ex.map(work, rows):
            done += 1
            if err:
                fails.append((idv, err))
            else:
                results[idv] = (g, c)
            if done % 250 == 0:
                log(f"  B4 guard {done}/{len(rows)}  ({done/(time.time()-t0):.1f}/s)  fails={len(fails)}")

    # write reviewed CSV (all candidates; failed renders get NULL scores)
    out = os.path.join(CULL, "B4_blurry_reviewed.csv")
    fp_count = 0
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "filepath", "year", "blur_global", "blur_centercrop",
                    "fp_suspect", "in_burst"])
        for r in rows:
            idv = int(r["id"])
            in_burst = 1 if r.get("in_burst") in ("1", "True", "true") else 0
            if idv in results:
                g, c = results[idv]
                fp_suspect = 1 if (c >= P10 and c >= FP_RATIO * g) else 0
                fp_count += fp_suspect
                w.writerow([idv, r["filepath"], r["year"], f"{g:.1f}", f"{c:.1f}",
                            fp_suspect, in_burst])
            else:
                w.writerow([idv, r["filepath"], r["year"], "", "", "", in_burst])
    log(f"B4: wrote {out}  fp_suspect={fp_count}  render_fails={len(fails)}")
    return rows, results, fp_count, fails


def sample_b4_for_sheet(rows, results):
    """Sheet focuses on the ISOLATED blurry (the risky ones not covered by B3).
       Sections: (1) all fp_suspect isolated; (2) all pre-2013 isolated + ~80/yr
       for 2013+, worst-blur (lowest global) first."""
    iso = []
    for r in rows:
        idv = int(r["id"])
        if r.get("in_burst") in ("1", "True", "true"):
            continue
        if idv not in results:
            continue
        g, c = results[idv]
        fp_suspect = 1 if (c >= P10 and c >= FP_RATIO * g) else 0
        iso.append({"id": idv, "filepath": r["filepath"], "year": int(r["year"]),
                    "g": g, "c": c, "fp": fp_suspect, "in_burst": 0})
    fp_items = sorted([x for x in iso if x["fp"]], key=lambda x: x["g"])
    rest = [x for x in iso if not x["fp"]]
    by_year = defaultdict(list)
    for x in rest:
        by_year[x["year"]].append(x)
    sampled = []
    for y in sorted(by_year):
        items = sorted(by_year[y], key=lambda x: x["g"])  # worst (lowest) first
        sampled.extend(items if y < 2013 else items[:80])
    sampled.sort(key=lambda x: x["g"])
    return fp_items, sampled, len(iso)


# ===========================================================================
# PHASE 2 — B2 screenshots (sampled), PHASE 3 — B5 junk (sampled)
# ===========================================================================
def sample_b2(rows):
    """~80/year, weighting recent years fuller (keepers hide in recent years)."""
    by_year = defaultdict(list)
    for r in rows:
        by_year[int(r["year"])].append(r)
    cap = lambda y: 150 if y >= 2024 else 120 if y >= 2022 else 80
    sampled = []
    for y in sorted(by_year):
        items = by_year[y]
        sampled.extend(items[:cap(y)])
    return sampled, len(rows)


def sample_b5(rows):
    """Stratify by extension x size-band. Show ALL the largest (>20MB) in full;
       sample ~40 per (ext, band) for the rest."""
    def band(sz):
        mb = sz / 1e6
        if mb >= 20: return "A_>20MB"
        if mb >= 5:  return "B_5-20MB"
        if mb >= 1:  return "C_1-5MB"
        return "D_<1MB"
    groups = defaultdict(list)
    for r in rows:
        sz = int(r["file_size_bytes"])
        groups[(r["extension"], band(sz))].append((sz, r))
    sampled = []
    for (ext, b), items in groups.items():
        items.sort(key=lambda t: -t[0])
        take = items if b == "A_>20MB" else items[:40]
        sampled.extend(r for _, r in take)
    sampled.sort(key=lambda r: -int(r["file_size_bytes"]))
    return sampled, len(rows)


def render_thumbs(items, idkey="id", fpkey="filepath"):
    """Read+thumb a list of dict rows (B2/B5). Returns set of ok ids + failures."""
    ok, fails = set(), []

    def work(r):
        idv = int(r[idkey]); fp = r[fpkey]
        try:
            rgb = decode_rgb(read_bytes(fp))
            write_thumb(rgb, idv)
            return idv, None
        except Exception as e:
            return idv, f"{type(e).__name__}: {e}"

    done = 0; t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for idv, err in ex.map(work, items):
            done += 1
            if err: fails.append((idv, err))
            else: ok.add(idv)
            if done % 200 == 0:
                log(f"  thumbs {done}/{len(items)} ({done/(time.time()-t0):.1f}/s) fails={len(fails)}")
    return ok, fails


# ===========================================================================
# HTML
# ===========================================================================
HEAD = """<!doctype html><meta charset=utf-8><title>{title}</title>
<style>
body{{font:13px/1.4 system-ui,sans-serif;margin:16px;background:#111;color:#ddd}}
h1{{font-size:18px}} h2{{font-size:15px;margin:24px 0 8px;color:#ffd479}}
.note{{color:#999;margin:4px 0 16px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}}
.cell{{background:#1c1c1c;border:1px solid #333;border-radius:6px;padding:6px;overflow:hidden}}
.cell.fp{{border-color:#ffd479}}
.cell img{{width:100%;height:160px;object-fit:contain;background:#000;border-radius:4px}}
.cap{{font-size:11px;color:#bbb;margin-top:4px;word-break:break-all}}
.cap b{{color:#fff}} .m{{color:#7fd1ff}}
</style>
<h1>{title}</h1><div class=note>{note}</div>
"""


def cell(idv, thumb_ok, caption, fp=False):
    cls = "cell fp" if fp else "cell"
    if thumb_ok:
        img = f'<img loading=lazy src="thumbs/{idv}.jpg">'
    else:
        img = '<div style="height:160px;display:flex;align-items:center;' \
              'justify-content:center;color:#a55">render failed</div>'
    return f'<div class="{cls}">{img}<div class=cap>{caption}</div></div>'


def write_html(fn, title, note, sections):
    """sections: list of (heading_or_None, [cell_html...])"""
    parts = [HEAD.format(title=title, note=note)]
    for heading, cells in sections:
        if heading:
            parts.append(f"<h2>{heading}</h2>")
        parts.append('<div class=grid>' + "".join(cells) + "</div>")
    with open(os.path.join(CS, fn), "w") as f:
        f.write("\n".join(parts))


def build_b4_html(fp_items, sampled, iso_total, total, fp_total):
    def cap(x):
        return (f'<b>{x["id"]}</b> · {x["year"]}<br>{short_path(x["filepath"])}<br>'
                f'<span class=m>g={x["g"]:.0f} cc={x["c"]:.0f} '
                f'fp={x["fp"]} burst={x["in_burst"]}</span>')
    sec1 = [cell(x["id"], True, cap(x), fp=True) for x in fp_items]
    sec2 = [cell(x["id"], True, cap(x)) for x in sampled]
    note = (f"bucket total (all B4) = {total} · isolated (not in burst) = {iso_total} · "
            f"fp_suspect (whole bucket) = {fp_total}<br>"
            f"sampling: ALL {len(fp_items)} fp-suspect isolated shown first; then "
            f"ALL pre-2013 isolated + ~80/yr for 2013+ ({len(sampled)} shown), worst-blur first")
    write_html("B4.html", "B4 — blurry (review, low-texture guarded)", note,
               [(f"FALSE-POSITIVE SUSPECTS — likely sharp subject / flat background ({len(fp_items)})", sec1),
                (f"ISOLATED BLURRY — sampled ({len(sampled)})", sec2)])


def build_b2_html(sampled, ok, total, by_year_counts):
    cells = []
    for r in sampled:
        idv = int(r["id"])
        cap = (f'<b>{idv}</b> · {r["year"]}<br>{short_path(r["filepath"])}<br>'
               f'<span class=m>{r["width_pixels"]}×{r["height_pixels"]}</span>')
        cells.append(cell(idv, idv in ok, cap))
    note = (f"bucket total = {total} · sampled = {len(sampled)}<br>"
            f"sampling: per-year cap (150 for 2024+, 120 for 2022-23, 80 older). "
            f"by-year totals: " + ", ".join(f"{y}:{n}" for y, n in sorted(by_year_counts.items())))
    write_html("B2.html", "B2 — screenshots (review)", note, [(None, cells)])


def build_b5_html(sampled, ok, total):
    cells = []
    for r in sampled:
        idv = int(r["id"]); mb = int(r["file_size_bytes"]) / 1e6
        cap = (f'<b>{idv}</b> · {r["year"]}<br>{short_path(r["filepath"])}<br>'
               f'<span class=m>{r["extension"]} · {mb:.1f} MB</span>')
        cells.append(cell(idv, idv in ok, cap))
    note = (f"bucket total = {total} · sampled = {len(sampled)}<br>"
            f"sampling: stratified by extension × size-band; ALL >20MB shown in full, "
            f"~40 per (ext,band) for the rest; largest first")
    write_html("B5.html", "B5 — junk imports (review)", note, [(None, cells)])


def main():
    t0 = time.time()
    all_fails = []

    # PHASE 1 — B4 guard
    b4_rows, b4_results, fp_total, b4_fails = phase_b4()
    all_fails += [("B4", i, e) for i, e in b4_fails]
    fp_items, b4_sampled, iso_total = sample_b4_for_sheet(b4_rows, b4_results)

    # PHASE 2 — B2 sampled
    b2_rows = load("B2_screenshots.csv")
    b2_sampled, b2_total = sample_b2(b2_rows)
    by_year_counts = defaultdict(int)
    for r in b2_rows:
        by_year_counts[int(r["year"])] += 1
    log(f"B2: {b2_total} total -> {len(b2_sampled)} sampled; rendering")
    b2_ok, b2_fails = render_thumbs(b2_sampled)
    all_fails += [("B2", i, e) for i, e in b2_fails]

    # PHASE 3 — B5 sampled
    b5_rows = load("B5_junk_imports.csv")
    b5_sampled, b5_total = sample_b5(b5_rows)
    log(f"B5: {b5_total} total -> {len(b5_sampled)} sampled; rendering")
    b5_ok, b5_fails = render_thumbs(b5_sampled)
    all_fails += [("B5", i, e) for i, e in b5_fails]

    # HTML
    build_b4_html(fp_items, b4_sampled, iso_total, len(b4_rows), fp_total)
    build_b2_html(b2_sampled, b2_ok, b2_total, by_year_counts)
    build_b5_html(b5_sampled, b5_ok, b5_total)

    log("=" * 60)
    log(f"B4: total={len(b4_rows)} fp_suspect={fp_total} isolated={iso_total} "
        f"sampled={len(fp_items)+len(b4_sampled)} (fp={len(fp_items)} + iso={len(b4_sampled)})")
    log(f"B2: total={b2_total} sampled={len(b2_sampled)} rendered_ok={len(b2_ok)}")
    log(f"B5: total={b5_total} sampled={len(b5_sampled)} rendered_ok={len(b5_ok)}")
    log(f"TOTAL render failures: {len(all_fails)}")
    for bucket, i, e in all_fails[:25]:
        log(f"  FAIL {bucket} id={i}: {e}")
    log(f"elapsed {time.time()-t0:.0f}s")
    LOG.close()


if __name__ == "__main__":
    main()
