#!/usr/bin/env python3
"""
setup_status.py — the Loupe setup/status console ("the darkroom").

READ-ONLY, data-driven status of the ingest pipeline rendered as a darkroom
"developing" process. This module owns:

  * status_model()  — the JSON state contract the /setup page polls (~4s).
  * is_ready()      — the gate the front door (GET /) uses to redirect.
  * setup_page()    — the console HTML (Fraunces / Newsreader / JetBrains Mono,
                      the existing Loupe darkroom palette).

Discipline (non-negotiable for this feature):
  * metadata.db / apple-enrichment.db / faces.db / summaries.db are opened
    mode=ro. We never write any of them. No DB writes at all here.
  * The status endpoint is cheap and side-effect-free: cheap signals (DB COUNTs,
    thumb dir scandir, process liveness) are memoised for a few seconds; the one
    expensive signal (the recursive originals walk on the CIFS mount, which takes
    minutes) runs OFF the request path in a single-flight background daemon and
    is only read from cache. Nothing here spawns or triggers pipeline work.
  * Process liveness is read from /proc cmdlines directly — no subprocess, no
    pgrep, no sudo, so hitting the endpoint spawns zero processes.

The interactive Connect flow (iCloud sign-in / storage picker / AI keys) is a
LATER pass — rendered here per the mock but inert. See the brief.
"""

import json
import os
import sqlite3
import threading
import time

from loupe_common import ro as _ro   # shared read-only DB helper (paths come via init/_CFG)
from loupe_common import VIDEO_EXT   # one definition of what counts as a video
from loupe_common import APP_DATA as _APP_DATA

RUN_STATUS_DIR = os.path.join(_APP_DATA, "run-status")

# ---------------------------------------------------------------------------
# configuration — server.py calls init() once with the paths it already derives
# (so this module never re-derives env and can't drift from the app's roots).
# ---------------------------------------------------------------------------
_CFG = {
    "METADATA_DB": None,   # loupe-pipeline/metadata.db (DATA dir; assets)  mode=ro
    "THUMBS": None,        # culling/contactsheets/thumbs/{id}.jpg          dir
    "ENRICH_DB": None,     # apple-enrichment.db (apple_score, labels…)     mode=ro
    "FACES_DB": None,      # faces.db (faces, processed)                    mode=ro
    "NSFW_DB": None,       # nsfw.db (processed, scores) — derived next to FACES_DB  mode=ro
    "SUMMARIES_DB": None,  # summaries.db (summaries, clusters)             mode=ro
    "LIBRARY_ROOT": None,  # NAS root; originals expected under originals/
    "EXCLUDE_SQL": "1=1",  # work-product folder exclusion (matches app view)
    "SETTINGS_PATH": None, # loupe-settings.json (read-only here; library_source/root)
}

# Image extensions ingest+faces operate on (RAW/video skipped). Mirrors the
# pipeline: faces covers images AND non-Live-Photo videos (stage2b video-face import),
# so the denominator is that combined population, not the image count.
IMAGE_EXTS = ("HEIC", "HEIF", "JPG", "JPEG", "PNG", "GIF", "TIFF", "TIF")
MEDIA_EXTS = {".heic", ".heif", ".jpg", ".jpeg", ".png", ".mov", ".mp4",
              ".m4v", ".gif", ".tiff", ".tif", ".heics"}

CHEAP_TTL = 4.0       # cheap snapshot cache window (≈ the poll interval)
# The originals walk traverses the whole CIFS tree (minutes for ~90k files), so
# we refresh it sparingly and single-flight. The count only moves during an
# active pull; when idle it's static, and the stage status is inferred from the
# DB regardless, so a stale count never misleads.
ORIG_TTL = 3600.0     # background originals-walk refresh interval (1 hour).
# Measured ~10 min for a full traversal of this CIFS mount (per-entry stats are
# forced — readdir returns DT_UNKNOWN), so a 1-hour interval keeps NAS duty low;
# the count only moves during an active pull, where the live Develop rate is the
# real progress signal anyway.
RATE_WINDOW = 180     # seconds of recent commits used for ingest rate/ETA


def init(**cfg):
    """Called once from server.py with already-derived paths/constants."""
    _CFG.update({k: v for k, v in cfg.items() if v is not None})
    # nsfw.db sits beside faces.db (APP_DATA). Derived here so Stage 1 needs no server.py
    # change; server may later pass NSFW_DB explicitly (this never overwrites that).
    if _CFG.get("FACES_DB") and not _CFG.get("NSFW_DB"):
        _CFG["NSFW_DB"] = os.path.join(os.path.dirname(_CFG["FACES_DB"]), "nsfw.db")


def originals_root():
    return os.path.join(_CFG["LIBRARY_ROOT"], "originals")


def _library_source():
    """The user's saved library source ("existing"|"icloud"|None). READ-ONLY: opens
    loupe-settings.json read-only and never writes/seeds it (keeps this module
    side-effect-free). Returns None on any absence/parse error."""
    p = _CFG.get("SETTINGS_PATH")
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f).get("library_source")
    except (OSError, ValueError):
        return None


def _nsfw_enabled():
    """The owner's opt-in NSFW-screening flag (default False). READ-ONLY, same source as
    the other settings flags; never writes/seeds. The card gates disabled-vs-idle on this."""
    p = _CFG.get("SETTINGS_PATH")
    if not p or not os.path.exists(p):
        return False
    try:
        with open(p) as f:
            return bool(json.load(f).get("nsfw_enabled"))
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# process liveness — read /proc cmdlines directly (no subprocess / pgrep)
# ---------------------------------------------------------------------------
def _alive(*needles):
    """Return the matching cmdline string if any process command contains one of
    the needles, else None. Pure /proc read — spawns nothing."""
    try:
        pids = os.listdir("/proc")
    except OSError:
        return None
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        if not raw:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        for n in needles:
            if n in cmd:
                return cmd.strip()
    return None


# ---------------------------------------------------------------------------
# read-only DB helpers
# ---------------------------------------------------------------------------


def _scalar(conn, sql, params=()):
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _table_count(path, table):
    """COUNT(*) of a table in a read-only db; None if db/table absent."""
    if not path or not os.path.exists(path):
        return None
    try:
        c = _ro(path)
        try:
            return _scalar(c, "SELECT count(*) FROM %s" % table)
        finally:
            c.close()
    except sqlite3.Error:
        return None


def _scandir_count(path, suffix=".jpg"):
    """Cheap count of entries with a suffix (local SSD; ~90ms for 91k)."""
    if not path or not os.path.isdir(path):
        return None
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                # ".jpg" but NOT an in-flight video-thumb tmp ("<id>.tmp.jpg", which
                # gen_thumbs writes then os.replace's into place): counting those would
                # briefly inflate the prints total and flip "done" early. No-op for a
                # completed "<id>.jpg".
                if e.name.endswith(suffix) and not e.name.endswith(".tmp.jpg"):
                    n += 1
    except OSError:
        return None
    return n


# ---------------------------------------------------------------------------
# the one expensive signal: recursive originals walk on the CIFS mount.
# Minutes per pass — kept strictly OFF the request path. A single-flight daemon
# refreshes it at most every ORIG_TTL; the request only ever reads the cache.
# ---------------------------------------------------------------------------
_orig_lock = threading.Lock()
_orig = {"count": None, "ts": 0.0, "scanning": False, "ever": False}


def _walk_originals():
    # READDIR-ONLY recursion: we never os.stat() individual files. On CIFS a
    # stat is a per-file round trip (~90k of them = minutes); readdir is one
    # round trip per directory (~hundreds), so the count is cheap. Size doesn't
    # come from here at all — it's summed from metadata.db's file_size_bytes,
    # which is instant and needs no NAS I/O (see _compute_cheap).
    n = 0
    ok = False
    try:
        stack = [originals_root()]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                                continue
                        except OSError:
                            continue
                        if os.path.splitext(e.name)[1].lower() in MEDIA_EXTS:
                            n += 1
            except OSError:
                continue
        ok = True
    finally:
        # Always clear the in-flight flag, even on an unexpected error, so the
        # single-flight guard can never wedge the refresher permanently.
        with _orig_lock:
            if ok:
                _orig.update(count=n, ever=True)
            _orig.update(ts=time.time(), scanning=False)


def _kick_originals():
    """Start a background walk if the cache is stale and none is in flight.
    Returns immediately; never blocks the caller."""
    if not os.path.isdir(originals_root()):
        return
    with _orig_lock:
        fresh = _orig["ever"] and (time.time() - _orig["ts"] < ORIG_TTL)
        if _orig["scanning"] or fresh:
            return
        _orig["scanning"] = True
    t = threading.Thread(target=_walk_originals, name="loupe-originals-walk",
                         daemon=True)
    t.start()


def _originals_snapshot():
    """Last-known originals file count; kicks a background refresh if stale."""
    _kick_originals()
    with _orig_lock:
        return _orig["count"], _orig["ever"]


# ---------------------------------------------------------------------------
# cheap snapshot — DB counts + thumb count + liveness, memoised CHEAP_TTL secs
# ---------------------------------------------------------------------------
_cheap_lock = threading.Lock()
_cheap = {"ts": 0.0, "data": None}


def _compute_cheap():
    md = _CFG["METADATA_DB"]
    EX = _CFG["EXCLUDE_SQL"]
    d = {
        "assets_total": None, "library_total": None, "image_total": None,
        "face_total": None, "nonvideo_total": None,
        "library_bytes": None,
        "ingest_recent": 0, "ingest_max_ts": None,
        "thumbs": None, "apple_score": None, "labels": None, "persons": None,
        "faces_processed": None, "faces_rows": None,
        "nsfw_processed": None,
        "summaries": None, "clusters": None,
        "alive": {}, "meta_mtime": None,
    }
    # metadata.db — the spine. One connection, several cheap COUNTs.
    if md and os.path.exists(md):
        try:
            d["meta_mtime"] = os.path.getmtime(md)
        except OSError:
            pass
        try:
            c = _ro(md)
            try:
                d["assets_total"] = _scalar(c, "SELECT count(*) FROM assets")
                d["library_total"] = _scalar(
                    c, "SELECT count(*) FROM assets WHERE %s" % EX)
                qmarks = ",".join("'%s'" % e for e in IMAGE_EXTS)
                d["image_total"] = _scalar(
                    c, "SELECT count(*) FROM assets WHERE %s AND "
                       "upper(extension) IN (%s)" % (EX, qmarks))
                # The face pass covers images PLUS non-Live-Photo videos: the
                # stage2b import added 26,972 video assets to faces.db. The tray was
                # dividing that combined numerator by the images-only total and showing
                # 88,985 / 61,802 -- a bar reading 144% done. The comment above the
                # faces stage still said "faces runs on images only"; that stopped being
                # true when video faces landed.
                # nsfw_pipeline's own WHERE clause is "extension NOT IN (video)", so
                # that is its population -- not the image whitelist, which is 211 narrower
                # and made the tray read 100.3%. A progress bar that can exceed its own
                # total is telling you the two halves disagree about what is counted.
                vq = ",".join("'%s'" % e for e in sorted(VIDEO_EXT))
                d["nonvideo_total"] = _scalar(
                    c, "SELECT count(*) FROM assets WHERE %s AND "
                       "upper(extension) NOT IN (%s)" % (EX, vq))
                d["face_total"] = _scalar(
                    c, "SELECT count(*) FROM assets WHERE %s AND ("
                       "upper(extension) IN (%s) OR is_live_photo_video=0 OR "
                       "is_live_photo_video IS NULL)" % (EX, qmarks))
                now = int(time.time())
                d["ingest_recent"] = _scalar(
                    c, "SELECT count(*) FROM assets WHERE processed_at >= ?",
                    (now - RATE_WINDOW,)) or 0
                d["ingest_max_ts"] = _scalar(
                    c, "SELECT max(processed_at) FROM assets")
                # Total bytes straight from the DB — no NAS stat storm needed.
                # EXCLUDE_SQL so size refers to the same originals set the disk
                # walk counts (work-product folders aren't under originals/).
                d["library_bytes"] = _scalar(
                    c, "SELECT sum(file_size_bytes) FROM assets WHERE %s" % EX)
            finally:
                c.close()
        except sqlite3.Error:
            pass
    # thumbnails (local SSD)
    d["thumbs"] = _scandir_count(_CFG["THUMBS"], ".jpg")
    # apple enrichment
    d["apple_score"] = _table_count(_CFG["ENRICH_DB"], "apple_score")
    d["labels"] = _table_count(_CFG["ENRICH_DB"], "labels")
    d["persons"] = _table_count(_CFG["ENRICH_DB"], "persons")
    # faces
    d["faces_processed"] = _table_count(_CFG["FACES_DB"], "processed")
    d["faces_rows"] = _table_count(_CFG["FACES_DB"], "faces")
    # nsfw (optional, on-device) — resume/done bookkeeping count
    d["nsfw_processed"] = _table_count(_CFG.get("NSFW_DB"), "processed")
    # summaries / trips
    d["summaries"] = _table_count(_CFG["SUMMARIES_DB"], "summaries")
    d["clusters"] = _table_count(_CFG["SUMMARIES_DB"], "clusters")
    # process liveness (/proc reads only)
    d["alive"] = {
        "download": bool(_alive("icloudpd")),
        "ingest": bool(_alive("ingest.py")),
        "thumbs": bool(_alive("pregen.py", "gen_thumbs.py")),
        "enrich": bool(_alive("enrichment/build.py", "/build.py")),
        "faces": bool(_alive("faces_pipeline.py")),
        "nsfw": bool(_alive("nsfw_pipeline.py")),
        "summaries": bool(_alive("summaries.py")),
    }
    return d


def _cheap_snapshot():
    with _cheap_lock:
        if _cheap["data"] is not None and (time.time() - _cheap["ts"] < CHEAP_TTL):
            return _cheap["data"]
    data = _compute_cheap()      # computed outside the lock (DB/scandir IO)
    with _cheap_lock:
        _cheap["data"] = data
        _cheap["ts"] = time.time()
    return data


# ---------------------------------------------------------------------------
# stage derivation
# ---------------------------------------------------------------------------
def _ratio(done, total):
    if not total:
        return 0.0
    return (done or 0) / total


def _pct(done, total):
    return round(100 * _ratio(done, total), 1) if total else None


def _stage(id, phase, name, location, optional, status, detail,
           done=None, total=None, rate=None, eta=None, logline=None, error=None):
    last_run, running = _stage_timing(id)
    return {
        "id": id, "phase": phase, "name": name, "location": location,
        "optional": optional, "status": status,
        "progress": {"done": done, "total": total,
                     "rate_per_s": rate, "eta_seconds": eta},
        "detail": detail, "logline": logline, "error": error,
        "last_run": last_run, "running": running,   # 9.6: each tray shows when it last ran
    }


def _ingest_rate_eta(s, done, total):
    """rate (files/s) + ETA from RECENT commit timestamps, not the enqueue
    counter. Only meaningful while ingest is live."""
    recent = s.get("ingest_recent") or 0
    rate = recent / RATE_WINDOW if recent else None
    eta = None
    if rate and total and done is not None and total > done:
        eta = int((total - done) / rate)
    return (round(rate, 3) if rate else None), eta


def _marker(stage):
    """The durable run marker for a stage, or None.

    9.6 asks each tray for a last-run timestamp. The markers already carry started_at
    and finished_at; nothing was surfacing them, so every tray read as though it had
    never run. Read-only and never raises -- a status page that can 500 on a malformed
    marker is worse than one that omits a timestamp.
    """
    try:
        with open(os.path.join(RUN_STATUS_DIR, "%s.status.json" % stage)) as f:
            return json.load(f)
    except Exception:
        return None


def _stage_timing(stage):
    """(last_run_epoch, running) for a stage. last_run is finish time when finished,
    else start time, so a running stage still shows when it began."""
    m = _marker(stage)
    if not m:
        return None, False
    running = m.get("state") == "running"
    return (m.get("finished_at") or m.get("started_at")), running


def status_model():
    """The full JSON state contract (see brief). Cheap + side-effect-free."""
    s = _cheap_snapshot()
    o_count, o_ever = _originals_snapshot()
    o_size = s["library_bytes"]    # bytes from metadata.db (no NAS I/O)
    a = s["alive"]

    assets = s["assets_total"]
    library = s["library_total"]
    images = s["image_total"]
    thumbs = s["thumbs"]

    stages = []

    # ---- 01 Connect — iCloud sign-in (inert this pass). Honest derivation:
    #      originals on disk ⇒ a successful connect happened. The user may instead
    #      point Loupe at an existing on-disk library (Phase-1 skip): then Connect
    #      and Pull are SATISFIED, never "sign in to iCloud". -------------------
    src = _library_source()
    existing = (src == "existing")
    have_roll = (o_count or 0) > 0 or (assets or 0) > 0
    if a["download"]:
        st, detail = "running", "Authorized — pulling from iCloud."
    elif existing:
        st, detail = "done", "Using your existing library on disk."
    elif have_roll:
        st, detail = "done", "iCloud authorized; originals are on this machine."
    else:
        st, detail = "needs_you", "Sign in to iCloud to load the first roll."
    stages.append(_stage(
        "connect", "connect", "Connect", "mac", False, st, detail,
        logline=None if st != "needs_you" else "waiting for Apple ID"))

    # ---- 02 Pull — Load the roll (download originals to disk) ---------------
    if a["download"]:
        st = "running"
        detail = "Downloading originals from iCloud."
        logline = "icloudpd active"
    elif existing and not have_roll:
        # Pointed at an existing library, but nothing readable there yet — honest,
        # but framed as the existing-library path, never "connect and pull a roll".
        st = "needs_you"
        detail = "Point Loupe at a library that has originals/ on disk."
        logline = None
    elif have_roll:
        st = "done"
        if existing:
            base = "Using your existing library"
            if o_count is not None:
                detail = "%s — %s originals on disk%s." % (
                    base, f"{o_count:,}",
                    "" if o_size is None else " · %s" % _human_size(o_size))
            else:
                detail = "%s on disk." % base
        elif o_count is not None:
            detail = "%s originals on disk%s." % (
                f"{o_count:,}",
                "" if o_size is None else " · %s" % _human_size(o_size))
        elif not o_ever:
            detail = "Originals present (counting on the NAS…)."
        else:
            detail = "Originals present on disk."
        logline = None
    else:
        st = "blocked"
        detail = "No originals yet — connect and pull a roll first."
        logline = None
    stages.append(_stage(
        "load_roll", "pull", "Load the roll", "server", False, st, detail,
        done=o_count, total=None, logline=logline))

    # ---- 03 Process — Develop (ingest.py): assets vs files on disk ----------
    dev_total = o_count if (o_count and o_count >= (assets or 0)) else assets
    if a["ingest"]:
        st = "running"
        rate, eta = _ingest_rate_eta(s, assets, dev_total)
        detail = "Developing the negatives — extracting metadata."
        logline = "ingest.py · %s rows committed" % (f"{assets:,}" if assets else "0")
    else:
        rate = eta = None
        if not assets:
            st = "queued" if have_roll else "blocked"
            detail = ("Ready to develop once the roll is loaded."
                      if have_roll else "Waiting on originals.")
            logline = None
        elif o_count and assets < o_count * 0.995:
            st = "queued"
            detail = "Paused — %s of %s developed." % (f"{assets:,}", f"{o_count:,}")
            logline = "ingest not running"
        else:
            st = "done"
            detail = "%s assets developed." % (f"{assets:,}" if assets else "0")
            logline = None
    stages.append(_stage(
        "develop", "process", "Develop", "server", False, st, detail,
        done=assets, total=dev_total, rate=rate, eta=eta, logline=logline))

    # ---- 03 Process — Make contact prints (thumbnails) ---------------------
    ct_total = library or assets
    if a["thumbs"]:
        st = "running"
        detail = "Printing contact sheets — %s of %s." % (
            f"{thumbs:,}" if thumbs else "0", f"{ct_total:,}" if ct_total else "?")
        logline = "thumbnailer active"
    elif thumbs is None:
        st, detail, logline = "unknown", "Thumbnail cache not found.", None
    elif ct_total and thumbs >= ct_total * 0.995:
        st, detail, logline = "done", "%s contact prints made." % f"{thumbs:,}", None
    elif thumbs == 0:
        st = "queued" if assets else "blocked"
        detail = "No prints yet."
        logline = None
    else:
        st = "queued"
        detail = "Paused — %s of %s printed." % (
            f"{thumbs:,}", f"{ct_total:,}" if ct_total else "?")
        logline = "thumbnailer not running"
    stages.append(_stage(
        "contact_prints", "process", "Make contact prints", "server", False,
        st, detail, done=thumbs, total=ct_total, logline=logline))

    # ---- 03 Process — Read the negatives (Apple enrichment, optional) ------
    score = s["apple_score"]
    enrich_present = _CFG["ENRICH_DB"] and os.path.exists(_CFG["ENRICH_DB"])
    if a["enrich"]:
        st = "running"
        detail = "Reading the negatives — Apple scene labels & scores."
        logline = "enrichment/build.py active"
    elif not enrich_present or not score:
        st = "needs_you"
        detail = ("Optional — bring the Mac's Photos data over to read scene "
                  "labels, scores and people.")
        logline = "no Apple-extracted inputs yet"
    else:
        st = "done"
        cov = _pct(score, library)
        detail = "Apple data read for %s assets%s." % (
            f"{score:,}", "" if cov is None else " (%s%% coverage)" % cov)
        logline = None
    stages.append(_stage(
        "negatives", "process", "Read the negatives", "mac", True, st, detail,
        done=score, total=library, logline=logline))

    # ---- 03 Process — Spot the faces (optional; images-only denominator) ----
    fp = s["faces_processed"]
    faces_total = s.get("face_total") or images
    if a["faces"]:
        st = "running"
        rate = None
        detail = "Spotting faces — %s of %s frames." % (
            f"{fp:,}" if fp else "0", f"{faces_total:,}" if faces_total else "?")
        logline = "faces_pipeline.py active · %s faces found" % (
            f"{s['faces_rows']:,}" if s["faces_rows"] else "0")
    elif fp is None:
        st, detail, logline = "needs_you", \
            "Optional — run the face pass to group people.", None
    elif faces_total and fp >= faces_total * 0.995:
        st = "done"
        detail = "%s faces found across %s images." % (
            f"{s['faces_rows']:,}" if s["faces_rows"] else "0", f"{fp:,}")
        logline = None
    elif fp == 0:
        st, detail, logline = "queued", "Face pass not started.", None
    else:
        st = "queued"
        detail = "Paused — %s of %s images scanned." % (
            f"{fp:,}", f"{faces_total:,}" if faces_total else "?")
        logline = "faces not running"
    stages.append(_stage(
        "faces", "process", "Spot the faces", "server", True, st, detail,
        done=fp, total=faces_total, logline=logline))

    # ---- 03 Process — Screen for nudity (optional; on-device; images-only denominator) ----
    nf = s["nsfw_processed"]
    nsfw_total = s.get("nonvideo_total") or images
    if a["nsfw"]:
        st = "running"
        detail = "Screening on-device — %s of %s frames." % (
            f"{nf:,}" if nf else "0", f"{nsfw_total:,}" if nsfw_total else "?")
        logline = "nsfw_pipeline.py active (on-device)"
    elif nf is None:
        st, detail, logline = "needs_you", \
            "Optional — run the on-device nudity screen.", None
    elif nsfw_total and nf >= nsfw_total * 0.995:
        st = "done"
        detail = "%s frames screened on-device." % f"{nf:,}"
        logline = None
    elif nf == 0:
        st, detail, logline = "queued", "Nudity screen not started.", None
    else:
        st = "queued"
        detail = "Paused — %s of %s frames screened." % (
            f"{nf:,}", f"{nsfw_total:,}" if nsfw_total else "?")
        logline = "nsfw not running"
    nsfw_stage = _stage(
        "nsfw", "process", "Screen for nudity", "server", True, st, detail,
        done=nf, total=nsfw_total, logline=logline)
    nsfw_stage["enabled"] = _nsfw_enabled()   # opt-in flag the card gates disabled/idle on
    stages.append(nsfw_stage)

    # ---- 03 Process — Sort the contact sheets (summaries / trips) ----------
    summ = s["summaries"]
    clus = s["clusters"]
    if a["summaries"]:
        st, detail, logline = "running", "Sorting the contact sheets into trips.", \
            "summaries.py active"
    elif summ is None:
        st, detail, logline = "unknown", "Summaries store not found.", None
    elif summ > 0:
        st = "done"
        detail = "%s trips/periods sorted%s." % (
            f"{summ:,}", "" if not clus else " · %s places" % f"{clus:,}")
        logline = None
    else:
        st, detail, logline = "queued", "Sorted on demand at first view.", None
    stages.append(_stage(
        "sort_sheets", "process", "Sort the contact sheets", "server", True,
        st, detail, done=summ, logline=logline))

    # ---- 04 Finish — Prints are dry (derived: Develop AND contact prints) ---
    by_id = {x["id"]: x for x in stages}
    develop_done = by_id["develop"]["status"] == "done"
    prints_done = by_id["contact_prints"]["status"] == "done"
    if develop_done and prints_done:
        st, detail, logline = "done", "Prints are dry — your library is ready.", None
    elif by_id["develop"]["status"] == "running" or \
            by_id["contact_prints"]["status"] == "running":
        st, detail, logline = "running", "Drying — developing and printing in progress.", None
    else:
        st, detail, logline = "blocked", \
            "Dries once Develop and contact prints finish.", None
    stages.append(_stage(
        "dry", "finish", "Prints are dry", "server", False, st, detail,
        logline=logline))

    # ---- overall ----
    # The front-door gate should unlock once the library spine is developed.
    # Contact prints can keep drying in the setup console without locking the
    # owner out of an otherwise usable library.
    ready = develop_done
    done_n = sum(1 for x in stages if x["status"] == "done")
    active_phase = "finish"
    for x in stages:
        if x["status"] in ("running", "needs_you", "queued", "blocked", "error"):
            active_phase = x["phase"]
            break

    return {
        "library": {
            "originals_present": o_count,
            "total_expected": None,    # unknown until the iCloud manifest exists
            "size_bytes": o_size,
        },
        "overall": {
            "ready": ready,
            "stages_done": done_n,
            "stages_total": len(stages),
            "active_phase": active_phase,
        },
        "stages": stages,
        "activity": _activity(stages),
        "ledger": ledger_status(),
        "generated_at": int(time.time()),
    }


def ledger_status():
    """Backup health, as a number you can look at (P15's ledger room).

    This exists because of a specific failure: between 2026-08-06 and 2026-08-09 the
    ledger timer produced no NAS snapshot at all -- two scheduled runs died on a missing
    sqlite-vec extension, and the only apparent success wrote to /tmp. `systemctl status`
    reported SUCCESS throughout, because it reflected that manual run. Three days of no
    backup, and every surface anyone would think to check said fine.

    So this reports the one thing that cannot lie: the newest file actually present in
    each destination, and how old it is. Exit codes and unit states are deliberately not
    consulted.

    Read-only, and never raises -- a status panel that 500s on a missing mount is worse
    than one that says the mount is missing.
    """
    import glob

    out = {"nas": None, "offhost": None, "restore_runbook":
           "loupe-vault/runbooks/ledger-restore.md"}
    now = time.time()

    def newest(pattern):
        try:
            files = glob.glob(pattern)
            if not files:
                return None
            f = max(files, key=os.path.getmtime)
            st = os.stat(f)
            return {"name": os.path.basename(f), "bytes": st.st_size,
                    "age_hours": round((now - st.st_mtime) / 3600.0, 1),
                    "at": int(st.st_mtime)}
        except OSError:
            return None

    out["nas"] = newest(os.path.expanduser(
        os.environ.get("LOUPE_LEDGER_DIR", "/home/david/loupe-archive/loupe-ledger")) + "/ledger-*.tar.zst")
    # the off-host mirror is a directory per run, not a file
    try:
        root = os.path.expanduser("~/FleetDatabaseBackups/charlie-snapshots")
        dirs = [d for d in glob.glob(root + "/2*") if os.path.isdir(d)]
        if dirs:
            d = max(dirs, key=os.path.getmtime)
            out["offhost"] = {"name": os.path.basename(d),
                              "age_hours": round((now - os.path.getmtime(d)) / 3600.0, 1),
                              "at": int(os.path.getmtime(d)),
                              "files": len(os.listdir(d))}
    except OSError:
        pass

    # 36h: a daily timer that has produced nothing in a day and a half has failed,
    # whatever anything else claims.
    limit = float(os.environ.get("LOUPE_LEDGER_MAX_AGE_H", "36"))
    ages = [x["age_hours"] for x in (out["nas"], out["offhost"]) if x]
    out["max_age_hours"] = limit
    out["stale"] = (not ages) or any(a >= limit for a in ages)
    return out


def _activity(stages):
    """The one-line answer to "is anything happening right now?" (9.6).

    Derived from the stages already built rather than recomputed, so the line and the
    rail beneath it can never disagree -- a header claiming a stage is running while its
    tray reads idle is worse than no header.

    `running` comes from the durable marker, `status` from the console's own view of the
    data. Either may lead: a stage can be mid-run before its counts move, and a marker
    can outlive the process that wrote it. Taking either as live is the honest read.
    """
    for st in stages:
        if not (st.get("running") or st.get("status") == "running"):
            continue
        p = st.get("progress") or {}
        return {
            "stage": st.get("id"),
            "name": st.get("name"),
            "done": p.get("done"),
            "total": p.get("total"),
            "rate_per_s": p.get("rate_per_s"),
            "eta_seconds": p.get("eta_seconds"),
            "since": st.get("last_run"),
        }
    return None


def is_ready():
    """Front-door gate: the developed library spine exists."""
    try:
        return bool(status_model()["overall"]["ready"])
    except Exception:
        # Never let a status hiccup lock the user out of the app.
        return True


def _human_size(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f%s" if unit in ("B", "KB") else "%.1f%s") % (n, unit)
        n /= 1024.0


# ---------------------------------------------------------------------------
# the console page (HTML lives in setup_page.py to keep this file focused)
# ---------------------------------------------------------------------------
def setup_page():
    from setup_page import render
    return render(json.dumps(status_model()))
