#!/usr/bin/env python3
"""Contact sheet for the production-suspect videos (read-only on originals).
Pulls one frame per video, writes culling/samples/production_suspect.html.
Ordered by year then longest-first."""
import csv, os, sqlite3, subprocess
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

PROJ = os.path.expanduser("~/loupe-pipeline")
CULL = os.path.join(PROJ, "culling")
SAMP = os.path.join(CULL, "samples")
OUT  = os.path.join(SAMP, "thumbs", "production_suspect")
TH = 256

rows = list(csv.DictReader(open(os.path.join(CULL, "production_suspect.csv"))))
con = sqlite3.connect(f"file:{os.path.join(PROJ,'metadata.db')}?mode=ro", uri=True)
caps = dict(con.execute(
    "SELECT id, capture_timestamp FROM assets WHERE id IN (%s)"
    % ",".join(r["id"] for r in rows)).fetchall())
con.close()

for r in rows:
    r["dur"] = float(r["duration_seconds"])
    r["mb"] = (int(r["file_size_bytes"]) or 0) / 1e6
    ts = caps.get(int(r["id"]))
    r["mon"] = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m") if ts else "—"
rows.sort(key=lambda r: (r["cap_year"], -r["dur"]))

os.makedirs(OUT, exist_ok=True)

def frame(r):
    out = os.path.join(OUT, f"{r['id']}.jpg")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1", "-i", r["filepath"],
           "-frames:v", "1", "-vf", f"scale={TH}:-1", out]
    if subprocess.run(cmd, capture_output=True, timeout=120).returncode != 0 \
       or not os.path.exists(out):
        cmd[cmd.index("-ss")+1] = "0"
        subprocess.run(cmd, capture_output=True, timeout=120)
    return r["id"], os.path.exists(out)

with ThreadPoolExecutor(max_workers=4) as ex:
    ok = dict(ex.map(frame, rows))

def mmss(s):
    return f"{int(s//60)}:{int(s%60):02d}"

cells = []
for r in rows:
    th = f"thumbs/production_suspect/{r['id']}.jpg" if ok[r['id']] else None
    big = " style='outline:2px solid #fa0'" if r["dur"] > 1200 else ""
    img = f"<img src='{th}'>" if th else "<div class=x>no frame</div>"
    cells.append(f"<div class=c{big}>{img}<div class=cap><b>{r['filename']}</b><br>"
                 f"⏱ {mmss(r['dur'])} · {r['mb']:.0f}MB<br>{r['mon']} · id {r['id']}</div></div>")

html = ("<!doctype html><meta charset=utf-8><title>production-suspect</title>"
        "<style>body{background:#111;color:#ddd;font:12px system-ui;margin:16px}"
        ".g{display:flex;flex-wrap:wrap;gap:10px}.c{width:256px}"
        ".c img{width:256px;height:256px;object-fit:contain;background:#000;border:1px solid #333}"
        ".x{width:256px;height:256px;display:flex;align-items:center;justify-content:center;"
        "background:#300;border:1px solid #533;color:#f99}"
        ".cap{margin-top:4px;line-height:1.35}</style>"
        f"<h2>Production-suspect — {len(rows)} videos (>5min, no GPS, HOME-1 era)</h2>"
        "<p>Orange outline = &gt;20min. Sorted by year, longest first.</p><div class=g>"
        + "".join(cells) + "</div>")
open(os.path.join(SAMP, "production_suspect.html"), "w").write(html)
print(f"wrote samples/production_suspect.html — {len(rows)} videos, "
      f"{sum(1 for v in ok.values() if not v)} frame failures")
