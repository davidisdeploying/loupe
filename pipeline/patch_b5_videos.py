#!/usr/bin/env python3
"""
patch_b5_videos.py — give the 129 sampled B5 *video* junk files a reviewable
midpoint frame, then regenerate ONLY B5.html.

Read-only: no DB writes, no deletions. Scope = exactly the B5 SAMPLE videos that
failed to render as stills (MP4/MOV). Image thumbnails already on disk are reused
untouched (never re-read).
"""

import csv
import os
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import make_review_sheets as M  # reuse sample logic, HEAD, helpers

THUMBS = M.THUMBS
CS = M.CS
VIDEO_EXT = {"MP4", "MOV", "M4V"}
WORKERS = 200


def fmt_dur(d):
    if d is None:
        return "?:??"
    d = int(round(float(d)))
    return f"{d//60}:{d%60:02d}"


def extract_frame(fp, dur, out):
    """One midpoint frame via ffmpeg fast input-seek; cv2 fallback."""
    ss = max(0.0, (float(dur) / 2) if dur else 1.0)
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-ss", f"{ss:.3f}", "-i", fp,
             "-frames:v", "1", "-vf", "scale=400:-1", "-q:v", "4", out],
            capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return True, "ffmpeg"
        ffmpeg_err = (r.stderr or "")[-160:].replace("\n", " ")
    except FileNotFoundError:
        ffmpeg_err = "ffmpeg-not-installed"
    except Exception as e:
        ffmpeg_err = f"{type(e).__name__}: {e}"

    # fallback: cv2.VideoCapture -> midpoint frame
    try:
        import cv2
        cap = cv2.VideoCapture(fp)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if n > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, n // 2)
        ok, frame = cap.read()
        cap.release()
        if ok and frame is not None:
            h, w = frame.shape[:2]
            s = 400 / max(w, h)
            if s < 1:
                frame = cv2.resize(frame, (max(1, int(w * s)), max(1, int(h * s))))
            cv2.imwrite(out, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            return True, "cv2"
        return False, f"ffmpeg[{ffmpeg_err}] + cv2[no frame]"
    except Exception as e:
        return False, f"ffmpeg[{ffmpeg_err}] + cv2[{type(e).__name__}: {e}]"


def main():
    t0 = time.time()
    b5 = list(csv.DictReader(open(os.path.join(M.CULL, "B5_junk_imports.csv"))))
    sampled, total = M.sample_b5(b5)
    have = {int(f[:-4]) for f in os.listdir(THUMBS) if f.endswith(".jpg")}

    # the in-scope videos: sampled rows that are video-ext AND have no thumb yet
    vids = [r for r in sampled
            if r["extension"].upper() in VIDEO_EXT and int(r["id"]) not in have]
    vid_ids = {int(r["id"]) for r in sampled if r["extension"].upper() in VIDEO_EXT}
    print(f"B5 sample: {len(sampled)}/{total}; in-scope videos to frame: {len(vids)}", flush=True)

    # durations (read-only)
    db = sqlite3.connect(os.path.join(M.HERE, "metadata.db"), timeout=30)
    q = ",".join(str(int(r["id"])) for r in sampled)
    dur = {row[0]: row[1] for row in db.execute(
        f"SELECT id, duration_seconds FROM assets WHERE id IN ({q})")}

    def work(r):
        idv = int(r["id"])
        out = os.path.join(THUMBS, f"{idv}.jpg")
        ok, how = extract_frame(r["filepath"], dur.get(idv), out)
        return idv, ok, how

    ok_ids, fails = set(), []
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for idv, ok, how in ex.map(work, vids):
            done += 1
            if ok:
                ok_ids.add(idv)
            else:
                fails.append((idv, how))
            if done % 25 == 0:
                print(f"  framed {done}/{len(vids)} ok={len(ok_ids)} fail={len(fails)} "
                      f"({done/(time.time()-t0):.1f}/s)", flush=True)

    # ---- regenerate ONLY B5.html, reusing all thumbs on disk -----------------
    have2 = {int(f[:-4]) for f in os.listdir(THUMBS) if f.endswith(".jpg")}
    css_extra = (".cell.vid{border-color:#7fd1ff}"
                 ".badge{display:inline-block;background:#7fd1ff;color:#003;"
                 "font-weight:700;font-size:10px;padding:1px 5px;border-radius:3px;"
                 "margin-right:4px;vertical-align:middle}")
    cells = []
    n_video_cells = n_video_with_frame = 0
    for r in sampled:
        idv = int(r["id"]); mb = int(r["file_size_bytes"]) / 1e6
        is_vid = idv in vid_ids
        thumb_ok = idv in have2
        if is_vid:
            n_video_cells += 1
            if thumb_ok:
                n_video_with_frame += 1
            badge = '<span class=badge>▶ VIDEO</span>'
            extra = f' · {fmt_dur(dur.get(idv))}'
        else:
            badge = ""
            extra = ""
        cap = (f'{badge}<b>{idv}</b> · {r["year"]}<br>{M.short_path(r["filepath"])}<br>'
               f'<span class=m>{r["extension"]} · {mb:.1f} MB{extra}</span>')
        cls = "cell vid" if is_vid else "cell"
        if thumb_ok:
            img = f'<img loading=lazy src="thumbs/{idv}.jpg">'
        else:
            img = ('<div style="height:160px;display:flex;align-items:center;'
                   'justify-content:center;color:#a55">no frame</div>')
        cells.append(f'<div class="{cls}">{img}<div class=cap>{cap}</div></div>')

    note = (f"bucket total = {total} · sampled = {len(sampled)}<br>"
            f"sampling: stratified by extension × size-band; ALL &gt;20MB in full, "
            f"~40 per (ext,band) for the rest; largest first.<br>"
            f"{n_video_with_frame}/{n_video_cells} video files show a midpoint frame "
            f"(▶ VIDEO badge); any without a frame stay as a metadata placeholder.")
    html = (M.HEAD.format(title="B5 — junk imports (review)", note=note)
            .replace("</style>", css_extra + "</style>")
            + '<div class=grid>' + "".join(cells) + "</div>")
    with open(os.path.join(CS, "B5.html"), "w") as f:
        f.write(html)

    print("=" * 56, flush=True)
    print(f"videos framed OK: {len(ok_ids)}/{len(vids)}  failed: {len(fails)}", flush=True)
    for idv, how in fails:
        print(f"  FAIL id={idv}: {how}", flush=True)
    print(f"B5.html: {n_video_with_frame}/{n_video_cells} video cells now have a frame", flush=True)
    print(f"elapsed {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
