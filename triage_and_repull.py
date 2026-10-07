#!/usr/bin/env python3
"""Triage the 3 UNKNOWN quarantined videos and prep the iCloud re-pull manifest.

- Runs the FULL /api/play transcode on each UNKNOWN end-to-end. A clip that re-encodes
  to a complete <=720p H.264 file is intact (the midpoint frame-grab just failed); one
  whose transcode bails short is genuinely broken.
- For intact ones: regenerate the thumbnail from the CLEAN transcode at a safe offset
  (~1.0s, never t=0) into the shared thumb cache, so the tile stops spinning.
- Builds ~/loupe/icloud-repull-list.csv/.md for the broken set (2 truncated + any
  UNKNOWN that fails) and updates the quarantine verdicts.

Read-only on metadata.db / decisions.db. Writes ONLY: thumb cache, play cache, the
re-pull manifest, the quarantine artifacts. No deletions, no iCloud, no touching originals.
"""
import csv, json, os, subprocess, sqlite3, time

V2 = "/home/david/loupe-pipeline"
META = f"{V2}/metadata.db"
THUMBS = f"{V2}/culling/contactsheets/thumbs"
PLAY_CACHE = "/home/david/loupe/cache/play"
QCSV = "/home/david/loupe/quarantine-unreadable-videos.csv"
QMD = "/home/david/loupe/quarantine-unreadable-videos.md"
RCSV = "/home/david/loupe/icloud-repull-list.csv"
RMD = "/home/david/loupe/icloud-repull-list.md"

UNKNOWNS = [58036, 87177, 89809]
TRUNCATED = [51490, 77887]
TRUNC_REASON = {
    51490: "36 B stub — ffprobe 'moov atom not found' (truncated/failed iCloud download)",
    77887: "163 KB for a 12 MP HEIC (4032x3024) — 'moov atom not found' (truncated download)",
}
os.makedirs(PLAY_CACHE, exist_ok=True)


def transcode(src, out):  # exact /api/play command
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-i", src,
         "-vf", "scale=w=1280:h=720:force_original_aspect_ratio=decrease:force_divisible_by=2",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out],
        capture_output=True, text=True, timeout=900)


def probe_dur(f):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "json", f], capture_output=True, text=True, timeout=60)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None


def video_streams(f):
    """Count VIDEO streams in a file — a real clip yields >=1; an audio-only or empty
    transcode yields 0 (the tell that the source video track is missing/undecodable)."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                            "-show_entries", "stream=codec_type", "-of", "csv=p=0", f],
                           capture_output=True, text=True, timeout=60)
        return len([l for l in r.stdout.splitlines() if l.strip() == "video"])
    except Exception:
        return 0


con = sqlite3.connect(f"file:{META}?mode=ro", uri=True); con.row_factory = sqlite3.Row

triage = {}
print("=== triage (full play-transcode, end to end) ===")
for idv in UNKNOWNS:
    row = con.execute("SELECT filepath, duration_seconds FROM assets WHERE id=?", (idv,)).fetchone()
    src, exp = row["filepath"], row["duration_seconds"]
    tmp = f"/tmp/triage_{idv}.mp4"
    p = transcode(src, tmp)
    ok = p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0
    nvid = video_streams(tmp) if ok else 0          # intact only if the transcode has a VIDEO stream
    outdur = probe_dur(tmp) if ok else None
    errs = p.stderr.lower().count("error")
    thumb_done, intact, reason = False, False, ""
    if ok and nvid >= 1:
        off = 1.0 if (outdur and outdur > 1.2) else max(0.1, (outdur or 1) * 0.3)
        tj = f"{THUMBS}/{idv}.jpg.tmp.jpg"
        tp = subprocess.run(["ffmpeg", "-nostdin", "-y", "-ss", f"{off:.2f}", "-i", tmp,
                             "-frames:v", "1", "-vf", "scale=400:-1", "-q:v", "4", tj],
                            capture_output=True, text=True, timeout=120)
        if tp.returncode == 0 and os.path.exists(tj) and os.path.getsize(tj) > 0:
            os.replace(tj, f"{THUMBS}/{idv}.jpg")
            os.replace(tmp, f"{PLAY_CACHE}/{idv}.mp4")     # cache the playable preview
            thumb_done = intact = True
        elif os.path.exists(tj):
            os.remove(tj)
            reason = "transcode had a video stream but no frame could be grabbed"
    if not intact:
        if os.path.exists(tmp):
            os.remove(tmp)
        stale = f"{PLAY_CACHE}/{idv}.mp4"               # drop any misleading (e.g. audio-only) cached preview
        if os.path.exists(stale):
            os.remove(stale)
        if not reason:
            if ok and nvid == 0:
                reason = "transcode produced NO video stream (audio/data only) — video track missing/undecodable"
            else:
                last = (p.stderr.strip().splitlines()[-1][:110] if p.stderr.strip() else "")
                reason = f"transcode decoded no usable video (rc {p.returncode}, {nvid} vid stream) {last}"
    triage[idv] = {"complete": intact, "outdur": outdur, "exp": exp, "errs": errs,
                   "thumb": thumb_done, "reason": reason, "nvid": nvid}
    print(f"  id {idv}: {'INTACT' if intact else 'BROKEN'} "
          f"(vid-streams={nvid}, out={outdur}/exp={exp}s) · thumbnail "
          f"{'REGENERATED' if thumb_done else 'no'}  — {reason or 'clean'}")

intact = [i for i in UNKNOWNS if triage[i]["complete"]]
broken_unknown = [i for i in UNKNOWNS if not triage[i]["complete"]]
repull_ids = TRUNCATED + broken_unknown


def fmt_ts(ts):
    if not ts:
        return "(no capture timestamp — locate by folder date + filename)"
    return time.strftime("%Y-%m-%d %H:%M:%S %a", time.localtime(ts))


# --- re-pull manifest -------------------------------------------------------
man = []
for idv in repull_ids:
    r = con.execute("SELECT filepath, filename, extension, capture_timestamp, camera_make, "
                    "camera_model, gps_lat, gps_lon, file_size_bytes FROM assets WHERE id=?",
                    (idv,)).fetchone()
    fp = r["filepath"]; disk = os.path.getsize(fp) if os.path.exists(fp) else 0
    reason = TRUNC_REASON.get(idv) or triage.get(idv, {}).get("reason", "failed full transcode")
    man.append({"id": idv, "filename": r["filename"], "filepath": fp,
                "capture_timestamp": fmt_ts(r["capture_timestamp"]),
                "camera": " ".join(x for x in (r["camera_make"], r["camera_model"]) if x) or "(none)",
                "gps": (f'{r["gps_lat"]:.5f}, {r["gps_lon"]:.5f}' if r["gps_lat"] is not None else "(none)"),
                "on_disk_bytes": disk, "failure_reason": reason})

stamp = time.strftime("%Y-%m-%d %H:%M %Z")
cols = ["id", "filename", "filepath", "capture_timestamp", "camera", "gps", "on_disk_bytes", "failure_reason"]
with open(RCSV, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
    for m in man:
        w.writerow(m)
with open(RMD, "w") as f:
    f.write("# iCloud re-pull list\n\n")
    f.write("> **Re-pull these from iCloud on the MacBook (Photos.app / osxphotos) — local "
            "copies are truncated/damaged; iCloud originals expected intact.**\n\n")
    f.write(f"_Generated {stamp} · {len(man)} files · the the compute host is not signed into iCloud, "
            f"so this is the MacBook's job._\n\n")
    for m in man:
        f.write(f"## id {m['id']} — {m['filename']}\n\n")
        f.write(f"- **captured: {m['capture_timestamp']}**  ← find it in Photos.app by this date/time\n")
        f.write(f"- camera: {m['camera']} · GPS: {m['gps']}\n")
        f.write(f"- local (damaged): `{m['filepath']}` — {m['on_disk_bytes']} B on disk\n")
        f.write(f"- why: {m['failure_reason']}\n\n")

# --- update quarantine verdicts (keep DO-NOT-DELETE header) -----------------
qrows = list(csv.DictReader(open(QCSV)))
for r in qrows:
    idv = int(r["id"])
    if idv in intact:
        r["classification"] = (f"INTACT — full transcode clean ({triage[idv]['outdur']}s); "
                               f"thumbnail regenerated; DROPPED from re-pull")
    elif idv in broken_unknown:
        r["classification"] = "CONFIRMED RE-PULL — failed full transcode too: " + triage[idv]["reason"][:100]
with open(QCSV, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(qrows[0].keys())); w.writeheader(); w.writerows(qrows)
with open(QMD, "w") as f:
    f.write("# Quarantine — videos unreadable at thumbnail pre-gen\n\n")
    f.write("> **DO NOT delete any id listed here from iCloud until verified against the "
            "iCloud original on the MacBook.** This is the record the eventual deletion phase MUST exclude.\n\n")
    f.write(f"_Updated {stamp} after local triage (full play-transcode). {len(intact)} found INTACT "
            f"(thumbnail fixed, dropped); {len(repull_ids)} still need a MacBook re-pull._\n\n")
    for r in qrows:
        f.write(f"## id {r['id']} — {r['classification']}\n\n")
        f.write(f"- file: `{r['filepath']}`\n")
        f.write(f"- ext/date: {r['extension']} · {r['year']}-{r['month']}\n")
        f.write(f"- on disk: {r['on_disk']}, size={r['disk_size_bytes']} B\n")
        f.write(f"- ffprobe: reads={r['ffprobe_reads']}, format={r['ffprobe_format']}; err: {r['ffprobe_err']}\n")
        f.write(f"- exiftool header: dur={r['exif_dur_s']} s, {r['exif_w']}x{r['exif_h']}, type={r['exif_filetype']}\n")
        f.write(f"- **decisions.db: {r['decision']}**\n\n")

con.close()
print("\n=== result ===")
print("INTACT (thumbnail fixed, dropped from re-pull):", intact)
print("still need MacBook re-pull:", repull_ids)
print("manifest:", RCSV)
