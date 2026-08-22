#!/usr/bin/env python3
"""Read-only forensics on the videos that failed thumbnail pre-gen. Writes a durable
quarantine record (CSV + MD). Touches nothing: metadata.db / decisions.db opened
read-only; only the two artifact files are written. No iCloud, no deletes."""
import csv, json, os, sqlite3, subprocess, time

PREGEN_LOG = "/home/david/loupe/pregen.log"
META_DB = "/home/david/loupe-pipeline/metadata.db"          # the db server.py opens (V2/metadata.db)
DEC_DB = "/home/david/loupe/decisions.db"             # unified decisions store
EXIFTOOL = "/home/david/loupe-pipeline/vendor/exiftool-dist/exiftool"
CSV_OUT = "/home/david/loupe/quarantine-unreadable-videos.csv"
MD_OUT = "/home/david/loupe/quarantine-unreadable-videos.md"
TRUNC_FLOOR_BPS = 50000   # < 50 KB/s for the recorded duration = implausibly small (iPhone video is MB/s)

def fail_ids():
    ids = set()
    for line in open(PREGEN_LOG):
        if "fail id=" in line:
            try: ids.add(int(line.split("fail id=")[1].split(":")[0].strip()))
            except Exception: pass
    return sorted(ids)

def ffprobe(fp):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
            "format=format_name,duration:stream=codec_type,codec_name,width,height",
            "-of", "json", fp], capture_output=True, text=True, timeout=90)
        data = json.loads(r.stdout or "{}")
        fmt = (data.get("format") or {})
        streams = data.get("streams") or []
        dur = fmt.get("duration")
        dur = float(dur) if dur not in (None, "N/A") else None
        has_video = any(s.get("codec_type") == "video" for s in streams)
        err = (r.stderr or "").strip().replace("\n", " ")[:200]
        reads = (r.returncode == 0 and (fmt.get("format_name") or streams))
        return {"reads": bool(reads), "format": fmt.get("format_name"), "dur": dur,
                "has_video": has_video, "nstreams": len(streams), "err": err, "rc": r.returncode}
    except Exception as e:
        return {"reads": False, "format": None, "dur": None, "has_video": False,
                "nstreams": 0, "err": f"{type(e).__name__}: {e}"[:200], "rc": -1}

def exif(fp):
    try:
        r = subprocess.run([EXIFTOOL, "-j", "-n", "-Duration", "-MediaDuration",
            "-TrackDuration", "-ImageWidth", "-ImageHeight", "-FileType", "-MIMEType", fp],
            capture_output=True, text=True, timeout=90)
        d = (json.loads(r.stdout or "[]") or [{}])[0]
        dur = d.get("Duration") or d.get("MediaDuration") or d.get("TrackDuration")
        try: dur = float(dur)
        except (TypeError, ValueError): dur = None
        return {"dur": dur, "w": d.get("ImageWidth"), "h": d.get("ImageHeight"),
                "filetype": d.get("FileType"), "mime": d.get("MIMEType"),
                "err": (r.stderr or "").strip()[:120]}
    except Exception as e:
        return {"dur": None, "w": None, "h": None, "filetype": None, "mime": None,
                "err": f"{type(e).__name__}: {e}"[:120]}

def plausible_min_bytes(ext, db_dur, db_w, db_h):
    """Conservative floor below which the file is too small to be the real media —
    judged against RECORDED duration/resolution (NOT db_size, which == on-disk size)."""
    ext = (ext or "").upper()
    is_video = ext in {"MP4", "MOV", "M4V", "AVI", "3GP", "MPG", "MKV", "WEBM"}
    floors = [50_000]                                  # any real photo/video is well over ~50 KB
    if is_video:
        if db_dur and db_dur > 0:
            floors.append(db_dur * TRUNC_FLOOR_BPS)    # bytes/s for the recorded duration
    else:                                              # still image (HEIC/JPG/…)
        floors.append(300_000)                         # a real iPhone photo is hundreds of KB+
        if db_w and db_h:
            floors.append((db_w * db_h / 1e6) * 80_000)  # ~80 KB per megapixel, very conservative
    return max(floors)

def classify(exists, disk, db, ffp, ex):
    if not exists or disk == 0:
        return "MISSING — re-pull from iCloud"
    # header parses if ANY tool can still read container facts (duration / dims / filetype)
    header_parses = (bool(ffp["reads"]) or ex["dur"] is not None
                     or bool(ex.get("filetype")) or bool(ex.get("w")))
    tiny = disk < plausible_min_bytes(db.get("extension"), db.get("duration_seconds"),
                                      db.get("width_pixels"), db.get("height_pixels"))
    if tiny:                                            # implausibly small for recorded dur/res
        if header_parses:
            return "LIKELY TRUNCATED DOWNLOAD — iCloud original probably intact, re-pull"
        return "TOO SMALL / BROKEN DOWNLOAD — re-pull from iCloud (verify original)"
    if not ffp["reads"]:                               # plausible/normal size but won't decode
        return "LIKELY CORRUPT — verify on MacBook, probably a safe cut"
    return "UNKNOWN — verify vs iCloud original"

def main():
    ids = fail_ids()
    mdb = sqlite3.connect(f"file:{META_DB}?mode=ro", uri=True); mdb.row_factory = sqlite3.Row
    ddb = sqlite3.connect(f"file:{DEC_DB}?mode=ro", uri=True)
    rows = []
    for idv in ids:
        m = mdb.execute("SELECT filepath, filename, extension, year, month, file_size_bytes, "
                        "duration_seconds, width_pixels, height_pixels FROM assets WHERE id=?",
                        (idv,)).fetchone()
        m = dict(m) if m else {}
        fp = m.get("filepath") or ""
        exists = bool(fp) and os.path.exists(fp)
        disk = os.stat(fp).st_size if exists else 0
        ffp = ffprobe(fp) if exists else {"reads": False, "format": None, "dur": None,
                                          "has_video": False, "nstreams": 0, "err": "file missing", "rc": -1}
        ex = exif(fp) if exists else {"dur": None, "w": None, "h": None, "filetype": None,
                                      "mime": None, "err": "file missing"}
        dur = ex["dur"] or ffp["dur"] or m.get("duration_seconds")  # best available duration
        dec = ddb.execute("SELECT state FROM decisions WHERE id=?", (idv,)).fetchone()
        decision = dec[0] if dec else "(none)"
        cls = classify(exists, disk, m, ffp, ex)
        rows.append({
            "id": idv, "filename": m.get("filename"), "extension": m.get("extension"),
            "year": m.get("year"), "month": m.get("month"),
            "db_size_bytes": m.get("file_size_bytes"), "db_duration_s": m.get("duration_seconds"),
            "db_w": m.get("width_pixels"), "db_h": m.get("height_pixels"),
            "on_disk": "yes" if exists else "MISSING", "disk_size_bytes": disk,
            "ffprobe_reads": "yes" if ffp["reads"] else "no", "ffprobe_format": ffp["format"],
            "ffprobe_dur_s": ffp["dur"], "ffprobe_err": ffp["err"],
            "exif_dur_s": ex["dur"], "exif_w": ex["w"], "exif_h": ex["h"],
            "exif_filetype": ex["filetype"], "bytes_per_s": round(disk / dur) if (dur and dur > 0 and disk) else None,
            "decision": decision, "classification": cls, "filepath": fp,
        })
    mdb.close(); ddb.close()

    cols = ["id", "filename", "extension", "year", "month", "db_size_bytes", "db_duration_s",
            "db_w", "db_h", "on_disk", "disk_size_bytes", "bytes_per_s", "ffprobe_reads",
            "ffprobe_format", "ffprobe_dur_s", "exif_dur_s", "exif_w", "exif_h", "exif_filetype",
            "ffprobe_err", "decision", "classification", "filepath"]
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    with open(CSV_OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow({k: r.get(k) for k in cols})

    with open(MD_OUT, "w") as f:
        f.write("# Quarantine — videos unreadable at thumbnail pre-gen\n\n")
        f.write("> **DO NOT delete any id listed here from iCloud until verified against the "
                "iCloud original on the MacBook.** This is the record the eventual deletion "
                "phase MUST exclude.\n\n")
        f.write(f"_Generated {stamp} · read-only forensics · {len(rows)} ids · source: {PREGEN_LOG}_\n\n")
        for r in rows:
            f.write(f"## id {r['id']} — {r['classification']}\n\n")
            f.write(f"- file: `{r['filepath']}`\n")
            f.write(f"- ext/date: {r['extension']} · {r['year']}-{r['month']}\n")
            f.write(f"- recorded (db): size={r['db_size_bytes']} B, dur={r['db_duration_s']} s, "
                    f"{r['db_w']}x{r['db_h']}\n")
            f.write(f"- on disk: {r['on_disk']}, size={r['disk_size_bytes']} B"
                    + (f", ≈{r['bytes_per_s']} B/s for its duration" if r['bytes_per_s'] else "") + "\n")
            f.write(f"- ffprobe: reads={r['ffprobe_reads']}, format={r['ffprobe_format']}, "
                    f"dur={r['ffprobe_dur_s']}; err: {r['ffprobe_err']}\n")
            f.write(f"- exiftool header: dur={r['exif_dur_s']} s, {r['exif_w']}x{r['exif_h']}, "
                    f"type={r['exif_filetype']}\n")
            f.write(f"- **decisions.db: {r['decision']}**\n\n")

    # console table
    print(f"\nfailed ids: {ids}\n")
    print(f"{'id':>6} {'ext':>4} {'on_disk':>8} {'disk_B':>12} {'B/s':>9} {'ffreads':>7} "
          f"{'exif_dur':>8} {'decision':>9}  classification")
    for r in rows:
        print(f"{r['id']:>6} {str(r['extension']):>4} {r['on_disk']:>8} "
              f"{str(r['disk_size_bytes']):>12} {str(r['bytes_per_s']):>9} {r['ffprobe_reads']:>7} "
              f"{str(r['exif_dur_s']):>8} {r['decision']:>9}  {r['classification']}")
    cut = [r['id'] for r in rows if r['decision'] == 'cut']
    print(f"\nartifacts: {CSV_OUT}\n           {MD_OUT}")
    print(f"already-CUT in decisions.db: {cut if cut else 'NONE'}")
    repull = [r['id'] for r in rows if ('TRUNCATED' in r['classification']
              or 'TOO SMALL' in r['classification'] or r['classification'].startswith('MISSING'))]
    print("re-pull from iCloud (missing / truncated / too-small):", repull)
    print("likely CORRUPT (plausible size but undecodable → verify on Mac, maybe cut):",
          [r['id'] for r in rows if 'CORRUPT' in r['classification']])
    print("UNKNOWN (verify vs iCloud):", [r['id'] for r in rows if r['classification'].startswith('UNKNOWN')])

if __name__ == "__main__":
    main()
