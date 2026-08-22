#!/usr/bin/env python3
"""
server.py — merged photo-cull tool: whole-library base + a Candidates filtered
view, backed by ONE unified decision store. Supersedes the two sibling apps
(review :8000 + librarycull :8001) — runs on :8000, fronted by your own tunnel/host.

Two views over the SAME months/weeks/days and the SAME decision store:
  * Library  — app-visible = all cataloged assets (102,614 as of 2026-07-04);
               the former production/ + long-video-elsewhere/ path exclusion
               was neutralized (FLEET-WORKER1-BUILD-20260704-app-scope-all).
  * Candidates — filtered to the 24,130 rule-flagged subset (reused from the v2
               candidates loader), with rule badges / per-rule chips / fp-rescue.
A cut is a cut regardless of view — one store keyed by assets.id.

IA: Overview (years→months) → Month (weeks→days) → Day (grid → focus).

DECISION-CAPTURE ONLY. Writes go to exactly three places:
  * $DATA_ROOT/culling/contactsheets/thumbs/                  (SHARED thumb cache)
  * decisions.db                                               (UNIFIED sqlite store)
  * culling/library-delete-YYYY-MM.csv + culling/candidates-delete.csv  (exports)
metadata.db is opened READ-ONLY. Never deletes/moves/alters any photo.
No deletion or move is wired anywhere — only CSV export.

States: undecided (no row) / keep / cut. Sweep "rest of day" writes keep.
% reviewed = non-undecided / total. Only 'cut' ids reach an export.

Run:  python3 server.py [port]   (default 8000)
"""

import bisect
import email.utils
import hashlib
import hmac
import io
import json
import mimetypes
import os
import random
import re as _re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from PIL import Image, ImageOps, UnidentifiedImageError
import pillow_heif

try:
    import rawpy
    RAW_SUPPORTED = True
except ImportError:
    RAW_SUPPORTED = False
pillow_heif.register_heif_opener()

import setup_status   # /setup darkroom console — read-only pipeline status
import icloud_connect # Connect 2a — iCloud auth handshake (sign-in + 2FA), no download
import run_control    # /setup compute-stage triggers (detached, single-flighted, resumable)
from loupe_common import (HERE, V2, APP_DATA, METADATA_DB, PIPELINE_DIR, EXCLUDE_SQL,
                          VIDEO_EXT, ro)   # shared roots + read-only DB helper

# --- paths (sibling app; shared read-only metadata + shared thumb cache) ------
# Portable roots — env-unset reproduces the historical on-disk layout (see ONBOARDING.md):
#   LIBRARY_ROOT — read-only source tree of original media (e.g. the NAS mount).
#   DATA_ROOT    — all generated DATA (metadata.db, thumb cache, exports, vendor/, loupe's
#                  own dbs/caches). Unset keeps the historical split: pipeline DATA in the
#                  sibling loupe-pipeline dir; loupe's own stores beside this file. Pipeline
#                  SOURCE is separate — it lives in the pipeline/ subtree (loupe_common.PIPELINE_DIR).
# HERE / V2 / APP_DATA / METADATA_DB / PIPELINE_DIR now come from loupe_common (imported above).
LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", os.path.join(os.sep, "mnt", "nas2", "photos"))
THUMBS = os.path.join(V2, "culling", "contactsheets", "thumbs")
EXPORT_DIR = os.path.join(V2, "culling")
DECISIONS_DB = os.path.join(APP_DATA, "decisions.db")   # UNIFIED store (both views)
os.makedirs(THUMBS, exist_ok=True)

# Apple enrichment (read-only): UUID bridge + scene/object labels + persons +
# aesthetic score, keyed to metadata.db assets.id. 77,684 of 102,614 (2026-07-16) assets have it;
# the rest render project-side facts only. Loupe LEFT-JOINs at query time.
ENRICH_DB = os.path.join(APP_DATA, "apple-enrichment.db")
NSFW_DB = os.path.join(APP_DATA, "nsfw.db")     # on-device nudity scores (read-only here)
PAIRS_DB = os.path.join(APP_DATA, "pairs.db")   # loupe's OWN rw store for Live Photo pairs
# Loupe's OWN settings (never the read-only dbs). Protected-people guard list etc.
SETTINGS_PATH = os.path.join(APP_DATA, "loupe-settings.json")
# Fallback protected-people list, used only when loupe-settings.json has none yet.
# Ships EMPTY for publish-safety — real names live in the gitignored loupe-settings.json
# (edit via /settings). An existing install keeps its list there, so this stays unused.
DEFAULT_PROTECTED = []

PORT = 8000

# EXCLUDE_SQL (work-product folder exclusion) imported from loupe_common.

# Hand the setup console its read-only paths (it never re-derives env, so it
# can't drift from the roots resolved above). faces.db / summaries.db live with
# loupe's own stores under APP_DATA.
setup_status.init(
    METADATA_DB=METADATA_DB, THUMBS=THUMBS, ENRICH_DB=ENRICH_DB,
    FACES_DB=os.path.join(APP_DATA, "faces.db"),
    SUMMARIES_DB=os.path.join(APP_DATA, "summaries.db"),
    LIBRARY_ROOT=LIBRARY_ROOT, EXCLUDE_SQL=EXCLUDE_SQL,
    SETTINGS_PATH=SETTINGS_PATH)

# Connect 2a: iCloud auth session cookie lives in a Loupe-controlled dir (files 0600).
# Established by the LAN-gated /api/connect/* handshake; reused by Phase 2b's pull.
ICLOUD_COOKIE_DIR = os.path.join(APP_DATA, "icloud-session")
icloud_connect.configure(ICLOUD_COOKIE_DIR)

# VIDEO_EXT imported from loupe_common (frozenset of the 8 video extensions).
LONG_EDGE = 400
# Thumbnail concurrency. Image DECODE is the memory-bound resource (48 MP HEICs
# ~146 MB each); cap it hard or the box OOM-kills. Videos are light (single extracted
# frame). Pool sized to videos; images gated by a semaphore. Host-aware defaults,
# anchored so a 4-core / ~7.6 GB box keeps the historical conservative values
# (IMG=12 OOM guard, VID=16); override either via the IMG_WORKERS / VID_WORKERS env.
def _envint(name, default):
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default

def _ram_gb():
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3)
    except Exception:
        return 8.0

_CORES = os.cpu_count() or 4
_RAM_GB = _ram_gb()
# OOM guard stays conservative on small boxes (≤12 GB → 12); larger RAM scales up, capped.
IMG_WORKERS = _envint("IMG_WORKERS", 12 if _RAM_GB < 12 else min(48, int(_RAM_GB)))
VID_WORKERS = _envint("VID_WORKERS", max(8, min(_CORES * 4, 64)))

# Full-res originals stream to LOCAL access only (LAN / SSH tunnel). Over the
# public Cloudflare host /api/full returns 403; only cached thumbs are served.
FULL_RES_LOCAL_ONLY = True
_PRIVATE = _re.compile(r"^(127\.|10\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1$)")

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
STATE_ABBR = {
 "Alabama":"AL","Alaska":"AK","Arizona":"AZ","Arkansas":"AR","California":"CA",
 "Colorado":"CO","Connecticut":"CT","Delaware":"DE","Florida":"FL","Georgia":"GA",
 "Hawaii":"HI","Idaho":"ID","Illinois":"IL","Indiana":"IN","Iowa":"IA","Kansas":"KS",
 "Kentucky":"KY","Louisiana":"LA","Maine":"ME","Maryland":"MD","Massachusetts":"MA",
 "Michigan":"MI","Minnesota":"MN","Mississippi":"MS","Missouri":"MO","Montana":"MT",
 "Nebraska":"NE","Nevada":"NV","New Hampshire":"NH","New Jersey":"NJ","New Mexico":"NM",
 "New York":"NY","North Carolina":"NC","North Dakota":"ND","Ohio":"OH","Oklahoma":"OK",
 "Oregon":"OR","Pennsylvania":"PA","Rhode Island":"RI","South Carolina":"SC",
 "South Dakota":"SD","Tennessee":"TN","Texas":"TX","Utah":"UT","Vermont":"VT",
 "Virginia":"VA","Washington":"WA","West Virginia":"WV","Wisconsin":"WI","Wyoming":"WY"}

WK_LABELS = {0: "1–7", 1: "8–14", 2: "15–21", 3: "22–28", 4: "29–31", 9: "no specific day"}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ---------------------------------------------------------------------------
# read-only metadata access (fresh connection per call — many concurrent readers)
# ---------------------------------------------------------------------------
def _ro():
    return ro(METADATA_DB)   # loupe_common.ro; keeps the no-arg call sites unchanged


def _enr():
    """Read-only Apple-enrichment connection (fresh per call; many readers)."""
    c = sqlite3.connect(f"file:{ENRICH_DB}?mode=ro", uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _pct(sorted_arr, v):
    """Library-relative percentile (0–100) of v within a presorted list; None if N/A."""
    if v is None or not sorted_arr:
        return None
    return round(100 * bisect.bisect_left(sorted_arr, v) / len(sorted_arr))


def enrichment(idv, blur=None):
    """Per-frame Apple signal set for the focus panel. None-safe: returns a dict
    with apple=False and empty groups when an asset has no enrichment, so the
    panel always renders. blur (project-side) feeds the sharpness bar regardless."""
    apple = idv in ENR_HAS
    ov = APPLE_SCORE.get(idv)
    aesthetic = ({"value": round(ov, 3), "pct": _pct(APPLE_SORTED, ov)}
                 if ov is not None else None)
    sharp = ({"value": round(blur), "pct": _pct(BLUR_SORTED, blur)}
             if blur is not None else None)
    labels, persons = [], []
    if apple:
        try:
            c = _enr()
            labels = [{"cat": r["category"], "term": r["term"],
                       "score": (round(r["score"], 2) if r["score"] is not None else None)}
                      for r in c.execute(
                          "SELECT category, term, score FROM labels WHERE asset_id=? "
                          "ORDER BY (score IS NULL), score DESC", (idv,))]
            persons = [r[0] for r in c.execute(
                "SELECT DISTINCT person FROM persons WHERE asset_id=?", (idv,))]
            c.close()
        except Exception:
            pass

    def grp(cat, n):
        return [{"term": l["term"], "score": l["score"]}
                for l in labels if l["cat"] == cat][:n]

    content = {
        "scene": [{"term": l["term"], "score": l["score"]}
                  for l in labels if l["cat"] in ("scene", "action")][:6],
        "food": grp("food", 4), "landmark": grp("landmark", 4),
        "species": grp("species", 4), "document": grp("document", 4),
        "ocr_count": sum(1 for l in labels if l["cat"] == "ocr"),
    }
    return {
        "apple": apple,
        "scores": {"aesthetic": aesthetic, "sharpness": sharp},
        "content": content,
        "screenshot": idv in SD_IDS,
        "persons": [{"name": p, "protected": p in PROTECTED_NAMES} for p in persons],
    }


def bucket_of(year, month):
    if year is None:
        return "undated"
    if month is None:
        return f"{year:04d}-00"
    return f"{year:04d}-{month:02d}"


def day_of(year, month, ts):
    """Day-of-month from capture_timestamp, but only when it agrees with the
    (year,month) the item is bucketed under; otherwise 'no specific day'."""
    if ts is None:
        return 0
    lt = time.localtime(ts)
    if year and month and lt.tm_year == year and lt.tm_mon == month:
        return lt.tm_mday
    return 0


def day_of_year(ts):
    """(month, day) from capture_timestamp in local time, or None if undated.
    Feb 29 is left as-is -- only leap years ever contribute to that key, which is
    exactly what "on this day across every year" should show."""
    if ts is None:
        return None
    lt = time.localtime(ts)
    return (lt.tm_mon, lt.tm_mday)


def mmdd(m, d):
    return f"{m:02d}-{d:02d}"


# ---------------------------------------------------------------------------
# candidate subset — the 24,130 rule-flagged rows, reused from the v2 loader.
# (candidates.py also backs gen_thumbs.py.) Powers the Candidates view.
# Source now lives under pipeline/ (PIPELINE_DIR); its data (metadata.db, thumbs,
# culling CSVs) stays in V2. candidates.py resolves those from $DATA_ROOT, else its
# OWN dir — so pin DATA_ROOT=V2 across the import (NOT globally: APP_DATA also keys on
# DATA_ROOT, so a persistent set would relocate loupe's own stores). Restore right after.
# ---------------------------------------------------------------------------
_prev_data_root = os.environ.get("DATA_ROOT")
os.environ["DATA_ROOT"] = V2
sys.path.insert(0, PIPELINE_DIR)
import candidates as _C
if _prev_data_root is None:
    os.environ.pop("DATA_ROOT", None)
else:
    os.environ["DATA_ROOT"] = _prev_data_root
_csets, _citems, CAND = _C.load_all()      # CAND: id -> {rules, m, year, month, ts, dur, ...}
CAND_IDS = set(CAND)
RULE_PRIORITY = _C.RULE_PRIORITY

# --- Cutting Room snapshot: per-id size + the clean 6-rule order, captured NOW,
# before CAND gets the SD/PB merges and the hidden-mov prune. The page reports the
# raw load_all() universe (the six rules) — never double-counting a frame's bytes.
CR_SIZE = {i: CAND[i].get("size", 0) or 0 for i in CAND}     # by_id sizes at load time
CR_ORDER = list(_C.RULE_PRIORITY)                            # ["B4","B3","B2","A2b","A2a","B5"]
# Per-rule copy — titles + confidence per the brief; explanation / THE RULE / WATCH FOR
# grounded in the candidates.py SETS descriptions. One human voice, reversible framing.
CR_COPY = {
 "B4":  {"title": "Soft & blurry", "conf": "Your call",
         "explain": "Frames that came out soft — low sharpness across the whole frame, with a texture guard so deliberately minimal shots aren't swept in.",
         "rule": "Global sharpness below the 10th percentile (Laplacian < 93.6), low-texture-guarded.",
         "watch": "Intentionally soft or minimalist shots — the fp-suspect frames are surfaced first."},
 "B3":  {"title": "Burst extras", "conf": "Your call",
         "explain": "The also-rans of a burst — the 4th-sharpest frame onward in a run of near-identical shots taken within five seconds of each other. The three sharpest are always kept.",
         "rule": "Rank 4+ by sharpness within a burst; the 3 sharpest are kept, so a burst of 3 flags nothing.",
         "watch": "Bursts where you'd happily keep more than three frames."},
 "B2":  {"title": "Screenshots", "conf": "High confidence",
         "explain": "PNGs saved at iPhone screen sizes — screenshots, not photographs.",
         "rule": "PNG at known iPhone screen resolutions.",
         "watch": "Screenshots you're keeping on purpose."},
 "A2b": {"title": "Short clips · 1–3s", "conf": "Review",
         "explain": "Lone one-to-three-second clips with no paired photo — short, but often intentional, so they're held for a look.",
         "rule": "Standalone MOV, 1–3s, with no still partner.",
         "watch": "Deliberate short videos worth keeping."},
 "A2a": {"title": "Accidental taps · <1s", "conf": "High confidence",
         "explain": "Sub-second lone clips — the accidental taps of the record button.",
         "rule": "Standalone MOV under one second.",
         "watch": "(rare) a real instant caught in under a second."},
 "B5":  {"title": "Junk imports", "conf": "Your call",
         "explain": "Files with no camera fingerprint at all — no EXIF, no GPS, no make or model. Usually saved images and downloads, not your own photos.",
         "rule": "No EXIF, no GPS, no camera make + model.",
         "watch": "Stripped-metadata photos you actually took."},
 "NSFW": {"title": "Closed Set", "conf": "On-device · owner only",
         "explain": "Frames the on-device screen flagged as possible nudity, above your current threshold. Private to you — never shown to shared viewers, never deleted; clear any false positive from your review.",
         "rule": "NudeNet max score ≥ your threshold (re-thresholdable, no rescan).",
         "watch": "False positives — clear them to remove the frame from the closed set."},
 "PROD": {"title": "Production · work product", "conf": "Work product · owner only",
         "explain": "Videos under production/ — finished work product, held aside from normal review. Private to you: never shown to shared viewers, never entered into a cut batch. A DISTINCT facet from the nudity screen — no threshold, nothing flagged, nothing to clear; just held aside.",
         "rule": "Path under production/ (the work-product subtree).",
         "watch": "Nothing to action — this pile is held aside, not a cull queue."},
}
import summaries as SUM   # AI-polished period summaries (read-only metadata.db; keys from env)
CAND_BUCKET_TOTAL = defaultdict(int)        # candidate count per month-bucket
for _cid, _cit in CAND.items():
    CAND_BUCKET_TOTAL[bucket_of(_cit["year"], _cit["month"])] += 1
print(f"candidates: {len(CAND_IDS)} rule-flagged across "
      f"{len(CAND_BUCKET_TOTAL)} month-buckets", flush=True)


# ---------------------------------------------------------------------------
# Apple enrichment — startup load. All read-only. Builds:
#   * library-relative distributions for the percentile bars (aesthetic, sharpness)
#   * per-id aesthetic score + has-enrichment set (for tile pips / worst-first sort)
#   * protected-people id set (the bulk-cut guard)
#   * the "screenshots & documents" (SD) candidate category, merged into CAND
# Degrades gracefully if the enrichment db is absent.
# ---------------------------------------------------------------------------
APPLE_SCORE = {}          # asset_id -> overall (0–1)
APPLE_SORTED = []         # sorted overall values (percentile bar)
BLUR_SORTED = []          # sorted blur_laplacian over the library (sharpness bar)
ENR_HAS = set()           # asset_ids that carry ANY Apple enrichment (in the UUID bridge)
PROTECTED_NAMES = []      # loupe-settings protected-people list
PROTECTED_IDS = set()     # asset_ids containing >=1 protected person
SD_IDS = set()            # screenshots & documents (labels nominate)


def _load_settings():
    """Loupe's own settings (protected-people list). Created with defaults if absent."""
    try:
        with open(SETTINGS_PATH) as f:
            s = json.load(f)
    except Exception:
        s = {}
    if not s.get("protected_people"):
        s["protected_people"] = list(DEFAULT_PROTECTED)
        try:
            with open(SETTINGS_PATH, "w") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            print(f"settings: could not write {SETTINGS_PATH}: {e}", flush=True)
    return s


PROTECTED_NAMES = _load_settings()["protected_people"]

# --- settings store: Residences (durable, loupe-owned; lives in loupe-settings.json, NOT a db) ---
_settings_lock = threading.Lock()


def _earliest_month():
    try:
        c = _ro(); m = c.execute(
            "SELECT strftime('%Y-%m', min(capture_timestamp),'unixepoch','localtime') "
            "FROM assets WHERE capture_timestamp>0").fetchone()[0]; c.close()
        return m or "2002-10"
    except Exception:
        return "2002-10"


def _seed_residences():
    """Residence seed for a FRESH install. Ships EMPTY for publish-safety — real residences
    live in the gitignored loupe-settings.json (configure via /settings). load_residences
    seeds this only when the store is absent and never overwrites user edits, so an existing
    install is unaffected. Each residence is
    {id,label,areas:[\"City, ST\"],radius_km,start:\"YYYY-MM\",end,order,color}."""
    return []


def load_residences():
    """Residence list from the store; seeded ONCE if absent (never overwrites user edits)."""
    with _settings_lock:
        try:
            s = json.load(open(SETTINGS_PATH))
        except Exception:
            s = {}
        if "residences" not in s:
            s["residences"] = _seed_residences()
            try:
                json.dump(s, open(SETTINGS_PATH, "w"), indent=2)
            except Exception as e:
                print(f"settings: could not seed residences: {e}", flush=True)
        return s["residences"]


def save_residences(reslist):
    """Persist residences and re-derive home/away (clears places caches; trips + map + bursts
    recompute on next access — the recompute-on-next-load path)."""
    with _settings_lock:
        try:
            s = json.load(open(SETTINGS_PATH))
        except Exception:
            s = {}
        s["residences"] = reslist
        json.dump(s, open(SETTINGS_PATH, "w"), indent=2)
    PLACES.set_residences(reslist)


# --- settings store: library source + root (Connect Phase 1; loupe-owned) -------
# {"library_source": "existing"|"icloud", "library_root": "/abs/path"}. Written ONLY
# by the LAN-gated /api/setup/library endpoint. resolve_library_root() is provided for
# LATER phases; existing consumers keep using the module-level LIBRARY_ROOT (env) for now.
def load_library_choice():
    """The saved library source/root, if any. Read-only; never seeds or writes."""
    with _settings_lock:
        try:
            s = json.load(open(SETTINGS_PATH))
        except Exception:
            s = {}
    return {"library_source": s.get("library_source"),
            "library_root": s.get("library_root")}


def save_library_choice(source, root):
    """Persist library source + root, mirroring save_residences' safe read-modify-write
    (whole-object merge under _settings_lock). Writes ONLY loupe-settings.json."""
    with _settings_lock:
        try:
            s = json.load(open(SETTINGS_PATH))
        except Exception:
            s = {}
        s["library_source"] = source
        s["library_root"] = root
        json.dump(s, open(SETTINGS_PATH, "w"), indent=2)


def save_nsfw_enabled(enabled):
    """Persist the opt-in on-device NSFW-screening flag, mirroring save_library_choice's
    safe read-modify-write (whole-object merge under _settings_lock). Writes ONLY
    loupe-settings.json and never touches other keys."""
    with _settings_lock:
        try:
            s = json.load(open(SETTINGS_PATH))
        except Exception:
            s = {}
        s["nsfw_enabled"] = bool(enabled)
        json.dump(s, open(SETTINGS_PATH, "w"), indent=2)
    return bool(enabled)


def load_write_token():
    """P2.2 (W23) shared write token from the gitignored loupe-settings.json.

    Absent/blank/non-string => the write gate stays INERT and LAN-trust behaviour is
    exactly what it was before. Arming and disarming is therefore a one-key settings
    edit, with no code change and no restart (this is read per request)."""
    with _settings_lock:
        try:
            s = json.load(open(SETTINGS_PATH))
        except Exception:
            s = {}
    t = s.get("write_token")
    return t.strip() if isinstance(t, str) else ""


def resolve_library_root():
    """Library root for LATER phases: saved choice → LIBRARY_ROOT env → default.
    Intentionally NOT wired into existing consumers in this phase."""
    saved = load_library_choice().get("library_root")
    if saved:
        return saved
    return LIBRARY_ROOT


def validate_library_path(root):
    """Read-only check that `root` is a usable existing library. Returns (ok, error).
    Creates/relocates/processes NOTHING — it only stats. Mirrors the pipeline's mount
    guard: when LOUPE_REQUIRE_MOUNT is set, the sentinel (MOUNT_SENTINEL env, else
    <root>/originals/.mounted) must be present so we don't point at an unmounted NAS."""
    if not root or not isinstance(root, str):
        return False, "Choose where your library lives."
    if not os.path.isabs(root):
        return False, "Use an absolute path (starting with /)."
    if not os.path.isdir(root) or not os.access(root, os.R_OK | os.X_OK):
        return False, "That folder doesn't exist or isn't readable."
    originals = os.path.join(root, "originals")
    if not os.path.isdir(originals) or not os.access(originals, os.R_OK | os.X_OK):
        return False, "No readable originals/ folder there — that's not a Loupe library."
    require_mount = os.environ.get("LOUPE_REQUIRE_MOUNT", "") not in ("", "0", "false", "False")
    if require_mount:
        sentinel = os.environ.get("MOUNT_SENTINEL", os.path.join(originals, ".mounted"))
        if not os.path.exists(sentinel):
            return False, "The library volume looks unmounted (mount sentinel missing)."
    return True, None


def _year_arg(q, name):
    """Parse a from/to year query param (YYYY or YYYY-MM) → int year, or None."""
    v = (q.get(name) or [None])[0]
    if not v:
        return None
    try:
        return int(str(v)[:4])
    except ValueError:
        return None


def _residences_with_geo():
    """The residence config (the Settings page's source) plus the map's computed
    centroid per residence (by id), so /api/residences carries centers + radii +
    date ranges in one place. Additive — existing config keys are untouched."""
    cfg = load_residences()
    geo = {g["id"]: g for g in PLACES.residence_geo()}
    out = []
    for R in cfg:
        g = geo.get(R.get("id"))
        merged = dict(R)
        if g and g.get("lat") is not None:
            merged["center"] = {"lat": g["lat"], "lng": g["lng"]}
        out.append(merged)
    return out


def _haversine_km(lat1, lng1, lat2, lng2):
    import math
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1); dl = math.radians(lng2 - lng1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _mask_points_near_residences(pts, centers, r_km):
    # centers: list of {"lat":..,"lng":..}. Returns (new_list, masked_count).
    if not centers: return pts, 0
    out, n = [], 0
    for p in pts:
        near = any(_haversine_km(p["lat"], p["lng"], c["lat"], c["lng"]) <= r_km
                   for c in centers if c.get("lat") is not None)
        if near:
            q = dict(p); q["lat"] = round(p["lat"], 2); q["lng"] = round(p["lng"], 2)
            q["approx"] = True; out.append(q); n += 1
        else:
            out.append(p)
    return out, n

# sharpness distribution comes from the project db (exists for every analyzed asset)
try:
    _c = _ro()
    BLUR_SORTED = sorted(
        r[0] for r in _c.execute(
            f"SELECT blur_laplacian FROM assets WHERE blur_laplacian IS NOT NULL AND {EXCLUDE_SQL}"))
    _c.close()
except Exception as e:
    print(f"enrichment: blur distribution unavailable: {e}", flush=True)

# The Apple-enrichment startup globals (ENR_HAS / APPLE_SCORE / APPLE_SORTED /
# PROTECTED_IDS / SD_IDS) and the SD->candidate fold are built by reload_enrichment(),
# defined below (it must follow _merge_sd_into_candidates) and called once at startup.
# Extracted into a function so it can be re-invoked after an apple-enrichment.db swap.


def _merge_sd_into_candidates():
    """Fold the SD set into the candidate machinery as rule 'SD' so it reuses the
    existing Candidates view (chips, per-rule filter, bulk cut). Needs year/month/ts
    for any SD id not already a candidate — one metadata read."""
    if not SD_IDS:
        return
    new_ids = [i for i in SD_IDS if i not in CAND]
    meta = {}
    if new_ids:
        conn = _ro()
        for i in range(0, len(new_ids), 900):
            chunk = new_ids[i:i + 900]
            qm = ",".join("?" * len(chunk))
            for r in conn.execute(
                    f"SELECT id, year, month, capture_timestamp, duration_seconds, extension "
                    f"FROM assets WHERE id IN ({qm})", chunk):
                meta[r["id"]] = r
        conn.close()
    for idv in SD_IDS:
        c = CAND.get(idv)
        if c is None:
            r = meta.get(idv)
            if r is None:
                continue
            ext = (r["extension"] or "").upper()
            c = {"rules": [], "m": {}, "year": r["year"], "month": r["month"],
                 "ts": r["capture_timestamp"], "dur": r["duration_seconds"],
                 "is_video": ext in VIDEO_EXT}
            CAND[idv] = c
            CAND_IDS.add(idv)
            CAND_BUCKET_TOTAL[bucket_of(r["year"], r["month"])] += 1
        if "SD" not in c["rules"]:
            c["rules"].append("SD")


def reload_enrichment():
    """Build the Apple-enrichment startup globals, then fold SD into the candidate view.
    Extracted verbatim from the former top-level block so it runs at startup AND can be
    re-invoked after an apple-enrichment.db swap.

    THREAD SAFETY: every structure is built into a NEW LOCAL, then the module globals are
    rebound at the end with plain assignment (atomic name rebind) — request threads reading
    ENR_HAS/APPLE_SCORE/etc. concurrently see old-or-new, never a torn half-built value.

    CANDIDATE SIDE: the SD->candidate fold below mutates the shared CAND machinery IN PLACE
    (additive-only; CAND is assembled by a multi-module pipeline with no rebuild seam), so a
    LIVE re-run can't be made to match a fresh restart within a surgical edit. It is therefore
    only safe at startup (single-threaded boot). The enrichment-import worker RESTARTS the
    service instead of calling this live; a clean in-process candidate reload awaits the
    single-agent refactor window."""
    global ENR_HAS, APPLE_SCORE, APPLE_SORTED, PROTECTED_IDS, SD_IDS, RULE_PRIORITY
    try:
        _e = _enr()
        _enr_has = {r[0] for r in _e.execute("SELECT asset_id FROM asset_uuid")}
        _apple_score = {r[0]: r[1] for r in _e.execute(
            "SELECT asset_id, overall FROM apple_score WHERE overall IS NOT NULL")}
        _apple_sorted = sorted(_apple_score.values())
        _protected_ids = set()
        if PROTECTED_NAMES:
            _qm = ",".join("?" * len(PROTECTED_NAMES))
            _protected_ids = {r[0] for r in _e.execute(
                f"SELECT DISTINCT asset_id FROM persons WHERE person IN ({_qm})", PROTECTED_NAMES)}
        # ---- Stage 3: portable protect union (ADDITIVE, apple-independent) -------------
        # (1) faces-confirmed: DISTINCT asset_ids of faces assigned (ANY source, incl. the
        #     Stage-2b 'video-match' rows) to a protected person. faces.db persons.is_protected
        #     mirrors loupe-settings protected_people, so this needs no Apple person labels.
        # (2) Live-MOV bridge: a Live-Photo MOV whose still partner is already protected rides
        #     along, so every keep/cut/reclaim decision moves with its still.
        # Straight ro reads of the app-owned stores; each in its OWN try/except so a faces or
        # pairs hiccup can NEVER drop the apple set already accumulated in _protected_ids.
        try:
            _fdb = sqlite3.connect(
                f"file:{os.path.join(APP_DATA, 'faces.db')}?mode=ro", uri=True)
            _protected_ids |= {r[0] for r in _fdb.execute(
                "SELECT DISTINCT f.asset_id FROM assignments a "
                "JOIN persons p ON p.person_id=a.person_id AND p.is_protected=1 "
                "JOIN faces f ON f.face_id=a.face_id")}
            _fdb.close()
        except Exception as _fe:
            print(f"enrichment: faces-confirmed protect unavailable ({_fe})", flush=True)
        try:
            _pdb = sqlite3.connect(f"file:{PAIRS_DB}?mode=ro", uri=True)
            _protected_ids |= {r[1] for r in _pdb.execute(
                "SELECT still_asset_id, mov_asset_id FROM live_pairs "
                "WHERE mov_asset_id IS NOT NULL") if r[0] in _protected_ids}
            _pdb.close()
        except Exception as _pe:
            print(f"enrichment: live-mov protect bridge unavailable ({_pe})", flush=True)
        # -------------------------------------------------------------------------------
        # SD = document label  ∪  ocr-heavy(>=4 terms)  ∪  PNG carrying OCR text
        _doc = {r[0] for r in _e.execute("SELECT DISTINCT asset_id FROM labels WHERE category='document'")}
        _ocr4 = {r[0] for r in _e.execute(
            "SELECT asset_id FROM labels WHERE category='ocr' GROUP BY asset_id HAVING COUNT(*)>=4")}
        _ocrany = {r[0] for r in _e.execute("SELECT DISTINCT asset_id FROM labels WHERE category='ocr'")}
        _e.close()
        _c = _ro()
        _png = {r[0] for r in _c.execute(
            f"SELECT id FROM assets WHERE upper(extension)='PNG' AND {EXCLUDE_SQL}")}
        # stills only: screen RECORDINGS / home videos that merely contain text must not
        # land in a batch-cuttable "screenshots & documents" pile (videos have their own path).
        _vq = ",".join("?" * len(VIDEO_EXT))
        _imgids = {r[0] for r in _c.execute(
            f"SELECT id FROM assets WHERE upper(extension) NOT IN ({_vq}) AND {EXCLUDE_SQL}",
            sorted(VIDEO_EXT))}
        _c.close()
        _sd_ids = (_doc | _ocr4 | (_png & _ocrany)) & _imgids
        # atomic rebind (build-new-then-assign; never mutate the live globals in place)
        ENR_HAS = _enr_has
        APPLE_SCORE = _apple_score
        APPLE_SORTED = _apple_sorted
        PROTECTED_IDS = _protected_ids
        SD_IDS = _sd_ids
        print(f"enrichment: {len(ENR_HAS)} assets bridged · {len(APPLE_SCORE)} scored · "
              f"{len(PROTECTED_IDS)} protected frames (apple+faces+mov) · {len(SD_IDS)} screenshots/docs",
              flush=True)
    except Exception as e:
        print(f"enrichment: Apple data unavailable ({e}); facts-only mode", flush=True)
    # fold SD into the candidate machinery — runs regardless (byte-identical to the former
    # top-level lines). In-place mutation of shared CAND; safe at startup only.
    if "SD" not in RULE_PRIORITY:
        RULE_PRIORITY = RULE_PRIORITY + ["SD"]   # lowest priority: only the primary badge when nothing else applies
    _merge_sd_into_candidates()
    print(f"candidates+SD: {len(CAND_IDS)} total flagged", flush=True)


reload_enrichment()


def primary_rule(rules):
    return next((r for r in RULE_PRIORITY if r in rules), rules[0])


def driver_text(c):
    """(rule, human 'why this is a candidate') for the Candidates focus view."""
    r = primary_rule(c["rules"]); m = c["m"].get(r, {})
    if r == "B4":
        s = f"Blurry — sharpness {m.get('g')} (p10 cutoff 93.6)"
        if m.get("fp"):
            s += f" · sharp center {m.get('c')} → likely FALSE POSITIVE (rescue?)"
        elif m.get("burst"):
            s += " · also a burst frame"
        return r, s
    if r == "B3":
        return r, f"Burst extra — {m.get('rank')} of {m.get('csize')}, sharpest kept"
    if r == "B2":
        return r, f"Screenshot {m.get('w')}×{m.get('h')}"
    if r == "A2b":
        d = c.get("dur"); return r, (f"Short clip {d:.1f}s (1–3s — often intentional)" if d else "Short clip (1–3s)")
    if r == "A2a":
        d = c.get("dur"); return r, (f"Accidental clip {d:.1f}s (<1s tap)" if d else "Accidental clip (<1s)")
    if r == "SD":
        return r, "Screenshot / document — text-heavy, categorically low-value"
    if r == "PB":
        return r, "Place-burst — many frames at one venue on one day (worst-first within it)"
    return r, "Junk import — no EXIF / GPS / camera"


# ---------------------------------------------------------------------------
# decisions store (UNIFIED sqlite, write-through) + in-memory state index
# ---------------------------------------------------------------------------
_dlock = threading.Lock()
_dconn = sqlite3.connect(DECISIONS_DB, check_same_thread=False)
_dconn.execute("PRAGMA journal_mode=WAL")
_dconn.execute("""CREATE TABLE IF NOT EXISTS decisions(
    id INTEGER PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('keep','cut')),
    bucket TEXT, updated_at INTEGER NOT NULL)""")
_dconn.execute("CREATE INDEX IF NOT EXISTS idx_bucket ON decisions(bucket)")
_dconn.commit()
# in-memory: id -> state  (used for day/month per-item lookups & day aggregation)
STATE = {row[0]: row[1] for row in _dconn.execute("SELECT id, state FROM decisions")}
print(f"decisions (unified): {len(STATE)} prior ({DECISIONS_DB})", flush=True)


# ---------------------------------------------------------------------------
# Live Photo pairing — icloudpd split each Live Photo into a still (IMG_xxxx.HEIC)
# + its motion clip (IMG_xxxx_HEVC.MOV); both show as separate assets. We HIDE the
# paired motion MOV from review and bind it to its still so every keep/cut decision
# and the delete/reclaim math move together. Built into loupe's OWN rw store
# (pairs.db); metadata.db + apple-enrichment.db stay read-only.
# ---------------------------------------------------------------------------
STILL_EXT = {"HEIC", "JPG", "JPEG"}
STILL_TO_MOV = {}        # still_asset_id -> mov_asset_id
MOV_TO_STILL = {}        # mov_asset_id   -> still_asset_id  (== the hidden set)
HIDDEN_MOV_IDS = set()
HIDDEN_BY_BUCKET = defaultdict(int)   # month-bucket -> count of hidden movs (review scope)
MOV_SIZE = {}            # mov_asset_id -> file_size_bytes (reclaim math)
LIVE_ORPHAN_IDS = set()  # look-like-live MOVs with NO findable still -> stay VISIBLE


def build_live_pairs():
    """(Re)build pairs.db + in-memory maps. Idempotent; runs each startup.
    a) authoritative: shared Apple uuid with exactly one still + one mov.
    b) tail: IMG_xxxx_HEVC.MOV <-> IMG_xxxx.HEIC, gated by is_live_photo_video."""
    t0 = time.time()
    # Cold start: metadata.db is created by the develop/ingest step. With no db there are
    # no assets to pair — leave the (already-empty) maps as-is rather than raising on a
    # mode=ro open. No-op when the db exists (the normal path).
    if not os.path.exists(METADATA_DB):
        print("live-pairs: metadata.db absent — 0 pairs (cold start)", flush=True)
        return
    meta = _ro()
    rows = meta.execute(
        f"SELECT id, filepath, filename, upper(extension) ext, is_live_photo_video lpv, "
        f"file_size_bytes sz, year, month, capture_timestamp ts FROM assets "
        f"WHERE {EXCLUDE_SQL}").fetchall()
    meta.close()
    A = {r["id"]: r for r in rows}
    stills_by_name = defaultdict(list)   # FILENAME upper -> [still ids]
    for r in rows:
        if r["ext"] in STILL_EXT and r["filename"]:
            stills_by_name[r["filename"].upper()].append(r["id"])

    pairs = []                       # (still_id, mov_id, method, confidence)
    paired_movs, paired_stills = set(), set()

    # --- method a: shared Apple uuid (exactly one still + one mov) ---
    try:
        enr = sqlite3.connect(f"file:{ENRICH_DB}?mode=ro", uri=True)
        uu = defaultdict(list)
        for aid, uuid in enr.execute("SELECT asset_id, uuid FROM asset_uuid"):
            if aid in A:
                uu[uuid].append(aid)
        enr.close()
        for uuid, ids in uu.items():
            stills = [i for i in ids if A[i]["ext"] in STILL_EXT]
            movs = [i for i in ids if A[i]["ext"] == "MOV"]
            if len(stills) == 1 and len(movs) == 1:
                pairs.append((stills[0], movs[0], "uuid", 1.0))
                paired_movs.add(movs[0]); paired_stills.add(stills[0])
    except Exception as e:
        print(f"live-pairs: uuid method skipped ({e})", flush=True)

    # --- method b: filename fallback for the unbridged live-video tail ---
    for r in rows:
        mid = r["id"]
        if r["ext"] != "MOV" or mid in paired_movs or not r["lpv"] or not r["filename"]:
            continue
        stem = _re.sub(r"\.[^.]+$", "",
                       _re.sub(r"_HEVC(?=\.[^.]+$)", "", r["filename"], flags=_re.I))
        cands = []
        for e in ("HEIC", "JPG", "JPEG"):
            cands += [i for i in stills_by_name.get(f"{stem}.{e}".upper(), [])
                      if i not in paired_stills]
        if not cands:
            continue
        if len(cands) == 1:
            sid = cands[0]
        else:                                  # recycled filename: closest capture time
            mts = r["ts"] or 0
            sid = min(cands, key=lambda i: abs((A[i]["ts"] or 0) - mts))
            if abs((A[sid]["ts"] or 0) - mts) > 86400:
                continue                       # >1 day apart: not trustworthy, leave visible
        pairs.append((sid, mid, "filename", 0.9))
        paired_movs.add(mid); paired_stills.add(sid)

    # --- orphans: look-like-live MOVs with no resolved still stay VISIBLE ---
    for r in rows:
        if r["ext"] == "MOV" and r["id"] not in paired_movs and (
                r["lpv"] or (r["filename"] and _re.search(r"_HEVC\.[^.]+$", r["filename"], flags=_re.I))):
            LIVE_ORPHAN_IDS.add(r["id"])

    # --- persist to loupe's own rw store ---
    db = sqlite3.connect(PAIRS_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS live_pairs(
        still_asset_id INTEGER PRIMARY KEY, mov_asset_id INTEGER UNIQUE,
        method TEXT, confidence REAL, built_at INTEGER)""")
    db.execute("DELETE FROM live_pairs")
    now = int(time.time())
    db.executemany("INSERT OR REPLACE INTO live_pairs VALUES(?,?,?,?,?)",
                   [(s, m, meth, c, now) for (s, m, meth, c) in pairs])
    db.commit(); db.close()

    for (s, m, meth, c) in pairs:
        STILL_TO_MOV[s] = m; MOV_TO_STILL[m] = s
        MOV_SIZE[m] = A[m]["sz"] or 0
        HIDDEN_BY_BUCKET[bucket_of(A[m]["year"], A[m]["month"])] += 1
    HIDDEN_MOV_IDS.update(MOV_TO_STILL)
    bym = defaultdict(int)
    for (_, _, meth, _) in pairs:
        bym[meth] += 1
    print(f"live-pairs: {len(pairs)} pairs ({dict(bym)}) · {len(HIDDEN_MOV_IDS)} movs hidden · "
          f"{len(LIVE_ORPHAN_IDS)} orphan live-movs visible · {time.time()-t0:.1f}s", flush=True)


build_live_pairs()

# Hidden movs must also vanish from the candidate machinery (a ~3s live clip can be
# flagged A2b/short-clip). Prune them so candidate counts/lists never surface them.
_pruned = 0
for _mid in list(HIDDEN_MOV_IDS):
    if _mid in CAND_IDS:
        CAND.pop(_mid, None); CAND_IDS.discard(_mid); _pruned += 1
if _pruned:
    CAND_BUCKET_TOTAL.clear()
    for _cid, _cit in CAND.items():
        CAND_BUCKET_TOTAL[bucket_of(_cit["year"], _cit["month"])] += 1
    print(f"live-pairs: pruned {_pruned} hidden movs from candidate set", flush=True)

# Carry any pre-existing decision that sits on a hidden mov onto its still (single
# source of truth), then drop the mov's row. decisions.db only.
_mig = 0
with _dlock:
    for _mid in list(HIDDEN_MOV_IDS):
        if _mid in STATE:
            _sid = MOV_TO_STILL[_mid]
            if _sid not in STATE:
                _row = _dconn.execute("SELECT bucket FROM decisions WHERE id=?", (_mid,)).fetchone()
                _dconn.execute(
                    "INSERT OR REPLACE INTO decisions(id,state,bucket,updated_at) VALUES(?,?,?,?)",
                    (_sid, STATE[_mid], _row[0] if _row else None, int(time.time())))
                STATE[_sid] = STATE[_mid]
            _dconn.execute("DELETE FROM decisions WHERE id=?", (_mid,))
            STATE.pop(_mid, None); _mig += 1
    if _mig:
        _dconn.commit()
        print(f"live-pairs: migrated {_mig} stray hidden-mov decisions onto stills", flush=True)


# ---------------------------------------------------------------------------
# Renders — edited-render DISPLAY seam (app-owned, beside pairs.db/vault.db).
# Maps an ORIGINAL asset_id to a local edited-render file that should be DISPLAYED in
# its place. A render is a display attribute of the original, NEVER an asset, NEVER
# hidden, NEVER a decision anchor — decisions/vault/candidates keep keying on the
# original id. This stage builds the seam INERT: the store starts EMPTY, so display_path
# always falls back to the original filepath and serving is byte-identical. A later stage
# populates renders.db (osxphotos pull + uuid->asset_id importer) and flips the served file.
# ---------------------------------------------------------------------------
RENDERS_DB = os.path.join(APP_DATA, "renders.db")   # loupe's OWN rw store for display renders
RENDER_PATHS = {}        # asset_id -> render_path (only rows whose file EXISTS on disk)


def _rebuild_renders():
    """(Re)build RENDER_PATHS from renders.db. Idempotent; runs each startup, mirroring
    build_live_pairs' import-time-global discipline. Self-creates the (empty) store if absent.
    A row whose render_path is missing on disk is SKIPPED (a not-yet-populated gap, never an
    error)."""
    global RENDER_PATHS
    db = sqlite3.connect(RENDERS_DB)
    db.execute("""CREATE TABLE IF NOT EXISTS renders(
        asset_id INTEGER PRIMARY KEY, render_path TEXT NOT NULL,
        uuid TEXT, render_bytes INTEGER, built_at INTEGER)""")
    db.commit()
    rows = db.execute("SELECT asset_id, render_path FROM renders").fetchall()
    db.close()
    paths = {aid: rp for aid, rp in rows if rp}
    RENDER_PATHS = paths
    print(f"renders: {len(RENDER_PATHS)} display renders registered "
          f"(serve-time existence, lazy like originals)", flush=True)


_rebuild_renders()


def display_path(idv, fallback_filepath):
    """The single chokepoint resolving which file to DISPLAY/serve for an asset id: the edited
    render if one is registered, else the caller's already-fetched original filepath. Existence
    is checked HERE at serve time (only on a render-row hit), not via a boot filter — a registered
    render whose file is absent falls through to the original. Empty store / a miss => returns the
    fallback with no stat (byte-identical serving)."""
    rp = RENDER_PATHS.get(idv)
    if rp is None:
        return fallback_filepath
    return rp if os.path.exists(rp) else fallback_filepath


# ---------------------------------------------------------------------------
# Edits — original<->edit VARIANT GROUPS (app-owned, beside pairs.db/renders.db).
# A RELATIONSHIP between TWO library assets (distinct from renders.db, which is a
# display attribute of ONE asset — see sessions/2026-07-03-edit-aware-design.md).
# edits.db is rebuilt at import time into in-memory maps and decorated onto
# lean()/item(); the flags (is_edit/has_edits) are mechanically derived from the
# membership rows, never hand-maintained. Edit-linked assets are PROTECTED as a
# unit (never nominated / never exported for delete), NOT hidden.
# ---------------------------------------------------------------------------
EDITS_DB = os.path.join(APP_DATA, "edits.db")   # loupe's OWN rw store for edit relationships
IS_EDIT = set()              # asset_ids that ARE a derived edit of another asset
HAS_EDITS = set()            # original asset_ids whose group contains >=1 edit
EDIT_GROUP = {}              # asset_id -> group_id
EDIT_GROUP_MEMBERS = {}      # group_id -> [(asset_id, role, edit_type), ...]
EDIT_LINKED_IDS = set()      # IS_EDIT | HAS_EDITS (protect-as-unit set)


def _rebuild_edits():
    """(Re)build the edit-relationship maps from edits.db. Idempotent; runs each startup,
    mirroring build_live_pairs / _rebuild_renders. edits.db is read-only here; a missing or
    empty/unreadable store yields empty maps + one logged line, never a crash."""
    global IS_EDIT, HAS_EDITS, EDIT_GROUP, EDIT_GROUP_MEMBERS, EDIT_LINKED_IDS
    is_edit, edit_group, members, groups_with_edit = set(), {}, defaultdict(list), set()
    if os.path.exists(EDITS_DB):
        try:
            c = sqlite3.connect(f"file:{EDITS_DB}?mode=ro", uri=True)
            try:
                for aid, gid, role, et in c.execute(
                        "SELECT asset_id, group_id, role, edit_type FROM variant_members"):
                    edit_group[aid] = gid
                    members[gid].append((aid, role, et))
                    if role == "edit":
                        is_edit.add(aid); groups_with_edit.add(gid)
            finally:
                c.close()
        except Exception as e:
            print(f"edits: edits.db unreadable ({e}) — 0 edit relationships", flush=True)
            is_edit, edit_group, members, groups_with_edit = set(), {}, defaultdict(list), set()
    else:
        print("edits: no edits.db — 0 edit relationships", flush=True)
    has_edits = {aid for gid in groups_with_edit
                 for (aid, role, _et) in members[gid] if role == "original"}
    IS_EDIT = is_edit
    HAS_EDITS = has_edits
    EDIT_GROUP = dict(edit_group)
    EDIT_GROUP_MEMBERS = {gid: list(v) for gid, v in members.items()}
    EDIT_LINKED_IDS = IS_EDIT | HAS_EDITS
    print(f"edits: {len(EDIT_GROUP_MEMBERS)} variant groups · {len(IS_EDIT)} edits · "
          f"{len(HAS_EDITS)} originals-with-edits · {len(EDIT_LINKED_IDS)} protected-as-unit",
          flush=True)


_rebuild_edits()


# ---------------------------------------------------------------------------
# Places — geolocation. The data layer (payload/venues/trips/bursts) lives in
# places.py (read-only metadata+enrichment, reuses summaries' home model + venue
# cache). Here we only fold the place-burst set into the candidate machinery as a
# new rule "PB" — so it reuses the existing Candidates view, worst-first ordering,
# and (critically) the SAME people-protect bulk-cut guard. Doctrine: PLACE
# nominates the burst, SCORE orders within it, PEOPLE protect.
# ---------------------------------------------------------------------------
import places as PLACES

# Install the residences at startup.
#
# places.py documents that "server.py sets RESIDENCES before first use", and save_residences()
# does call PLACES.set_residences() -- but only when residences are SAVED. Nothing ran on
# boot, so places.RESIDENCES came back as [] after every restart and stayed that way until
# someone happened to edit residences again.
#
# With an empty store every frame's distance-from-home is 0.0, so nothing is ever "away".
# By that module's own comment the same predicate feeds trip detection, the map's hide-home
# and away-only bursts. Measured before this fix: 69,071 geotagged frames, max distance
# 0.0 km, and /api/trips returned an empty list -- the Trips room was silently dead.
PLACES.set_residences(load_residences())
PLACES.HIDDEN_MOV_IDS = HIDDEN_MOV_IDS     # one map dot per Live Photo; bursts count stills only
PLACES.set_residences(load_residences())   # data-driven is_home (seeded once); supersedes hardcoded eras
PB_IDS = set()
try:
    PB_IDS = PLACES.burst_ids() - HIDDEN_MOV_IDS      # never nominate a hidden live mov
    if "PB" not in RULE_PRIORITY:
        RULE_PRIORITY = RULE_PRIORITY + ["PB"]
    _pb_new = [i for i in PB_IDS if i not in CAND]
    _pmeta = {}
    if _pb_new:
        _c = _ro()
        for _i in range(0, len(_pb_new), 900):
            _chunk = _pb_new[_i:_i + 900]
            _qm = ",".join("?" * len(_chunk))
            for _r in _c.execute(
                    f"SELECT id, year, month, capture_timestamp, duration_seconds, extension "
                    f"FROM assets WHERE id IN ({_qm})", _chunk):
                _pmeta[_r["id"]] = _r
        _c.close()
    for _id in PB_IDS:
        _cc = CAND.get(_id)
        if _cc is None:
            _r = _pmeta.get(_id)
            if _r is None:
                continue
            _ext = (_r["extension"] or "").upper()
            _cc = {"rules": [], "m": {}, "year": _r["year"], "month": _r["month"],
                   "ts": _r["capture_timestamp"], "dur": _r["duration_seconds"],
                   "is_video": _ext in VIDEO_EXT}
            CAND[_id] = _cc; CAND_IDS.add(_id)
            CAND_BUCKET_TOTAL[bucket_of(_r["year"], _r["month"])] += 1
        if "PB" not in _cc["rules"]:
            _cc["rules"].append("PB")
    print(f"places: {len(PB_IDS)} place-burst frames folded into candidates (rule PB)", flush=True)
except Exception as _e:
    print(f"places: burst merge skipped ({_e})", flush=True)


# ---------------------------------------------------------------------------
# Personal Vault (PROTOTYPE: flag-and-hide only — NO file moves). A THIRD axis,
# orthogonal to keep/cut: a visibility flag. VAULT_IDS feeds the SAME canonical
# hide-layer as HIDDEN_MOV_IDS (HIDDEN_VIEW_IDS = movs ∪ vault) so vaulted assets
# vanish from every view + count; they are also wired OUT of every export/delete
# path (a vaulted asset can never enter a cut batch). Store: vault.db (rw, loupe-
# owned, reversible). decisions.db keep/cut is untouched.
# ---------------------------------------------------------------------------
VAULT_DB = os.path.join(APP_DATA, "vault.db")
_vconn = sqlite3.connect(VAULT_DB, check_same_thread=False)
_vconn.execute("CREATE TABLE IF NOT EXISTS vault("
               "asset_id INTEGER PRIMARY KEY, marked_at INTEGER NOT NULL, note TEXT)")
_vconn.commit()
_vlock = threading.Lock()
VAULT_IDS = set()
VAULT_BY_BUCKET = defaultdict(int)   # month-bucket -> count of vaulted assets (review scope)
HIDDEN_VIEW_IDS = set()              # canonical "hide from EVERY view/count" = movs ∪ vault


def _rebuild_vault_indices():
    """Recompute VAULT_IDS + per-bucket counts + the combined view-hide set from vault.db,
    and push the union into places so the map/trips/bursts hide vaulted assets too."""
    global VAULT_IDS, VAULT_BY_BUCKET, HIDDEN_VIEW_IDS
    with _vlock:
        VAULT_IDS = {r[0] for r in _vconn.execute("SELECT asset_id FROM vault")}
    vbb = defaultdict(int)
    if VAULT_IDS:
        conn = _ro(); ids = list(VAULT_IDS)
        for i in range(0, len(ids), 900):
            ch = ids[i:i + 900]; qm = ",".join("?" * len(ch))
            for r in conn.execute(
                    f"SELECT id, year, month FROM assets WHERE id IN ({qm}) AND {EXCLUDE_SQL}", ch):
                vbb[bucket_of(r["year"], r["month"])] += 1
        conn.close()
    VAULT_BY_BUCKET = vbb
    HIDDEN_VIEW_IDS = HIDDEN_MOV_IDS | VAULT_IDS | PRODUCTION_IDS   # + production held aside (unconditional)
    PLACES.HIDDEN_MOV_IDS = HIDDEN_VIEW_IDS   # places-side hide-layer = movs ∪ vault ∪ production
    PLACES._cache.clear()                     # trips/map/bursts recompute without vaulted assets


def vault_set(idv, on, note=None):
    """Mark/unmark an asset personal. Writes ONLY vault.db; reversible. Re-derives the hide
    layer so the asset vanishes from (or returns to) every view immediately."""
    with _vlock:
        if on:
            _vconn.execute("INSERT OR REPLACE INTO vault(asset_id,marked_at,note) VALUES(?,?,?)",
                           (idv, int(time.time()), note))
        else:
            _vconn.execute("DELETE FROM vault WHERE asset_id=?", (idv,))
        _vconn.commit()
    _rebuild_vault_indices()
    _rebuild_nsfw_indices()                             # vault rebuild reset HIDDEN_VIEW_IDS to movs|vault — re-add nsfw
    _period_rows_cache.clear(); _sample_cache.clear()   # backdrops/samples may include the (un)vaulted id


# ---------------------------------------------------------------------------
# Production work-product → Closed Set (path-derived, STATIC). The production/ subtree
# stays in the library but is held aside from normal review: composed UNCONDITIONALLY
# into the canonical HIDDEN_VIEW_IDS (exactly like vault), excluded from every export/
# delete batch, and surfaced ONLY on the owner's Closed Set "Production" facet. This is a
# DISTINCT facet from the NSFW nudity screen — NO threshold / disclosure / nudity
# semantics whatsoever. Membership is PATH-DERIVED (filepath under LIBRARY_ROOT/production/)
# so there is NO db, NO settings write, NO migration: it recomputes identically each
# startup. Defined BEFORE the import-time _rebuild_vault_indices()/_rebuild_nsfw_indices()
# calls so both recompose sites pick it up. PROD_BY_BUCKET is vault-disjoint (double-
# subtract guard for the Overview month totals; production∩vault is empty today).
# ---------------------------------------------------------------------------
PRODUCTION_IDS = set()
PROD_BY_BUCKET = defaultdict(int)
_prod_prefix = LIBRARY_ROOT.rstrip(os.sep) + os.sep + "production" + os.sep + "%"
try:
    _pconn = _ro()
    PRODUCTION_IDS = {r["id"] for r in _pconn.execute(
        f"SELECT id FROM assets WHERE filepath LIKE ? AND {EXCLUDE_SQL}", (_prod_prefix,))}
    _prod_vids = {r[0] for r in _vconn.execute("SELECT asset_id FROM vault")}
    for r in _pconn.execute(
            f"SELECT id, year, month FROM assets WHERE filepath LIKE ? AND {EXCLUDE_SQL}",
            (_prod_prefix,)):
        if r["id"] in _prod_vids:
            continue                       # never double-subtract a (hypothetical) vaulted production clip
        PROD_BY_BUCKET[bucket_of(r["year"], r["month"])] += 1
    _pconn.close()
except Exception as _e:
    print(f"production: index build failed ({_e})", flush=True)
print(f"production: {len(PRODUCTION_IDS)} work-product videos held aside (Closed Set · Production)", flush=True)


_rebuild_vault_indices()
print(f"vault: {len(VAULT_IDS)} personal/vaulted assets hidden", flush=True)


# ---------------------------------------------------------------------------
# NSFW review foundation (Stage 3a, OWNER-SIDE). NSFW_IDS is the flagged set derived
# from nsfw.db's raw scores at a tunable threshold, minus owner-cleared false positives.
# Read-side mirror of the VAULT_IDS machinery. NOTE: this is LOADED here but deliberately
# NOT composed into HIDDEN_VIEW_IDS / contact-sheet / lean / places hide sets — guest
# suppression is Stage 3b. Only export/delete exclusion + owner surfaces use it in 3a.
# ---------------------------------------------------------------------------
NSFW_IDS = set()
NSFW_BY_BUCKET = defaultdict(int)   # month-bucket -> count of nsfw-suppressed assets (Overview totals; gated on nsfw_enabled)


def _nsfw_settings():
    """(threshold, cleared-set) from loupe-settings.json. threshold default 0.5 (PROVISIONAL
    — calibration sets the real value); cleared default []. Read-only."""
    with _settings_lock:
        try:
            s = json.load(open(SETTINGS_PATH))
        except Exception:
            s = {}
    try:
        thr = float(s.get("nsfw_threshold", 0.5))
    except (TypeError, ValueError):
        thr = 0.5
    cleared = {int(x) for x in (s.get("nsfw_cleared") or [])
               if str(x).lstrip("-").isdigit()}
    return thr, cleared


def _mask_radius_km():
    with _settings_lock:
        try: s = json.load(open(SETTINGS_PATH))
        except Exception: s = {}
    try: return float(s.get("mask_radius_km", 1.0))
    except (TypeError, ValueError): return 1.0


def _search_settings():
    """search_backend from loupe-settings.json: "local" | "disabled" (default). Read-only,
    mirrors _nsfw_settings. "local" is the only backend implemented (stage 5, step 4c)."""
    with _settings_lock:
        try:
            s = json.load(open(SETTINGS_PATH))
        except Exception:
            s = {}
    backend = s.get("search_backend", "disabled")
    return backend if backend in ("local", "disabled") else "disabled"


def _nsfw_enabled():
    """The owner's master switch (default False). Read-only, mirrors _nsfw_settings. When
    True, the flagged set is composed into the global HIDDEN_VIEW_IDS (suppressed from all
    normal views for everyone); the owner reviews it via /nsfw + the Cutting Room chip."""
    with _settings_lock:
        try:
            return bool(json.load(open(SETTINGS_PATH)).get("nsfw_enabled"))
        except Exception:
            return False


def _rebuild_nsfw_indices():
    """NSFW_IDS = {asset_id FROM nsfw.db scores WHERE max_score >= threshold} − cleared.
    nsfw.db read-only; absent -> empty (no error). Re-threshold needs no rescan. Does NOT
    touch HIDDEN_VIEW_IDS (guest suppression is Stage 3b)."""
    global NSFW_IDS, HIDDEN_VIEW_IDS, NSFW_BY_BUCKET
    thr, cleared = _nsfw_settings()
    ids = set()
    if os.path.exists(NSFW_DB):
        try:
            c = ro(NSFW_DB)                 # read-only; placeholder-bound param, no frozenset-in-SQL
            try:
                ids = {r[0] for r in c.execute(
                    "SELECT asset_id FROM scores WHERE max_score >= ?", (thr,))}
            finally:
                c.close()
        except Exception:
            ids = set()
    NSFW_IDS = ids - cleared
    # STAGE 3b — global suppress (option B): when enabled, compose the flagged set into the
    # canonical view-hide layer so EVERY normal view/count (owner + guest) drops it. The
    # owner's review surfaces (/nsfw, Cutting Room chip) bypass HIDDEN_VIEW_IDS, so they
    # still show the set. PLACES (map/trips/bursts) reads the same global + its cache is
    # busted. _period_rows_cache/_sample_cache are busted by the routes (not yet defined here).
    en = _nsfw_enabled()
    HIDDEN_VIEW_IDS = HIDDEN_MOV_IDS | VAULT_IDS | PRODUCTION_IDS | (NSFW_IDS if en else set())
    PLACES.HIDDEN_MOV_IDS = HIDDEN_VIEW_IDS
    PLACES._cache.clear()
    # per-bucket nsfw count for the Overview month totals — vault-disjoint (avoid double
    # subtract; nsfw∩movs is empty, images vs movs) and empty when disabled.
    bb = defaultdict(int)
    extra = (NSFW_IDS - VAULT_IDS) if en else set()
    if extra:
        try:
            c = _ro(); idl = list(extra)
            for i in range(0, len(idl), 900):
                ch = idl[i:i + 900]; qm = ",".join("?" * len(ch))
                for r in c.execute(
                        f"SELECT id, year, month FROM assets WHERE id IN ({qm}) AND {EXCLUDE_SQL}", ch):
                    bb[bucket_of(r["year"], r["month"])] += 1
            c.close()
        except Exception:
            bb = defaultdict(int)
    NSFW_BY_BUCKET = bb


def save_nsfw_threshold(threshold):
    """Persist the flagged-set threshold (the calibration sweep lever). Locked whole-object
    merge, mirroring save_nsfw_enabled; other keys untouched."""
    with _settings_lock:
        try:
            s = json.load(open(SETTINGS_PATH))
        except Exception:
            s = {}
        s["nsfw_threshold"] = float(threshold)
        json.dump(s, open(SETTINGS_PATH, "w"), indent=2)
    return float(threshold)


def nsfw_clear_set(asset_id, cleared):
    """Add/remove an asset_id in nsfw_cleared (the owner's per-image false-positive escape
    hatch). Locked whole-object merge, mirroring vault_set; other keys untouched."""
    with _settings_lock:
        try:
            s = json.load(open(SETTINGS_PATH))
        except Exception:
            s = {}
        st = {int(x) for x in (s.get("nsfw_cleared") or [])
              if str(x).lstrip("-").isdigit()}
        if cleared:
            st.add(int(asset_id))
        else:
            st.discard(int(asset_id))
        s["nsfw_cleared"] = sorted(st)
        json.dump(s, open(SETTINGS_PATH, "w"), indent=2)
    return sorted(st)


_rebuild_nsfw_indices()
print(f"nsfw: {len(NSFW_IDS)} flagged for owner review (threshold-derived)", flush=True)


def set_decision(pairs, state):
    """pairs: list of (id, bucket). bucket may be None for 'undecided'."""
    now = int(time.time())
    with _dlock:
        cur = _dconn.cursor()
        for idv, bk in pairs:
            idv = MOV_TO_STILL.get(idv, idv)   # a decision never lands on a hidden mov
            if state == "undecided":
                cur.execute("DELETE FROM decisions WHERE id=?", (idv,))
                STATE.pop(idv, None)
            else:
                cur.execute(
                    "INSERT INTO decisions(id,state,bucket,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET state=excluded.state,"
                    "bucket=excluded.bucket,updated_at=excluded.updated_at",
                    (idv, state, bk, now))
                STATE[idv] = state
        _dconn.commit()


def decisions_by_bucket(cand_only=False):
    """{bucket: {'decided':n,'cut':n,'keep':n}}. cand_only restricts to candidate ids."""
    out = defaultdict(lambda: {"decided": 0, "cut": 0, "keep": 0})
    with _dlock:
        rows = list(_dconn.execute("SELECT bucket, state, id FROM decisions"))
    for bk, st, idv in rows:
        if cand_only and idv not in CAND_IDS:
            continue
        if idv in VAULT_IDS:               # vaulted assets are hidden -> not in visible progress
            continue
        out[bk]["decided"] += 1
        out[bk][st] = out[bk].get(st, 0) + 1
    return out


def cut_sizes_by_bucket(cand_only=False):
    """{bucket: (count, bytes)} for cut items — joins cut ids to metadata sizes."""
    with _dlock:
        cuts = list(_dconn.execute(
            "SELECT id, bucket FROM decisions WHERE state='cut'"))
    if cand_only:
        cuts = [(i, b) for (i, b) in cuts if i in CAND_IDS]
    cuts = [(i, b) for (i, b) in cuts if i not in VAULT_IDS]   # vaulted assets never enter reclaim
    if not cuts:
        return {}
    bkof = {idv: bk for idv, bk in cuts}
    sizes = {}
    conn = _ro()
    ids = list(bkof)
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        q = ",".join("?" * len(chunk))
        for row in conn.execute(
                f"SELECT id, file_size_bytes FROM assets WHERE id IN ({q})", chunk):
            sizes[row["id"]] = row["file_size_bytes"] or 0
    conn.close()
    out = defaultdict(lambda: [0, 0])
    for idv, bk in bkof.items():
        out[bk][0] += 1
        out[bk][1] += sizes.get(idv, 0)
        if idv in STILL_TO_MOV:                 # cutting a Live still also deletes its motion clip
            out[bk][1] += MOV_SIZE.get(STILL_TO_MOV[idv], 0)
    return {bk: tuple(v) for bk, v in out.items()}


def _meta_files(ids):
    """{id: (filepath, size_bytes)} for a set of asset ids (read-only metadata)."""
    out = {}
    ids = list(ids)
    if not ids:
        return out
    conn = _ro()
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        qm = ",".join("?" * len(chunk))
        for r in conn.execute(
                f"SELECT id, filepath, file_size_bytes FROM assets WHERE id IN ({qm})", chunk):
            out[r["id"]] = (r["filepath"], r["file_size_bytes"] or 0)
    conn.close()
    return out


def global_stats(cand_only=False):
    if cand_only:
        total = len(CAND_IDS - VAULT_IDS)
        cut = sum(1 for i, s in STATE.items() if s == "cut" and i in CAND_IDS and i not in VAULT_IDS)
        keep = sum(1 for i, s in STATE.items() if s == "keep" and i in CAND_IDS and i not in VAULT_IDS)
    else:
        conn = _ro()
        total = conn.execute(f"SELECT count(*) FROM assets WHERE {EXCLUDE_SQL}").fetchone()[0]
        conn.close()
        total -= len(HIDDEN_VIEW_IDS)            # movs | vault | nsfw(when enabled) — matches the suppressed views
        cut = sum(1 for i, s in STATE.items() if s == "cut" and i not in VAULT_IDS)
        keep = sum(1 for i, s in STATE.items() if s == "keep" and i not in VAULT_IDS)
    decided = cut + keep
    gb = sum(v[1] for v in cut_sizes_by_bucket(cand_only).values()) / 1e9
    return {"total": total, "decided": decided, "cut": cut, "keep": keep,
            "remaining": total - decided, "gb": round(gb, 2),
            "pct": round(100 * decided / total, 1) if total else 0, "cand": cand_only}


# ---------------------------------------------------------------------------
# The Cutting Room — explained overview of everything the rules flagged. READ-ONLY:
# reuses candidates.load_all()'s output (the six rules); makes no decisions. Counts
# come from sets_meta; reclaim from the per-id size snapshot (CR_SIZE), each unique
# frame's bytes counted ONCE for the page total (a frame can match several rules).
# ---------------------------------------------------------------------------
def _cr_ids(key):
    return [d["id"] for d in _citems.get(key, [])]


def _cr_sample(ids, k=6):
    """~k representative thumbnail ids, sampled evenly across the set."""
    if len(ids) <= k:
        return list(ids)
    step = len(ids) / k
    return [ids[int(i * step)] for i in range(k)]


def cutting_room_model(include_nsfw=False):
    meta = {m["key"]: m for m in _csets}             # sets_meta, by rule key (6 rules)
    counts = {k: meta.get(k, {}).get("total", 0) for k in CR_ORDER}
    biggest = max(CR_ORDER, key=lambda k: counts[k]) if CR_ORDER else None   # the one amber pile
    cats = []
    for k in CR_ORDER:
        ids = _cr_ids(k)
        reclaim = sum(CR_SIZE.get(i, 0) for i in ids)
        c = CR_COPY.get(k, {})
        cats.append({
            "key": k, "title": c.get("title", k), "confidence": c.get("conf", ""),
            "count": counts[k], "reclaim_gb": round(reclaim / 1e9, 1),
            "explain": c.get("explain", ""), "rule": c.get("rule", ""),
            "watch": c.get("watch", ""),
            "thumbs": _cr_sample(ids, 6), "accent": (k == biggest)})
    # OWNER-ONLY NSFW chip — computed live from NSFW_IDS (reflects the current threshold +
    # clears, no rescan). Appended only for owner (LAN) requests; a guest never gets it.
    if include_nsfw and NSFW_IDS:
        nids = sorted(NSFW_IDS)
        c = CR_COPY.get("NSFW", {})
        cats.append({
            "key": "NSFW", "title": c.get("title", "Flagged · nudity"),
            "confidence": c.get("conf", ""), "count": len(nids),
            "reclaim_gb": round(sum(CR_SIZE.get(i, 0) for i in nids) / 1e9, 1),
            "explain": c.get("explain", ""), "rule": c.get("rule", ""),
            "watch": c.get("watch", ""), "thumbs": _cr_sample(nids, 6),
            "accent": False, "owner_only": True})
    # OWNER-ONLY PRODUCTION chip — path-derived work-product set, held aside (not a cull
    # queue, so nothing reclaimable). Mirrors the NSFW chip's owner-only gate; opens the
    # Closed Set "Production" facet. A DISTINCT facet from the nudity screen.
    if include_nsfw and PRODUCTION_IDS:
        pids = sorted(PRODUCTION_IDS)
        c = CR_COPY.get("PROD", {})
        cats.append({
            "key": "PROD", "title": c.get("title", "Production · work product"),
            "confidence": c.get("conf", ""), "count": len(pids),
            "reclaim_gb": round(sum(CR_SIZE.get(i, 0) for i in pids) / 1e9, 1),
            "explain": c.get("explain", ""), "rule": c.get("rule", ""),
            "watch": c.get("watch", ""), "thumbs": _cr_sample(pids, 6),
            "accent": False, "owner_only": True})
    # page totals — unique frames (a frame in two rules counts once), bytes once each
    uniq = list(CR_SIZE.keys())
    reclaim_total = sum(CR_SIZE.values())
    kept = sum(1 for i in uniq if STATE.get(i) == "keep")
    cut = sum(1 for i in uniq if STATE.get(i) == "cut")
    return {
        "total": {"unique": len(uniq), "reclaim_gb": round(reclaim_total / 1e9, 1),
                  "kept": kept, "cut": cut, "untouched": len(uniq) - kept - cut},
        "cats": cats,
    }


# ---------------------------------------------------------------------------
# thumbnails — on demand, per day; shared cache; skip-existing; deduped in-flight
# ---------------------------------------------------------------------------
_pool = ThreadPoolExecutor(max_workers=VID_WORKERS)
_img_sem = threading.BoundedSemaphore(IMG_WORKERS)   # cap concurrent image decodes (OOM guard)
_inflight = set()
_iflock = threading.Lock()


# ---------------------------------------------------------------------------
# enrichment import (LAN-gated): accept a Mac bundle, build a SCRATCH enrichment db
# off-thread, sanity-check, back up + atomically swap the live db, then RESTART to
# reload (the candidate-side fold mutates shared state in place — no safe live re-run
# yet; see reload_enrichment). Job status is mirrored to a durable marker file so it
# survives the restart that wipes the in-memory _enrich_jobs dict.
# ---------------------------------------------------------------------------
ENRICH_IMPORT_DIR = os.path.join(APP_DATA, "enrich-import-staging")
ENRICH_MAX_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB ceiling on an uploaded bundle
_enrich_jobs = {}                            # job_id -> status dict (in-memory; mirrored to disk)
_enrich_lock = threading.Lock()


def _enrich_status_path(job_id):
    return os.path.join(ENRICH_IMPORT_DIR, f"{job_id}.status.json")


def _enrich_set(job_id, **fields):
    """Merge fields into a job's status, in memory AND on a durable marker file (the
    worker triggers a restart that wipes the in-memory dict; the marker is the survivor)."""
    with _enrich_lock:
        st = _enrich_jobs.get(job_id, {"job_id": job_id})
        st.update(fields)
        _enrich_jobs[job_id] = st
        snap = dict(st)
    try:
        os.makedirs(ENRICH_IMPORT_DIR, exist_ok=True)
        tmp = _enrich_status_path(job_id) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snap, f)
        os.replace(tmp, _enrich_status_path(job_id))   # atomic marker update
    except Exception:
        pass
    return snap


def _enrich_get(job_id):
    """Job status: in-memory if present, else the durable marker (post-restart path)."""
    with _enrich_lock:
        st = _enrich_jobs.get(job_id)
        if st is not None:
            return dict(st)
    try:
        with open(_enrich_status_path(job_id)) as f:
            return json.load(f)
    except Exception:
        return None


def _enrich_worker(job_id, tgz_path):
    """Runs in _pool: build SCRATCH -> sanity -> backup + atomic swap -> durable 'done'
    -> restart. The live db is only ever replaced by an atomic os.replace of a
    sanity-checked scratch db; any failure leaves it untouched."""
    scratch = os.path.join(APP_DATA, f"apple-enrichment.import-{job_id}.db")
    try:
        _enrich_set(job_id, state="building")
        try:
            os.remove(scratch)
        except FileNotFoundError:
            pass
        # build to SCRATCH (never the live db) — same invocation as the manual promote
        build_py = os.path.join(HERE, "enrichment", "build.py")
        r = subprocess.run(
            [sys.executable, build_py, "--bundle", tgz_path, "--out", scratch],
            capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not os.path.exists(scratch):
            tail = (r.stderr or r.stdout or "")[-800:]
            return _enrich_set(job_id, state="failed",
                               error=f"build failed (rc={r.returncode}): {tail}")
        # sanity-check the scratch db before it can touch the live one
        sc = sqlite3.connect(f"file:{scratch}?mode=ro", uri=True)
        try:
            n_uuid = sc.execute("SELECT COUNT(*) FROM asset_uuid").fetchone()[0]
            n_score = sc.execute("SELECT COUNT(*) FROM apple_score").fetchone()[0]
            n_labels = sc.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
            methods = {m: c for m, c in sc.execute(
                "SELECT match_method, COUNT(*) FROM asset_uuid GROUP BY match_method")}
        finally:
            sc.close()
        if n_uuid <= 0 or n_score <= 0:
            os.remove(scratch)
            return _enrich_set(job_id, state="failed",
                               error=f"sanity failed: asset_uuid={n_uuid} apple_score={n_score}")
        # back up the live db (cp -a, same idiom as the manual promote), then atomic swap
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        if os.path.exists(ENRICH_DB):
            subprocess.run(["cp", "-a", ENRICH_DB, ENRICH_DB + f".bak-{ts}"], check=True)
        os.replace(scratch, ENRICH_DB)   # atomic (same filesystem)
        # durable DONE *before* the restart wipes memory; status survives via the marker
        _enrich_set(job_id, state="done", coverage=n_uuid, scored=n_score, labels=n_labels,
                    methods=methods, backup=os.path.basename(ENRICH_DB) + f".bak-{ts}",
                    note="service restarting to reload enrichment globals")
        # RESTART to reload (in-process candidate-side reload not yet safe; see reload_enrichment).
        # start_new_session detaches systemctl so the SIGTERM to loupe's cgroup can't cut it off
        # before it has enqueued the restart with the user manager.
        try:
            subprocess.Popen(["systemctl", "--user", "restart", "loupe.service"],
                             start_new_session=True)
        except Exception as e:
            _enrich_set(job_id, state="done", coverage=n_uuid, scored=n_score, labels=n_labels,
                        methods=methods, backup=os.path.basename(ENRICH_DB) + f".bak-{ts}",
                        note=f"swapped, but auto-restart failed ({e}) — restart loupe by hand to reload")
    except Exception as e:
        _enrich_set(job_id, state="failed", error=str(e))
        try:
            if os.path.exists(scratch):
                os.remove(scratch)
        except Exception:
            pass
    finally:
        try:
            os.remove(tgz_path)
        except Exception:
            pass


def thumb_path(idv):
    return os.path.join(THUMBS, f"{idv}.jpg")


def _write_atomic(idv, pil_img):
    tmp = thumb_path(idv) + ".tmp"
    pil_img.save(tmp, "JPEG", quality=80)
    os.replace(tmp, thumb_path(idv))


def _load_raw_image(fp):
    """Decode a camera raw file (CR2/CR3/DNG/...) via rawpy. Prefers the
    embedded JPEG preview for speed; falls back to a full raw postprocess."""
    if not RAW_SUPPORTED:
        raise RuntimeError("rawpy not installed -- cannot decode raw file")
    with rawpy.imread(fp) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                return Image.open(io.BytesIO(thumb.data))
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            pass
        rgb = raw.postprocess()
    return Image.fromarray(rgb)


def _make_image_thumb(fp, idv):
    with open(fp, "rb") as f:
        data = f.read()
    try:
        im = Image.open(io.BytesIO(data))
    except UnidentifiedImageError:
        im = _load_raw_image(fp)
    im.draft("RGB", (LONG_EDGE, LONG_EDGE))
    im = im.convert("RGB")
    im.thumbnail((LONG_EDGE, LONG_EDGE))
    _write_atomic(idv, im)


def _make_video_thumb(fp, idv, dur):
    ss = max(0.0, (float(dur) / 2) if dur else 1.0)
    out = thumb_path(idv)
    tmp = out + ".tmp.jpg"   # real .jpg ext: ffmpeg picks format from extension; ".tmp" fails detection
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-ss", f"{ss:.3f}", "-i", fp,
         "-frames:v", "1", "-vf", f"scale={LONG_EDGE}:-1", "-q:v", "4", tmp],
        capture_output=True, text=True, timeout=180)
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
        os.replace(tmp, out)
        return
    if os.path.exists(tmp):
        os.remove(tmp)
    import cv2
    cap = cv2.VideoCapture(fp)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if n > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, n // 2)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise RuntimeError(f"ffmpeg+cv2 both failed: {(r.stderr or '')[-120:]}")
    h, w = frame.shape[:2]
    s = LONG_EDGE / max(w, h)
    if s < 1:
        frame = cv2.resize(frame, (max(1, int(w * s)), max(1, int(h * s))))
    ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok2:
        raise RuntimeError("cv2 imencode failed")
    tmp = out + ".tmp"
    with open(tmp, "wb") as fo:
        fo.write(buf.tobytes())
    os.replace(tmp, out)


def _gen_one(idv, fpath, is_video, dur):
    """Generate a single thumb (idempotent). Returns True on success."""
    fpath = display_path(idv, fpath)        # edited render if registered, else the original
    if os.path.exists(thumb_path(idv)):
        return True
    with _iflock:
        if idv in _inflight:
            return False
        _inflight.add(idv)
    try:
        if is_video:
            _make_video_thumb(fpath, idv, dur)
        else:
            with _img_sem:        # OOM guard: ≤ IMG_WORKERS concurrent 48 MP HEIC decodes
                _make_image_thumb(fpath, idv)
        return True
    except Exception as e:
        print(f"thumb fail id={idv}: {type(e).__name__}: {e}", flush=True)
        return False
    finally:
        with _iflock:
            _inflight.discard(idv)


def enqueue_day_thumbs(items):
    """Fire-and-forget generation for every missing thumb in a freshly opened day."""
    for it in items:
        if not os.path.exists(thumb_path(it["id"])):
            _pool.submit(_gen_one, it["id"], it["fpath"], it["is_video"], it["dur"])


# --- on-demand transcoded video previews (H.264/AAC, <=720p, cached) ----------
# The ONLY original-media endpoint served over the public CF path — and it serves a
# small re-encode, not the raw original (which stays LAN-only via /api/full). Transcode
# concurrency is capped at 1 so it can't gang up on the 8 GB box; the cache means each
# clip transcodes only once.
PLAY_CACHE = os.path.join(APP_DATA, "cache", "play")
os.makedirs(PLAY_CACHE, exist_ok=True)
_xcode_sem = threading.BoundedSemaphore(1)
_xcode_lock = threading.Lock()
_xcode_inflight = {}   # id -> Event (dedup concurrent requests for the same clip)


def play_path(idv):
    return os.path.join(PLAY_CACHE, f"{idv}.mp4")


def transcode_preview(idv, src):
    """src -> cached <=720p H.264/AAC mp4 (faststart), transcoded once. Returns path or None."""
    src = display_path(idv, src)            # edited render if registered, else the original
    out = play_path(idv)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    with _xcode_lock:
        ev = _xcode_inflight.get(idv)
        first = ev is None
        if first:
            ev = threading.Event()
            _xcode_inflight[idv] = ev
    if not first:                       # someone else is already transcoding this id
        ev.wait(timeout=1800)
        return out if (os.path.exists(out) and os.path.getsize(out) > 0) else None
    try:
        with _xcode_sem:                # <= 1 transcode at a time on this box
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return out
            tmp = out + ".tmp.mp4"
            r = subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-i", src,
                 "-vf", "scale=w=1280:h=720:force_original_aspect_ratio=decrease:force_divisible_by=2",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", tmp],
                capture_output=True, text=True, timeout=1800)
            if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, out)
                return out
            if os.path.exists(tmp):
                os.remove(tmp)
            print(f"transcode fail id={idv}: {(r.stderr or '')[-200:]}", flush=True)
            return None
    finally:
        with _xcode_lock:
            _xcode_inflight.pop(idv, None)
        ev.set()


# --- on-demand high-res image previews (cached JPEG; for the focus view) -------
# A derived JPEG (long edge <=2048) of the original, generated once (one NAS read, then
# cached forever) and served over BOTH LAN and the CF path — like the video transcode.
# Needed because originals are HEIC (browsers can't render raw HEIC) and the 400px thumb
# is too soft for full-screen viewing. The raw original (/api/full) stays LAN-only.
PREVIEW_CACHE = os.path.join(APP_DATA, "cache", "preview")
os.makedirs(PREVIEW_CACHE, exist_ok=True)
_preview_sem = threading.BoundedSemaphore(2)     # cap concurrent (HEIC) decodes — OOM guard
_preview_lock = threading.Lock()
_preview_inflight = {}
PREVIEW_EDGE = 2048


# --- W24: bounded preview cache -------------------------------------------
# The cache above is written once per asset and previously grew without bound
# (536 MB / 1001 files when this landed). Cap it and drop least-recently-used
# entries. Eviction is driven from build_preview(), the ONLY writer into
# PREVIEW_CACHE, so every growth path is covered by construction rather than by
# remembering to call this from a second place.
PREVIEW_CACHE_MAX_BYTES = int(os.environ.get("LOUPE_PREVIEW_CACHE_MAX_MB", "2048")) * 1024 * 1024
PREVIEW_CACHE_LOW_WATER = 0.90          # evict down to this fraction of the cap
_evict_lock = threading.Lock()
# Primed to the cap so the first write after boot always forces a real size check
# -- otherwise lowering the cap would not take effect until 5% of it was rewritten.
_evict_pending = [PREVIEW_CACHE_MAX_BYTES]


def _preview_cache_evict(written):
    """Keep PREVIEW_CACHE under its cap, oldest-accessed JPEG first.

    Cheap by default: the directory is only stat-walked once enough new bytes have
    accumulated to plausibly matter, and the counter is then reset from the real
    total. /data is relatime, so atime is recorded and usable here; mtime is the
    fallback for anything whose atime never moved. A file unlinked while it is
    being served stays readable through the already-open fd, so eviction cannot
    truncate an in-flight response. LOUPE_PREVIEW_CACHE_MAX_MB=0 disables it."""
    if PREVIEW_CACHE_MAX_BYTES <= 0:
        return
    with _evict_lock:
        _evict_pending[0] += written
        if _evict_pending[0] < PREVIEW_CACHE_MAX_BYTES // 20:
            return                      # <5% of the cap written since the last scan
        entries, total = [], 0
        try:
            with os.scandir(PREVIEW_CACHE) as it:
                for e in it:
                    if not e.is_file() or e.name.endswith(".tmp"):
                        continue        # never touch a partial write in flight
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    entries.append((max(st.st_atime, st.st_mtime), st.st_size, e.path))
                    total += st.st_size
        except OSError:
            return
        _evict_pending[0] = 0
        if total <= PREVIEW_CACHE_MAX_BYTES:
            return
        target = int(PREVIEW_CACHE_MAX_BYTES * PREVIEW_CACHE_LOW_WATER)
        entries.sort()                  # least recently accessed first
        freed = 0
        for _, size, path in entries:
            if total - freed <= target:
                break
            try:
                os.remove(path)
                freed += size
            except OSError:
                pass
        print("preview cache: evicted %.1f MB (%.0f -> %.0f MB, cap %.0f MB)"
              % (freed / 1048576.0, total / 1048576.0,
                 (total - freed) / 1048576.0, PREVIEW_CACHE_MAX_BYTES / 1048576.0),
              flush=True)


def preview_path(idv, original=False):
    suffix = "_orig.jpg" if original else ".jpg"
    return os.path.join(PREVIEW_CACHE, f"{idv}{suffix}")


def build_preview(idv, src, original=False):
    """Decode the original image -> cached <=2048px JPEG (once). Returns path or None."""
    if not original:
        src = display_path(idv, src)        # edited render if registered, else the original
    out = preview_path(idv, original=original)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    with _preview_lock:
        ev = _preview_inflight.get(idv)
        first = ev is None
        if first:
            ev = threading.Event(); _preview_inflight[idv] = ev
    if not first:                       # another request is already building this one
        ev.wait(timeout=120)
        return out if (os.path.exists(out) and os.path.getsize(out) > 0) else None
    try:
        with _preview_sem:
            if os.path.exists(out) and os.path.getsize(out) > 0:
                return out
            with open(src, "rb") as f:          # the one NAS read; the result is cached
                data = f.read()
            im = Image.open(io.BytesIO(data))
            im.draft("RGB", (PREVIEW_EDGE, PREVIEW_EDGE))
            # Honour the EXIF orientation flag. Roughly 44% of this library carries a
            # non-normal value -- phones record the sensor's frame and a rotation tag
            # rather than rotating pixels -- so without this the photograph is served
            # sideways or upside down. In a culling app that is not cosmetic: you cannot
            # judge a frame you are looking at edge-on. draft() only scales the DCT, it
            # does not rotate, so this has to be explicit.
            im = ImageOps.exif_transpose(im) or im
            im = im.convert("RGB")
            im.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE))
            tmp = out + ".tmp"
            im.save(tmp, "JPEG", quality=88)
            os.replace(tmp, out)
            try:
                _preview_cache_evict(os.path.getsize(out))
            except Exception as e:                  # eviction must never fail a request
                print("preview cache evict failed: %s: %s" % (type(e).__name__, e), flush=True)
            return out
    except Exception as e:
        print(f"preview fail id={idv}: {type(e).__name__}: {e}", flush=True)
        return None
    finally:
        with _preview_lock:
            _preview_inflight.pop(idv, None)
        ev.set()


# ---------------------------------------------------------------------------
# Faces, Phase 1 — read-only People view + similarity suggestions (faces_api.py).
# Lazy: embeddings load on the first People request, never at import. Crops reuse
# the existing thumb cache (and the hi-res preview only for tiny group faces).
# ---------------------------------------------------------------------------
import faces_api as FACES


def _faces_thumb_path(aid):
    """Ensure + return the local thumb path for an asset (or None)."""
    fp = thumb_path(aid)
    if os.path.exists(fp):
        return fp
    conn = _ro()
    row = conn.execute(
        f"SELECT filepath, extension, duration_seconds FROM assets "
        f"WHERE id=? AND {EXCLUDE_SQL}", (aid,)).fetchone()
    conn.close()
    if not row:
        return None
    _gen_one(aid, row["filepath"], _ext(row) in VIDEO_EXT, row["duration_seconds"])
    return fp if os.path.exists(fp) else None


def _faces_preview_path(aid):
    """Ensure + return the hi-res preview path for an IMAGE asset (or None)."""
    conn = _ro()
    row = conn.execute(
        f"SELECT filepath, extension FROM assets WHERE id=? AND {EXCLUDE_SQL}",
        (aid,)).fetchone()
    conn.close()
    if not row or _ext(row) in VIDEO_EXT:
        return None
    return build_preview(aid, row["filepath"])


FACES.init(
    FACES_DB=os.path.join(APP_DATA, "faces.db"),
    METADATA_DB=METADATA_DB, ENRICH_DB=ENRICH_DB, THUMBS=THUMBS,
    CACHE_DIR=os.path.join(APP_DATA, "cache"),
    thumb_path_fn=_faces_thumb_path, preview_path_fn=_faces_preview_path)


# ---------------------------------------------------------------------------
# Local semantic search, Stage 5 step 4c — text query -> SigLIP2 NN -> asset_id
# list (local_search.py). Flag-gated (_search_settings); lazy: the ~2.7GB text
# model loads on the first /api/search hit, never at import.
# ---------------------------------------------------------------------------
import local_search as SEARCH

SEARCH.init(embeddings_db=os.path.join(HERE, "stage5", "embeddings_siglip2.db"),
            stage5_dir=os.path.join(HERE, "stage5"))


# --- offline reverse geocode (lazy, cached) --------------------------------
_geo_cache = {}
_geo_lock = threading.Lock()


def geocode(lat, lon):
    if lat is None or lon is None:
        return None
    k = (round(lat, 3), round(lon, 3))
    if k in _geo_cache:
        return _geo_cache[k]
    try:
        with _geo_lock:
            import reverse_geocoder as rg
            r = rg.search([(lat, lon)], mode=1)[0]
        name, a1, cc = r.get("name"), r.get("admin1"), r.get("cc")
        city = f"{name}, {STATE_ABBR.get(a1, a1)}" if cc == "US" else f"{name}, {cc}"
    except Exception:
        city = None
    _geo_cache[k] = city
    return city


def _ext(row):
    return (row["extension"] or "").upper()


def _short_path(fp):
    return (fp or "").replace(LIBRARY_ROOT.rstrip(os.sep) + os.sep, "")


def _aspect(row):
    """Displayed aspect ratio (w/h), honouring EXIF orientation.

    width_pixels/height_pixels are the SENSOR frame, not what the viewer sees. For
    orientation 5-8 the image is displayed a quarter turn round, so the displayed aspect
    is the reciprocal. 35,940 assets in this library carry a rotating orientation, so
    ignoring it would lay out a third of the sheet at the wrong shape -- and it would
    look like a rendering bug rather than a data one.

    None when dimensions are missing (0.65% of assets); callers fall back to a square.
    """
    try:
        w = int(row["width_pixels"] or 0)
        h = int(row["height_pixels"] or 0)
    except (TypeError, ValueError, IndexError):
        return None
    if w <= 0 or h <= 0:
        return None
    try:
        if row["orientation"] in (5, 6, 7, 8):
            w, h = h, w
    except (IndexError, KeyError):
        pass
    return round(float(w) / float(h), 4)


def lean(row):
    ext = _ext(row)
    return {"id": row["id"], "year": row["year"], "month": row["month"],
            "ext": ext, "size": row["file_size_bytes"] or 0,
            "is_video": ext in VIDEO_EXT, "dur": row["duration_seconds"],
            "has_gps": row["gps_lat"] is not None,
            "path": _short_path(row["filepath"]), "fpath": row["filepath"],
            "ts": row["capture_timestamp"],
            "bucket": bucket_of(row["year"], row["month"]),   # per-item: Places review spans months
            "blurpct": _pct(BLUR_SORTED, row["blur_laplacian"]),  # sharp/soft read (None if unanalyzed)
            "ar": _aspect(row),                       # justified grid (8.4)
            "state": STATE.get(row["id"], "undecided")}


# ---------------------------------------------------------------------------
# living contact-sheet samples — backdrops for the month/day cards.
# LOCAL ONLY: reads metadata.db (local) and lists the local thumb dir; it NEVER
# touches the NAS or originals, so it can't interfere with the running upload.
# ---------------------------------------------------------------------------
_sample_cache = {}            # (y,m,d) -> [ids]   (stable per period — no reshuffle on poll)
_period_rows_cache = {}       # (y,m)   -> [(id, ts, EXT)]
_thumb_ids = None             # set of ids with a thumbnail in the LOCAL cache
_thumb_ids_ts = 0.0
_thumb_ids_lock = threading.Lock()


def cached_thumb_ids():
    """Ids that have a thumb in the local cache — built by listing the local THUMBS dir
    (never the NAS), refreshed at most every 5 min. The set only grows, so membership
    implies the file is present (no per-request stat, no NAS)."""
    global _thumb_ids, _thumb_ids_ts
    with _thumb_ids_lock:
        if _thumb_ids is None or (time.time() - _thumb_ids_ts) > 300:
            s = set()
            try:
                for f in os.listdir(THUMBS):
                    if f.endswith(".jpg"):
                        try:
                            s.add(int(f[:-4]))
                        except ValueError:
                            pass
            except OSError:
                pass
            _thumb_ids, _thumb_ids_ts = s, time.time()
        return _thumb_ids


def _period_rows(y, m):
    key = (y, m)
    if key in _period_rows_cache:
        return _period_rows_cache[key]
    cond, params = [EXCLUDE_SQL], []
    if y is None:
        cond.append("year IS NULL")
    else:
        cond.append("year=?"); params.append(y)
        cond.append("month=?" if m else "month IS NULL")
        if m:
            params.append(m)
    conn = _ro()
    rows = conn.execute("SELECT id, capture_timestamp, extension FROM assets WHERE "
                        + " AND ".join(cond), params).fetchall()
    conn.close()
    out = [(r["id"], r["capture_timestamp"], (r["extension"] or "").upper())
           for r in rows if r["id"] not in HIDDEN_VIEW_IDS]   # movs + vaulted off card backdrops
    _period_rows_cache[key] = out
    return out


def sample_period(y, m, d, n=6):
    """Up to n random ids for a period's card backdrop. Keepers first (exclude cull
    candidates + PNG screenshots so good photos represent the period), falling back to
    any cached-thumb id if a period is thin. Cached per period so it doesn't reshuffle."""
    key = (y, m, d)
    if key in _sample_cache:
        return _sample_cache[key]
    rows = _period_rows(y, m)
    if d is not None:
        rows = [r for r in rows if day_of(y, m, r[1]) == d]
    cached = cached_thumb_ids()
    period_cached = [r[0] for r in rows if r[0] in cached]
    keepers = [r[0] for r in rows if r[0] in cached and r[0] not in CAND_IDS and r[2] != "PNG"]
    random.shuffle(keepers)
    ks = set(keepers)
    others = [i for i in period_cached if i not in ks]
    random.shuffle(others)
    ids = (keepers + others)[:n]            # keepers first, fall back to other cached thumbs
    _sample_cache[key] = ids
    return ids


def sample_best(y, m, d, n=8):
    """Up to n cached-thumb keeper ids for a period, ordered by Apple aesthetic score
    (APPLE_SCORE, desc) so the Overview hero/mosaic tiles show a month's BEST frames.
    Falls back to the random representative sample when nothing in the period is scored.
    Cached per period (distinct key) so it doesn't reshuffle."""
    key = (y, m, d, "best")
    if key in _sample_cache:
        return _sample_cache[key]
    rows = _period_rows(y, m)
    if d is not None:
        rows = [r for r in rows if day_of(y, m, r[1]) == d]
    cached = cached_thumb_ids()
    keepers = [r[0] for r in rows if r[0] in cached and r[0] not in CAND_IDS and r[2] != "PNG"]
    scored = [i for i in keepers if i in APPLE_SCORE]
    if scored:
        scored.sort(key=lambda i: APPLE_SCORE[i], reverse=True)
        ids = scored[:n]
        if len(ids) < n:                    # top up a thin month with other keepers
            extra = [i for i in keepers if i not in set(ids)]
            random.shuffle(extra)
            ids += extra[:n - len(ids)]
    else:
        ids = sample_period(y, m, d, n)     # nothing scored here -> representative sample
    _sample_cache[key] = ids
    return ids


def portrait_ids(ids):
    """Subset of ids whose frame is portrait (effective height > width, honoring EXIF
    orientation). Read-only on metadata; used to front-load portrait frames in the
    photobook-spread tiles so cover-cropping stays minimal. Cached per id-set."""
    if not ids:
        return set()
    key = ("portrait", tuple(ids))
    if key in _sample_cache:
        return _sample_cache[key]
    conn = _ro()
    q = ("SELECT id, width_pixels, height_pixels, orientation FROM assets "
         "WHERE id IN (%s)" % ",".join("?" * len(ids)))
    rows = conn.execute(q, ids).fetchall()
    conn.close()
    out = set()
    for r in rows:
        w, hgt, o = r["width_pixels"], r["height_pixels"], r["orientation"]
        if not w or not hgt:
            continue
        if o in (5, 6, 7, 8):               # EXIF 90deg rotations swap effective dims
            w, hgt = hgt, w
        if hgt > w:
            out.add(r["id"])
    _sample_cache[key] = out
    return out


# --- photobook portrait selection: fresh + diverse (no near-dups), never starves --------
# Scoped to /api/sample?best=1, which ONLY the month photobook fetches; the discovery
# carousel and every other view use the non-best path and are untouched. READ-ONLY.
PHASH_NEAR = 8       # min Hamming distance (64-bit pHash) between any two shown frames
BURST_S = 8          # min seconds apart -- same-burst backstop when a pHash is missing
BLUR_FLOOR_PCT = 10  # drop the blurriest decile so fresh randomness can't surface mush


def _ham64(a, b):
    """Hamming distance between two 16-hex (64-bit) perceptual hashes; far apart if unparseable."""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def portrait_diverse(y, m, d, n):
    """Up to n photobook frames for a period: a LIGHT-floored pool (no screenshots/docs,
    no blurriest decile), reshuffled with FRESH entropy each call, then a greedy diverse
    pick that rejects visual near-dups (pHash) and same-burst frames (timestamp). Prefers
    portrait (minimal cover-crop), tops up with landscape so the book never starves.
    Returns [(id, is_portrait), ...]; no cache, so every load differs."""
    conn = _ro()
    cond, params = [EXCLUDE_SQL], []
    if y is None:
        cond.append("year IS NULL")
    else:
        cond.append("year=?"); params.append(y)
        cond.append("month=?" if m else "month IS NULL")
        if m:
            params.append(m)
    rows = conn.execute(
        "SELECT id, capture_timestamp AS ts, phash, blur_laplacian AS bl, "
        "width_pixels AS w, height_pixels AS h, orientation AS o, extension AS ext "
        "FROM assets WHERE " + " AND ".join(cond), params).fetchall()
    conn.close()
    cached = cached_thumb_ids()
    portrait_pool, land_pool = [], []
    for r in rows:
        i = r["id"]
        if i in HIDDEN_VIEW_IDS or i not in cached or i in SD_IDS:
            continue                                       # renderable, not a screenshot/doc
        if (r["ext"] or "").upper() == "PNG":
            continue
        if r["bl"] is not None and _pct(BLUR_SORTED, r["bl"]) < BLUR_FLOOR_PCT:
            continue                                       # hard blur
        if d is not None and day_of(y, m, r["ts"]) != d:
            continue
        w, h, o = r["w"], r["h"], r["o"]
        if not w or not h:
            continue
        ew, eh = (h, w) if o in (5, 6, 7, 8) else (w, h)   # EXIF-aware effective dims
        item = {"id": i, "ts": r["ts"] or 0, "ph": r["phash"], "port": eh > ew}
        (portrait_pool if eh > ew else land_pool).append(item)
    random.shuffle(portrait_pool); random.shuffle(land_pool)   # fresh entropy each request

    chosen, seen = [], set()

    def near(c, a, pn, bs):
        if c["ph"] and a["ph"] and _ham64(c["ph"], a["ph"]) <= pn:
            return True
        if c["ts"] and a["ts"] and abs(c["ts"] - a["ts"]) <= bs:
            return True
        return False

    def fill(pool, pn, bs):
        for c in pool:
            if len(chosen) >= n:
                return
            if c["id"] in seen:
                continue
            if all(not near(c, a, pn, bs) for a in chosen):
                chosen.append(c); seen.add(c["id"])

    # portraits first, strict -> progressively relaxed so a homogeneous month never starves
    for pn, bs in ((PHASH_NEAR, BURST_S), (PHASH_NEAR // 2, BURST_S // 2), (2, 2), (0, 0)):
        fill(portrait_pool, pn, bs)
        if len(chosen) >= n:
            break
    # portrait-poor month: top up with landscape, same guard then relaxed
    if len(chosen) < n:
        for pn, bs in ((PHASH_NEAR, BURST_S), (2, 2), (0, 0)):
            fill(land_pool, pn, bs)
            if len(chosen) >= n:
                break
    return [(c["id"], c["port"]) for c in chosen]


# ---------------------------------------------------------------------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _is_local(self):
        if not FULL_RES_LOCAL_ONLY:
            return True
        # Socket-peer trust (not the client-controllable Host header): a spoofed Host can
        # no longer pass. Repairs both callers — the /api/full gate and the __LOCAL__ hint.
        return self._is_lan_peer()

    def _is_lan_peer(self):
        """Stricter gate for the Connect write surface (NOT a substitute for _is_local).
        LAN-local ONLY IF BOTH:
          (a) NO Cloudflare headers — the primary tunnel rejection; the tunnel runs on
              the NAS so its traffic arrives from a LAN IP, and (b) alone can't see that.
          (b) the REAL socket peer (self.client_address) is private/loopback — NOT the
              client-controllable Host header.
        Both required."""
        h = self.headers
        if h.get("CF-Ray") or h.get("CF-Connecting-IP") or h.get("X-Forwarded-For"):
            return False
        try:
            peer = (self.client_address[0] or "").strip()
        except Exception:
            return False
        if peer.startswith("::ffff:"):          # IPv4-mapped IPv6
            peer = peer[7:]
        return peer in ("127.0.0.1", "::1") or bool(_PRIVATE.match(peer))

    def _is_loopback_peer(self):
        """Real socket peer is this host. Same socket-truth rule as _is_lan_peer (never
        the client-controllable Host header), just narrowed from private-LAN to local."""
        try:
            peer = (self.client_address[0] or "").strip()
        except Exception:
            return False
        if peer.startswith("::ffff:"):          # IPv4-mapped IPv6
            peer = peer[7:]
        return peer in ("127.0.0.1", "::1")

    def _write_authorized(self):
        """P2.2 (W23): being on the LAN no longer implies write authority.

        Layered ON TOP of the per-route _is_lan_peer() gates, never instead of them --
        a Cloudflare/guest request still fails _is_lan_peer() and still 403s, so the
        guest tunnel write path is unchanged. Three cases:
          * no token configured -> inert; prior LAN-trust behaviour preserved.
          * loopback peer       -> exempt, so server-local tooling on this host works.
          * any other peer      -> must present the secret in X-Loupe-Write-Token.
        Compared with hmac.compare_digest so the check is not a timing oracle."""
        tok = load_write_token()
        if not tok:
            return True
        if self._is_loopback_peer():
            return True
        return hmac.compare_digest(self.headers.get("X-Loupe-Write-Token") or "", tok)

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _json_gz(self, obj):
        """JSON, gzipped when the client accepts it (the Places payload is a few MB raw)."""
        import gzip as _gz
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if "gzip" in (self.headers.get("Accept-Encoding") or ""):
            b = _gz.compress(b, 6)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _lean_items(self, ids, hide=None):
        """Lean review items for an id list, in the given order. Same shape day() emits,
        so the existing focus/swipe flow consumes it unchanged. `hide` defaults to the full
        view-hide set (movs + vault); the vault view passes HIDDEN_MOV_IDS so it can show
        vaulted assets."""
        if hide is None:
            hide = HIDDEN_VIEW_IDS
        if not ids:
            return {"items": []}
        order = {i: n for n, i in enumerate(ids)}
        conn = _ro()
        items, seen = [], set()
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            qm = ",".join("?" * len(chunk))
            for r in conn.execute(f"SELECT * FROM assets WHERE id IN ({qm})", chunk):
                if r["id"] in seen or r["id"] in hide:
                    continue
                seen.add(r["id"])
                it = lean(r)
                it["thumb"] = os.path.exists(thumb_path(it["id"]))
                it["ascore"] = APPLE_SCORE.get(it["id"])
                it["protected"] = it["id"] in PROTECTED_IDS
                it["is_edit"] = it["id"] in IS_EDIT        # a derived edit of another asset
                it["has_edits"] = it["id"] in HAS_EDITS    # an original that has edit variants
                it["live"] = it["id"] in STILL_TO_MOV
                it["vaulted"] = it["id"] in VAULT_IDS
                items.append(it)
        conn.close()
        items.sort(key=lambda it: order.get(it["id"], 0))
        enqueue_day_thumbs(items)
        return {"items": items}

    def decisions_list(self, q):
        """READ-ONLY: every asset currently marked cut|keep, newest decision first.
        Counts come from the same in-memory STATE mirror global_stats() uses, so the
        section count matches the header stats line exactly. Reuses _lean_items()."""
        state = (q.get("state", ["cut"])[0] or "cut").lower()
        if state not in ("cut", "keep"):
            state = "cut"
        cand = q.get("cand", ["0"])[0] == "1"
        ids = [i for i, st in STATE.items()
               if st == state and (not cand or i in CAND_IDS) and i not in VAULT_IDS]
        ids.sort(reverse=True)
        out = self._lean_items(ids)
        out["state"] = state
        out["count"] = len(ids)
        return out

    def items_by_ids(self, q):
        """Places/map→review handoff: items for an explicit id list (small id sets)."""
        ids = [int(x) for x in (q.get("ids", [""])[0].split(",")) if x.strip().isdigit()]
        return self._lean_items(ids)

    def trip_items(self, q):
        """Trip→sheet/review: items for a trip by INDEX (tiny URL — a 5,658-frame roll's
        id list would blow the GET line length). Reuses the same lean-item builder."""
        try:
            t = PLACES.trips()[int(q.get("i", ["-1"])[0])]
        except (ValueError, IndexError):
            return {"items": []}
        return self._lean_items(t["ids"])

    def do_GET(self):
        u = urlparse(self.path); p = u.path; q = parse_qs(u.query)
        # --- setup / darkroom console (read-only status) ---------------------
        if p == "/setup":
            # 8.5: the darkroom is owner-only. Client-side hiding is not a control, so
            # the gate lives here; the nav button is hidden separately for tidiness.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            return self._html(setup_status.setup_page())
        if p == "/api/setup/status":
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            return self._json(setup_status.status_model())
        if p == "/api/enrich/status":
            job = (q.get("job") or [None])[0]
            st = _enrich_get(job) if job else None
            if st is None:
                return self._json({"error": "unknown job"}, 404)
            return self._json(st)
        if p == "/api/run/status":
            stage = (q.get("stage") or [None])[0]
            st = run_control.status(stage) if stage else None
            if st is None:
                return self._json({"error": "unknown stage"}, 404)
            return self._json(st)
        # --- front-door gate: until the core stages are developed, send the
        #     user to /setup. /setup itself stays reachable either way. --------
        if p in ("/", "/index.html") and not setup_status.is_ready():
            self.send_response(302); self.send_header("Location", "/setup")
            self.end_headers(); return
        if p in ("/", "/index.html", "/trips", "/map", "/cutting-room", "/settings", "/vault", "/people",
                  "/nsfw", "/today", "/calendar"):
            return self._html(PAGE.replace("__LOCAL__", "true" if self._is_local() else "false")
                                   .replace("__SEARCH__", "true" if _search_settings() == "local" else "false"))
        if p == "/places":                      # renamed -> /trips (cheap redirect)
            self.send_response(302); self.send_header("Location", "/trips"); self.end_headers(); return
        if p.startswith("/static/"):
            return self._static(p)
        if p in ("/favicon.ico", "/favicon.svg", "/apple-touch-icon-180.png",
                 "/icon-192.png", "/icon-512.png"):
            return self._static("/static" + p)
        if p == "/api/places":
            return self._json_gz(PLACES.payload())
        if p == "/api/trips":
            return self._json(PLACES.trips())
        # --- Map (place browser): geotagged points + trip overlays. Read-only,
        #     cached in places.py per (from,to); gzipped (the one biggish payload).
        #     Offline geocoder names only — zero Places-API calls. ---------------
        if p == "/api/map/points":
            frm, to = _year_arg(q, "from"), _year_arg(q, "to")
            pts = PLACES.map_points(frm, to)
            m = PLACES.map_meta()
            resp = {"points": pts, "no_gps": m["no_gps"], "mappable": len(pts),
                    "total": m["total"], "home": m["home"], "span": m["span"]}
            if not self._is_lan_peer():
                r_km = _mask_radius_km()
                centers = [rr["center"] for rr in _residences_with_geo() if rr.get("center")]
                masked, _n = _mask_points_near_residences(pts, centers, r_km)
                resp["points"] = masked
                resp["mask_radius_km"] = r_km
            return self._json_gz(resp)
        if p == "/api/map/trips":
            frm, to = _year_arg(q, "from"), _year_arg(q, "to")
            return self._json(PLACES.map_trips(frm, to))
        if p == "/api/items":
            return self._json(self.items_by_ids(q))
        if p == "/api/zeroshot":
            # 9.5 / C4: the precomputed prompt set behind the subtract chips. Membership
            # is static between runs of tools/zero_shot.py, so it is sent once and cached
            # client-side rather than re-queried per sheet.
            zpath = os.path.join(HERE, "stage5", "zeroshot.db")
            if not os.path.exists(zpath):
                # Never generated (or a fresh checkout): the chips simply do not appear.
                return self._json({"cats": {}, "counts": {}, "disabled": True})
            cats, counts = {}, {}
            try:
                zc = sqlite3.connect("file:%s?mode=ro" % zpath, uri=True)
                try:
                    for cat, aid in zc.execute("SELECT cat, asset_id FROM zs ORDER BY cat"):
                        cats.setdefault(cat, []).append(aid)
                finally:
                    zc.close()
            except sqlite3.Error:
                return self._json({"cats": {}, "counts": {}, "disabled": True})
            counts = {k: len(v) for k, v in cats.items()}
            return self._json({"cats": cats, "counts": counts})

        if p == "/api/similar":
            # 9.5 / C4: "the asset's own embedding as query". Costs one extra row read
            # rather than the text model, so this stays cheap even with search disabled
            # for text -- but it is gated the same way, because it reads the same index.
            if _search_settings() != "local":
                return self._json({"items": [], "disabled": True})
            try:
                aid = int((q.get("id", ["0"])[0] or "0").strip())
            except (TypeError, ValueError):
                aid = 0
            if not aid:
                return self._json({"items": []})
            try:
                k = min(max(int((q.get("k") or ["60"])[0]), 1), 200)
            except (TypeError, ValueError):
                k = 60
            pairs = SEARCH.similar(aid, k=k)
            want_all = (q.get("all", ["0"])[0] == "1") and self._is_lan_peer()
            payload = self._lean_items(
                [a for a, _s in pairs],
                hide=(HIDDEN_MOV_IDS if want_all else HIDDEN_VIEW_IDS),
            )
            sim = {a: s for a, s in pairs}
            for it in payload.get("items", []):
                it["sim"] = round(float(sim.get(it["id"], 0.0)), 4)
            return self._json(payload)

        if p == "/api/search":
            # Stage 5 step 4c: text -> SigLIP2 NN -> asset_id list -> lean items (same
            # shape/ordering as /api/items). Flag-gated; disabled never touches the model.
            if _search_settings() != "local":
                return self._json({"items": [], "disabled": True})
            query = (q.get("q", [""])[0] or "").strip()
            if not query:
                return self._json({"items": []})
            try:
                k = min(max(int((q.get("k") or ["60"])[0]), 1), 200)
            except (TypeError, ValueError):
                k = 60
            ids = SEARCH.search(query, k=k)
            want_all = (q.get("all", ["0"])[0] == "1") and self._is_lan_peer()
            return self._json(self._lean_items(ids, hide=(HIDDEN_MOV_IDS if want_all else HIDDEN_VIEW_IDS)))
        if p == "/api/trip_items":
            return self._json(self.trip_items(q))
        if p == "/api/residences":
            res = _residences_with_geo()
            if not self._is_lan_peer():
                res = [{k: v for k, v in r.items() if k != "center"} for r in res]
            return self._json({"residences": res})
        if p == "/api/place_names":
            return self._json({"places": PLACES.place_names()})
        if p == "/api/vault_items":          # the gated vault view: show ONLY vaulted (movs still hidden)
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ids = sorted(VAULT_IDS, reverse=True)
            return self._json(self._lean_items(ids, hide=HIDDEN_MOV_IDS))
        if p == "/api/nsfw_items":           # OWNER-ONLY Closed Set: nudity-screen pile OR production facet
            # The Closed Set is the opposite of something to show a guest — LAN-gate it (both facets).
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            if (q.get("facet", [""])[0] or "").strip() == "production":
                # Production work-product facet: a PLAIN held-aside grid. NO nudity semantics
                # (no score / threshold / clear). Only the live-mov hide applies, so the set
                # itself renders. Distinct facet, same LAN-gated owner surface.
                ids = sorted(PRODUCTION_IDS, reverse=True)
                out = self._lean_items(ids, hide=HIDDEN_MOV_IDS)
                out["facet"] = "production"
                return self._json(out)
            ids = sorted(NSFW_IDS, reverse=True)
            out = self._lean_items(ids, hide=HIDDEN_MOV_IDS)
            items = out.get("items", [])
            # attach each item's raw max_score (read-only; placeholder-bound, never frozenset-in-SQL)
            # so the console can show + sort by it — the knee is visible as you scroll.
            if items and os.path.exists(NSFW_DB):
                want = [it["id"] for it in items]
                sc = {}
                try:
                    c = ro(NSFW_DB)
                    try:
                        for i in range(0, len(want), 900):
                            chunk = want[i:i + 900]
                            qm = ",".join("?" * len(chunk))
                            for aid, ms in c.execute(
                                    f"SELECT asset_id, max_score FROM scores WHERE asset_id IN ({qm})", chunk):
                                sc[aid] = ms
                    finally:
                        c.close()
                except Exception:
                    sc = {}
                for it in items:
                    it["score"] = sc.get(it["id"])
                items.sort(key=lambda it: (it.get("score") if it.get("score") is not None else -1.0),
                           reverse=True)   # high -> low; this route is consumed only by /nsfw
            out["threshold"] = _nsfw_settings()[0]   # surface current threshold so the slider binds to it
            return self._json(out)
        if p == "/api/nsfw/config":          # OWNER-ONLY: state for the Settings section (no item list)
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            thr, _cl = _nsfw_settings()
            try:
                with open(SETTINGS_PATH) as f:
                    en = bool(json.load(f).get("nsfw_enabled"))
            except Exception:
                en = False
            return self._json({"nsfw_enabled": en, "nsfw_threshold": thr, "flagged": len(NSFW_IDS)})
        if p == "/api/overview":
            return self._json(self.overview(q))
        if p == "/api/month":
            return self._json(self.month(q))
        if p == "/api/day":
            return self._json(self.day(q))
        if p == "/api/on-this-day":
            return self._json(self.on_this_day(q))
        if p == "/api/calendar":
            return self._json(self.calendar(q))
        if p == "/api/decisions":
            return self._json(self.decisions_list(q))
        if p.startswith("/api/item/"):
            return self._json(self.item(int(p.rsplit("/", 1)[1])))
        if p == "/api/export":
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            return self._json(self.export_month(q))
        if p == "/api/export-candidates/preflight":
            # 9.4: the count and the live protection re-check, before the door opens.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            return self._json(self.export_preflight())
        if p == "/api/export-candidates":
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            return self._json(self.export_candidates())
        if p == "/api/stats":
            return self._json(global_stats(q.get("mode", ["lib"])[0] == "cand"))
        if p == "/api/cutting-room":
            # the NSFW chip is owner-only; a guest (tunnel) gets the same 6-rule model as before
            return self._json(cutting_room_model(include_nsfw=self._is_lan_peer()))
        if p == "/api/cutting-room/ids":
            key = (q.get("rule", [""])[0] or "").strip()
            ids = _cr_ids(key) if key in CR_ORDER else []
            out = {"rule": key, "ids": ids}
            # 9.4's caption is "BURST EXTRA · 5 OF 7" -- the frame's place in its own
            # burst. B3 already carries cluster_id/sharp_rank/cluster_size per frame
            # from the pipeline; it simply was not reaching the client.
            if key == "B3" and ids:
                pos = {}
                for i in ids:
                    m = ((CAND.get(i) or {}).get("m") or {}).get("B3") or {}
                    if m.get("rank") and m.get("csize"):
                        pos[i] = [m["rank"], m["csize"]]
                if pos:
                    out["pos"] = pos
            return self._json(out)
        if p == "/api/sample":
            return self._json(self.sample(q))
        if p == "/api/summary":
            # The venue+LLM upgrade (gen=1) spends API quota + writes summaries.db — LAN-only.
            # The plain cached read (no gen) stays public.
            if q.get("gen") and not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            return self._json(self.summary(q))
        # --- Faces, Phase 1: read-only People view + similarity suggestions ----
        if p == "/api/people":
            return self._json(FACES.people_list())
        if p == "/api/person":
            return self._json(FACES.person_detail(int((q.get("id") or ["0"])[0])))
        if p == "/api/person/suggestions":
            return self._json(FACES.suggestions(
                int((q.get("id") or ["0"])[0]), int((q.get("k") or ["100"])[0])))
        if p == "/api/cluster/faces":
            # D7 / 9.3 wants twelve exemplars; clusters.rep_face_ids stores four. The
            # members live in cluster_faces with cos_to_centroid, so the sheet is drawn
            # from there instead.
            #
            # They are spread ACROSS the cohesion range rather than taken from the top.
            # The twelve most central faces of a contaminated cluster all look alike --
            # they are the cluster's own idea of itself -- so a top-12 sheet would
            # systematically hide the one failure the question "same person?" exists to
            # catch. Sampling evenly shows the edges, where a second person would be.
            if not self._is_lan_peer():
                return self._json({"faces": [], "error": "owner only"}, code=403)
            try:
                cid = int((q.get("cluster_id", ["0"])[0] or "0").strip())
            except (TypeError, ValueError):
                cid = 0
            try:
                k = min(max(int((q.get("k") or ["12"])[0]), 1), 40)
            except (TypeError, ValueError):
                k = 12
            cpath = os.path.join(APP_DATA, "clusters.db")
            if not cid or not os.path.exists(cpath):
                return self._json({"faces": []})
            try:
                cdb = sqlite3.connect("file:%s?mode=ro" % cpath, uri=True)
                try:
                    rows = cdb.execute(
                        "SELECT face_id, cos_to_centroid FROM cluster_faces"
                        " WHERE cluster_id = ? ORDER BY cos_to_centroid DESC",
                        (cid,),
                    ).fetchall()
                finally:
                    cdb.close()
            except sqlite3.Error:
                return self._json({"faces": []})
            if not rows:
                return self._json({"faces": []})
            n = len(rows)
            if n <= k:
                picked = rows
            else:
                picked = [rows[(i * (n - 1)) // (k - 1)] for i in range(k)] if k > 1 else [rows[0]]
            cohesion = sum(r[1] for r in rows) / float(n)
            return self._json({
                "faces": [r[0] for r in picked],
                "members": n,
                "cohesion": round(float(cohesion), 4),
                "tightest": round(float(rows[0][1]), 4),
                "loosest": round(float(rows[-1][1]), 4),
            })

        if p == "/api/people/candidates":
            # Stage 1a: ranked unnamed clusters for the "name a person" surface.
            # Owner-only labeling data (unnamed strangers' crops) — LAN-gated.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            try:
                strict = (q.get("strict") or ["0"])[0] == "1"
                lim = int((q.get("limit") or ["25"])[0])
            except ValueError:
                return self._json({"error": "bad params"}, 400)
            return self._json(FACES.candidates(strict=strict, limit=lim))
        if p.startswith("/api/face/"):
            return self._face(p)
        if p.startswith("/thumb/"):
            return self._thumb(p, q)
        if p.startswith("/api/full/"):
            return self._full(p)
        if p.startswith("/api/play/"):
            return self._play(p)
        if p == "/api/edits" or p.startswith("/api/edits?"): 
            if not RENDER_PATHS:
                return self._json([])
            conn = _ro()
            rows = conn.execute("SELECT id, extension FROM assets WHERE id IN (" + ",".join(map(str, RENDER_PATHS.keys())) + ")").fetchall()
            conn.close()
            return self._json([r["id"] for r in rows if (r["extension"] or "").upper() not in VIDEO_EXT])
        if p.startswith("/api/preview/"):
            return self._preview(p)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        # P2.2 (W23) -- ONE chokepoint. Every write route below inherits this gate, so
        # a new POST branch is covered by construction rather than by remembering to
        # add a 17th check. Deliberately before any body read: an unauthorized peer
        # never gets to send us a payload.
        if not self._write_authorized():
            return self._json({"error": "write token required"}, 403)
        if urlparse(self.path).path == "/api/decide":
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}")
            st = body.get("state", "undecided")
            if st not in ("keep", "cut", "undecided"):
                return self._json({"error": "bad state"}, 400)
            # client sends [{id, bucket}] so we never re-query metadata to write
            pairs = [(int(x["id"]), x.get("bucket")) for x in body.get("items", [])]
            if not pairs and body.get("ids"):  # tolerate id-only payloads
                pairs = [(int(i), None) for i in body["ids"]]
            set_decision(pairs, st)
            return self._json({"ok": True, "global": global_stats(bool(body.get("cand")))})
        if urlparse(self.path).path == "/api/residences":
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}")
            res = body.get("residences", [])
            if not isinstance(res, list):
                return self._json({"error": "bad payload"}, 400)
            save_residences(res)                 # writes ONLY the settings store; clears places caches
            return self._json({"ok": True, "count": len(res)})
        if urlparse(self.path).path == "/api/vault":
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}")
            idv = int(body.get("id"))
            on = body.get("action", "mark") != "unmark"
            vault_set(idv, on, body.get("note"))   # writes ONLY vault.db; re-derives the hide-layer
            # ---- PHYSICAL MOVE: DEFERRED (prototype is flag-and-hide only) -------------------
            # When promoted to a real vault, a vaulted asset's original would be relocated to a
            # private subtree on the SAME volume (atomic os.rename, no copy) and metadata.db's
            # filepath repointed — reusing the proven long-videos move pattern. Gated on two
            # pending decisions (backup policy + move timing). Intentionally a NO-OP here:
            # _vault_relocate(idv, on)   # <- stub; moves nothing, repoints nothing in the prototype
            return self._json({"ok": True, "id": idv, "vaulted": on, "vault_count": len(VAULT_IDS)})
        if urlparse(self.path).path == "/api/setup/library":
            # Connect Phase 1: persist the library SOURCE + ROOT. LAN-only write surface.
            # Captures no credentials, invokes no icloudpd, triggers no pipeline.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            source = body.get("source")
            if source not in ("existing", "icloud"):
                return self._json({"error": "bad source"}, 400)
            if source == "icloud":
                # Inert in this phase — sign-in arrives later; we never persist iCloud here.
                return self._json({"error": "iCloud sign-in arrives in a later pass"}, 400)
            root = (body.get("library_root") or "").strip()
            ok, err = validate_library_path(root)
            if not ok:
                return self._json({"ok": False, "error": err}, 400)
            save_library_choice("existing", root)   # writes ONLY loupe-settings.json
            return self._json({"ok": True, "source": "existing", "library_root": root})
        if urlparse(self.path).path == "/api/settings/nsfw":
            # Opt-in: enable on-device NSFW screening. LAN-only OWNER surface — a tunnel/
            # guest viewer is rejected exactly like /api/setup/library (the owner can't be
            # a guest). Writes only the flag; the scan itself is the existing run_control stage.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            enabled = bool(body.get("enabled"))
            save_nsfw_enabled(enabled)               # locked whole-object merge; other keys intact
            _rebuild_nsfw_indices()                  # compose/decompose the global hide on toggle
            _period_rows_cache.clear(); _sample_cache.clear()   # backdrops/samples follow the suppression
            return self._json({"ok": True, "nsfw_enabled": enabled})
        if urlparse(self.path).path == "/api/settings/nsfw_threshold":
            # Calibration sweep lever — re-derives NSFW_IDS at a new threshold, no rescan.
            # LAN-only owner surface (the flagged set is owner-private).
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            try:
                thr = float(body.get("threshold"))
            except (TypeError, ValueError):
                return self._json({"error": "bad threshold"}, 400)
            save_nsfw_threshold(thr)                  # locked merge; other keys intact
            _rebuild_nsfw_indices()
            _period_rows_cache.clear(); _sample_cache.clear()   # backdrops/samples re-pick at the new threshold
            return self._json({"ok": True, "nsfw_threshold": thr, "flagged": len(NSFW_IDS)})
        if urlparse(self.path).path == "/api/nsfw/clear":
            # Owner's per-image false-positive escape hatch: add/remove from nsfw_cleared.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            try:
                aid = int(body.get("asset_id"))
            except (TypeError, ValueError):
                return self._json({"error": "bad asset_id"}, 400)
            cleared = bool(body.get("cleared"))
            nsfw_clear_set(aid, cleared)              # locked merge; other keys intact
            _rebuild_nsfw_indices()
            _period_rows_cache.clear(); _sample_cache.clear()   # a cleared frame reappears in backdrops/samples
            return self._json({"ok": True, "asset_id": aid, "cleared": cleared,
                               "flagged": len(NSFW_IDS)})
        if urlparse(self.path).path == "/api/connect/start":
            # Connect 2a: iCloud sign-in step 1 (Apple ID + REAL password). LAN-only.
            # Password is handed to icloudpd over stdin and dropped; never argv/log/disk.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            apple_id = (body.get("apple_id") or "").strip()
            password = body.get("password") or ""
            if not apple_id or not password:
                return self._json({"state": "error",
                                   "message": "Apple ID and password are required."}, 400)
            res = icloud_connect.start_auth(apple_id, password)
            password = None; del password          # drop our reference immediately
            code = 200 if res.get("state") in ("requires_2fa", "authenticated") else 400
            return self._json(res, code)
        if urlparse(self.path).path == "/api/connect/2fa":
            # Connect 2a: iCloud sign-in step 2 — submit the 6-digit code. LAN-only.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            res = icloud_connect.submit_2fa(body.get("token"), body.get("code"))
            code = 200 if res.get("state") == "authenticated" else 400
            return self._json(res, code)
        # --- Faces, Phase 2: confirm / reject (writes faces.db assignments +
        #     rejections ONLY — never the faces/embedding rows) -----------------
        if urlparse(self.path).path == "/api/person/confirm":
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
                pid = int(body.get("person_id"))
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            faces = body.get("faces") or []
            if not isinstance(faces, list):
                return self._json({"error": "bad payload"}, 400)
            return self._json(FACES.confirm(pid, faces))
        if urlparse(self.path).path == "/api/person/reject":
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
                pid = int(body.get("person_id"))
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            fids = body.get("face_ids") or []
            if not isinstance(fids, list):
                return self._json({"error": "bad payload"}, 400)
            return self._json(FACES.reject(pid, fids))
        if urlparse(self.path).path == "/api/person/create":
            # Stage 1a: name an unnamed cluster — writes faces.db persons +
            # assignments (source='cluster') and claims the cluster in the
            # clusters.db sidecar. Mirrors /api/person/confirm gating.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            res = FACES.person_create(body.get("name"), body.get("cluster_id"))
            return self._json(res, 400 if res.get("error") else 200)
        if urlparse(self.path).path == "/api/person/assign_cluster":
            # Stage 1c: fold an unnamed cluster into an EXISTING person (the
            # autocomplete "Add to [Name]" path) — assignments + cluster claim,
            # no new persons row. Mirrors /api/person/create gating.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            res = FACES.person_assign_cluster(
                body.get("person_id"), body.get("cluster_id"))
            return self._json(res, 400 if res.get("error") else 200)
        if urlparse(self.path).path == "/api/people/candidates/dismiss":
            # Stage 1b: "not a person" — flags the cluster in the clusters.db
            # sidecar only (candidates skip it like a claimed cluster);
            # faces.db is never touched. Mirrors /api/person/create gating.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            res = FACES.candidate_dismiss(body.get("cluster_id"))
            return self._json(res, 400 if res.get("error") else 200)
        if urlparse(self.path).path == "/api/enrich/import":
            # Enrichment import: accept a Mac bundle (raw application/octet-stream body),
            # stage it, and build/swap/reload OFF the request thread. LAN-only write surface.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            if ln <= 0:
                return self._json({"error": "empty body"}, 400)
            if ln > ENRICH_MAX_BYTES:
                return self._json({"error": "bundle too large"}, 413)
            job_id = os.urandom(8).hex()
            tgz_path = os.path.join(ENRICH_IMPORT_DIR, f"{job_id}.tgz")
            try:
                os.makedirs(ENRICH_IMPORT_DIR, exist_ok=True)
                remaining = ln
                with open(tgz_path, "wb") as f:
                    while remaining > 0:
                        chunk = self.rfile.read(min(remaining, 1 << 20))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
            except Exception as e:
                return self._json({"error": f"upload failed: {e}"}, 400)
            if remaining > 0:                         # short read -> client hung up
                try:
                    os.remove(tgz_path)
                except Exception:
                    pass
                return self._json({"error": "incomplete upload"}, 400)
            _enrich_set(job_id, state="building", received_bytes=ln)
            _pool.submit(_enrich_worker, job_id, tgz_path)
            return self._json({"job_id": job_id, "state": "building"})
        if urlparse(self.path).path == "/api/run/start":
            # Compute-stage trigger (e.g. Develop/ingest): detached, single-flighted,
            # resumable. LAN-only write surface. Spawns NOTHING for an already-running
            # stage; the detached runner owns the run + its durable marker.
            if not self._is_lan_peer():
                return self._json({"error": "LAN only"}, 403)
            ln = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                return self._json({"error": "bad payload"}, 400)
            res = run_control.start(body.get("stage"))
            if res is None:
                return self._json({"error": "unknown stage"}, 400)
            return self._json(res)
        return self._json({"error": "not found"}, 404)

    # ---- data endpoints ----
    def overview(self, q):
        cand = q.get("mode", ["lib"])[0] == "cand"
        conn = _ro()
        rows = conn.execute(
            f"SELECT year, month, count(*) c FROM assets WHERE {EXCLUDE_SQL} "
            f"GROUP BY year, month").fetchall()
        conn.close()
        dec = decisions_by_bucket(cand)
        cutsz = cut_sizes_by_bucket(cand)
        years = defaultdict(list)
        undated = None
        for r in rows:
            y, m = r["year"], r["month"]
            bk = bucket_of(y, m)
            total = (CAND_BUCKET_TOTAL.get(bk, 0) if cand
                     else (r["c"] - HIDDEN_BY_BUCKET.get(bk, 0))) - VAULT_BY_BUCKET.get(bk, 0) \
                     - (0 if cand else NSFW_BY_BUCKET.get(bk, 0)) \
                     - (0 if cand else PROD_BY_BUCKET.get(bk, 0))   # nsfw + production held aside off month totals
            if cand and total == 0:
                continue                      # no candidates in this month
            d = dec.get(bk, {"decided": 0, "cut": 0, "keep": 0})
            ccount, cbytes = cutsz.get(bk, (0, 0))
            state = ("untouched" if d["decided"] == 0
                     else ("complete" if d["decided"] >= total else "in-progress"))
            cell = {"total": total, "decided": d["decided"], "cut": d["cut"],
                    "keep": d["keep"], "state": state, "bucket": bk,
                    "cut_gb": round(cbytes / 1e9, 2),
                    "pct": round(100 * d["decided"] / total, 1) if total else 0}
            if y is None:
                cell.update({"y": None, "m": None, "label": "Undated", "undated": True})
                undated = cell
            else:
                cell.update({"y": y, "m": m or 0,
                             "label": (MONTHS[m] if m and 1 <= m <= 12 else "Unknown")})
                years[y].append(cell)
        out = []
        for y in sorted(years, reverse=True):
            months = sorted(years[y], key=lambda c: -c["m"])
            tot = sum(c["total"] for c in months)
            d = sum(c["decided"] for c in months)
            out.append({"year": y, "total": tot, "decided": d,
                        "pct": round(100 * d / tot, 1) if tot else 0, "months": months})
        return {"years": out, "undated": undated, "summary": global_stats(cand)}

    def _month_rows(self, q, include_hidden=False):
        """Return (label, y, m, undated_flag, list-of-rows) for a month request.
        Paired Live-Photo motion MOVs are hidden by default (the still represents the
        pair); include_hidden=True keeps them (day-view reveal toggle)."""
        conn = _ro()
        if q.get("undated"):
            rows = conn.execute(
                f"SELECT * FROM assets WHERE year IS NULL AND {EXCLUDE_SQL}").fetchall()
            conn.close()
            return "Undated", None, None, True, self._drop_hidden(rows, include_hidden)
        y = int(q.get("y", [0])[0]); m = int(q.get("m", [0])[0])
        if m:
            rows = conn.execute(
                f"SELECT * FROM assets WHERE year=? AND month=? AND {EXCLUDE_SQL}",
                (y, m)).fetchall()
        else:
            rows = conn.execute(
                f"SELECT * FROM assets WHERE year=? AND month IS NULL AND {EXCLUDE_SQL}",
                (y,)).fetchall()
        conn.close()
        label = f"{(MONTHS[m] if 1 <= m <= 12 else 'Unknown')} {y}" if m else f"{y} (month?)"
        return label, y, m, False, self._drop_hidden(rows, include_hidden)

    @staticmethod
    def _drop_hidden(rows, include_hidden):
        # vaulted assets are ALWAYS dropped (even the day-view reveal toggle only reveals movs)
        if include_hidden:
            return [r for r in rows if r["id"] not in VAULT_IDS] if VAULT_IDS else rows
        if not HIDDEN_VIEW_IDS:
            return rows
        return [r for r in rows if r["id"] not in HIDDEN_VIEW_IDS]

    def month(self, q):
        label, y, m, undated, rows = self._month_rows(q)
        if q.get("mode", ["lib"])[0] == "cand":
            rows = [r for r in rows if r["id"] in CAND_IDS]
        # bucket rows into weeks → days (counts + pct only)
        weeks = defaultdict(lambda: defaultdict(list))  # wk -> day -> [ids]
        for r in rows:
            day = 0 if undated else day_of(y, m, r["capture_timestamp"])
            wk = 9 if day == 0 else min(4, (day - 1) // 7)
            weeks[wk][day].append(r["id"])
        out_weeks = []
        for wk in sorted(weeks):
            days = []
            for day in sorted(weeks[wk]):
                ids = weeks[wk][day]
                dec = sum(1 for i in ids if i in STATE)
                cut = sum(1 for i in ids if STATE.get(i) == "cut")
                total = len(ids)
                days.append({"day": day, "total": total, "decided": dec, "cut": cut,
                             "state": ("untouched" if dec == 0 else
                                       ("complete" if dec >= total else "in-progress")),
                             "pct": round(100 * dec / total, 1) if total else 0,
                             "label": (f"day {day}" if day else "no specific day")})
            wtot = sum(d["total"] for d in days)
            wdec = sum(d["decided"] for d in days)
            out_weeks.append({"key": wk, "label": WK_LABELS[wk], "days": days,
                              "total": wtot, "decided": wdec,
                              "pct": round(100 * wdec / wtot, 1) if wtot else 0})
        allids = [r["id"] for r in rows]
        dec = sum(1 for i in allids if i in STATE)
        return {"label": label, "y": y, "m": m, "undated": bool(undated),
                "weeks": out_weeks,
                "summary": {"total": len(allids), "decided": dec,
                            "cut": sum(1 for i in allids if STATE.get(i) == "cut"),
                            "keep": sum(1 for i in allids if STATE.get(i) == "keep"),
                            "pct": round(100 * dec / len(allids), 1) if allids else 0}}

    def day(self, q):
        reveal = bool(q.get("showpairs"))      # optional: reveal hidden Live motion clips
        label, y, m, undated, rows = self._month_rows(q, include_hidden=reveal)
        cand = q.get("mode", ["lib"])[0] == "cand"
        want = int(q.get("d", [0])[0])
        items = []
        for r in rows:
            day = 0 if undated else day_of(y, m, r["capture_timestamp"])
            if day != want:
                continue
            if cand and r["id"] not in CAND_IDS:
                continue
            it = lean(r)
            it["thumb"] = os.path.exists(thumb_path(it["id"]))
            it["ascore"] = APPLE_SCORE.get(it["id"])      # aesthetic pip + worst-first sort
            it["protected"] = it["id"] in PROTECTED_IDS    # bulk-cut guard flag
            it["is_edit"] = it["id"] in IS_EDIT            # a derived edit of another asset
            it["has_edits"] = it["id"] in HAS_EDITS        # an original that has edit variants
            it["live"] = it["id"] in STILL_TO_MOV          # still has a paired motion clip
            if it["id"] in MOV_TO_STILL:                   # only when revealed
                it["live_mov_of"] = MOV_TO_STILL[it["id"]]
            if cand:
                c = CAND[it["id"]]
                it["rules"] = c["rules"]; it["m"] = c["m"]; it["rule"] = primary_rule(c["rules"])
            items.append(it)
        items.sort(key=lambda it: (it["ts"] or 0, it["id"]))
        enqueue_day_thumbs(items)   # on-demand: generate this day's missing thumbs now
        bk = "undated" if undated else bucket_of(y, m)
        if want:
            daylabel = f"{(MONTHS[m] if m and 1 <= m <= 12 else '')} {want}, {y}".strip()
            wk = min(4, (want - 1) // 7)
            wklabel = WK_LABELS[wk]
        else:
            daylabel = "no specific day"
            wklabel = "no specific day"
        dec = sum(1 for it in items if it["state"] != "undecided")
        cut = sum(1 for it in items if it["state"] == "cut")
        return {"label": daylabel, "wklabel": wklabel, "monthlabel": label,
                "y": y, "m": m, "d": want, "undated": bool(undated), "bucket": bk,
                "items": items,
                "summary": {"total": len(items), "decided": dec, "cut": cut,
                            "pct": round(100 * dec / len(items), 1) if items else 0}}

    def on_this_day(self, q):
        """Every asset ever captured on this (month, day), across every year --
        the cross-year sibling of day(). Same item shape, same decide flow
        (each item still carries its own real year-month bucket via lean())."""
        reveal = bool(q.get("showpairs"))
        cand = q.get("mode", ["lib"])[0] == "cand"
        try:
            m = int(q.get("m", [0])[0]); d = int(q.get("d", [0])[0])
        except (TypeError, ValueError):
            m = d = 0
        if not (1 <= m <= 12 and 1 <= d <= 31):
            return {"error": "bad m/d"}
        conn = _ro()
        rows = conn.execute(
            f"SELECT * FROM assets WHERE {EXCLUDE_SQL} "
            f"AND strftime('%m', capture_timestamp, 'unixepoch', 'localtime')=? "
            f"AND strftime('%d', capture_timestamp, 'unixepoch', 'localtime')=?",
            (f"{m:02d}", f"{d:02d}")).fetchall()
        conn.close()
        rows = self._drop_hidden(rows, reveal)
        by_year = defaultdict(list)
        for r in rows:
            if cand and r["id"] not in CAND_IDS:
                continue
            it = lean(r)
            it["thumb"] = os.path.exists(thumb_path(it["id"]))
            it["ascore"] = APPLE_SCORE.get(it["id"])
            it["protected"] = it["id"] in PROTECTED_IDS
            it["is_edit"] = it["id"] in IS_EDIT
            it["has_edits"] = it["id"] in HAS_EDITS
            it["live"] = it["id"] in STILL_TO_MOV
            if it["id"] in MOV_TO_STILL:
                it["live_mov_of"] = MOV_TO_STILL[it["id"]]
            if cand:
                c = CAND[it["id"]]
                it["rules"] = c["rules"]; it["m"] = c["m"]; it["rule"] = primary_rule(c["rules"])
            by_year[r["year"]].append(it)
        # Flat items array (matches day()'s shape so the client's day-grid/focus-mode/
        # decide code -- all keyed off day.items -- works unmodified). `years` carries
        # only lightweight per-year subtotals, not a second copy of the items.
        all_items, years = [], []
        for y in sorted(by_year, key=lambda y: (y is None, -(y or 0))):   # newest year first, undated last
            yitems = sorted(by_year[y], key=lambda it: (it["ts"] or 0, it["id"]))
            all_items.extend(yitems)
            ydec = sum(1 for it in yitems if it["state"] != "undecided")
            ycut = sum(1 for it in yitems if it["state"] == "cut")
            years.append({"y": y, "count": len(yitems), "decided": ydec, "cut": ycut,
                          "pct": round(100 * ydec / len(yitems), 1) if yitems else 0})
        enqueue_day_thumbs(all_items)   # on-demand: generate this date's missing thumbs, all years
        dec = sum(1 for it in all_items if it["state"] != "undecided")
        cut = sum(1 for it in all_items if it["state"] == "cut")
        return {"label": f"{MONTHS[m]} {d}", "m": m, "d": d, "items": all_items, "years": years,
                "summary": {"total": len(all_items), "decided": dec, "cut": cut,
                            "pct": round(100 * dec / len(all_items), 1) if all_items else 0}}

    def calendar(self, q):
        """366-cell completion overview: every calendar day's total/decided/cut/pct,
        aggregated across every year that day ever occurred. Same untouched /
        in-progress / complete convention as month(). Computed fresh per request
        (like month()/day()) rather than cached -- a full-table scan here costs
        ~0.1s, so a startup cache would only add staleness risk for no real gain."""
        cand = q.get("mode", ["lib"])[0] == "cand"
        conn = _ro()
        rows = conn.execute(
            f"SELECT id, capture_timestamp FROM assets "
            f"WHERE {EXCLUDE_SQL} AND capture_timestamp IS NOT NULL").fetchall()
        conn.close()
        ts_of = {}
        totals = defaultdict(int)
        for r in rows:
            idv = r["id"]
            if idv in HIDDEN_VIEW_IDS or (cand and idv not in CAND_IDS):
                continue
            doy = day_of_year(r["capture_timestamp"])
            if not doy:
                continue
            ts_of[idv] = r["capture_timestamp"]
            totals[mmdd(*doy)] += 1
        decided, cut = defaultdict(int), defaultdict(int)
        for idv, st in STATE.items():
            if idv in VAULT_IDS or idv not in ts_of:
                continue
            key = mmdd(*day_of_year(ts_of[idv]))
            decided[key] += 1
            if st == "cut":
                cut[key] += 1
        days = []
        for m in range(1, 13):
            maxd = 29 if m == 2 else (30 if m in (4, 6, 9, 11) else 31)
            for d in range(1, maxd + 1):
                key = mmdd(m, d)
                total, dec = totals.get(key, 0), decided.get(key, 0)
                days.append({"m": m, "d": d, "label": f"{MONTHS[m]} {d}",
                             "total": total, "decided": dec, "cut": cut.get(key, 0),
                             "state": ("untouched" if dec == 0 else
                                       ("complete" if dec >= total else "in-progress")),
                             "pct": round(100 * dec / total, 1) if total else 0})
        total_all, dec_all = sum(totals.values()), sum(decided.values())
        return {"days": days,
                "summary": {"total": total_all, "decided": dec_all,
                            "pct": round(100 * dec_all / total_all, 1) if total_all else 0}}

    def item(self, idv):
        conn = _ro()
        row = conn.execute("SELECT * FROM assets WHERE id=?", (idv,)).fetchone()
        conn.close()
        if not row:
            return {"error": "no such id"}
        d = dict(row)
        ts = d.get("capture_timestamp")
        if ts:
            lt = time.localtime(ts)
            when = time.strftime("%Y-%m-%d %H:%M", lt)
            weekday = WEEKDAYS[lt.tm_wday]
        else:
            when, weekday = "no capture time", "—"
        place = None
        gps = None
        if d.get("gps_lat") is not None and d.get("gps_lon") is not None:
            lat, lon = d["gps_lat"], d["gps_lon"]
            place = geocode(lat, lon)
            gps = {"city": place, "lat": round(lat, 5), "lon": round(lon, 5),
                   "map": f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=14/{lat}/{lon}"}
        ext = _ext(d)
        is_video = ext in VIDEO_EXT
        cam = " ".join(x for x in (d.get("camera_make"), d.get("camera_model")) if x) or "—"
        w, h = d.get("width_pixels"), d.get("height_pixels")
        dims = f"{w}×{h} ({w*h/1e6:.1f} MP)" if w and h else "—"
        size = (d.get("file_size_bytes") or 0) / 1e6
        lp = "—"
        if d.get("is_live_photo_video"): lp = "Live Photo (video half)"
        elif d.get("is_live_photo_still"): lp = "Live Photo (still half)"
        elif d.get("live_photo_partner_id"): lp = "Live Photo (paired)"
        # capture context leads (when/where is the keep/cut anchor for own photos)
        context = {"when": when, "weekday": weekday, "place": place or
                   ("(located, uncoded)" if gps else None)}
        exif = [
            ["camera", cam],
            ["dimensions", dims],
            ["format", f"{ext} · {size:.1f} MB"],
        ]
        if is_video and d.get("duration_seconds"):
            exif.append(["duration", f"{d['duration_seconds']:.1f} s"])
        exif += [["live photo", lp],
                 ["shared album", "yes" if d.get("is_shared_album") else "no"],
                 ["path", _short_path(d.get("filepath"))]]
        hints = []
        if d.get("blur_laplacian") is not None:
            hints.append(["blur score", f"{d['blur_laplacian']:.0f}"])
        if is_video and d.get("duration_seconds"):
            hints.append(["clip length", f"{d['duration_seconds']:.1f} s"])
        full = [
            ["id", str(idv)],
            ["sha256", (d.get("file_sha256") or "—")[:16]],
            ["phash", d.get("phash") or "—"],
            ["lens", d.get("lens_model") or "—"],
            ["iso / shutter / f", f"{d.get('iso') or '—'} · {d.get('shutter_speed') or '—'} · {d.get('aperture') or '—'}"],
            ["full path", d.get("filepath") or "—"],
        ]
        out = {"id": idv, "is_video": is_video,
               "state": STATE.get(idv, "undecided"),
               "context": context, "exif": exif, "hints": hints,
               "gps": gps, "full": full,
               "signal": enrichment(idv, d.get("blur_laplacian")),
               "thumb": os.path.exists(thumb_path(idv))}
        if idv in STILL_TO_MOV:                   # Live Photo: motion clip plays on demand
            out["live"] = {"mov_id": STILL_TO_MOV[idv]}
        if idv in EDIT_GROUP:                      # original<->edit variant group (all members incl. self)
            gid = EDIT_GROUP[idv]
            mem = EDIT_GROUP_MEMBERS.get(gid, [])
            out["edit_group"] = {
                "group_id": gid,
                "role": next((r for (a, r, e) in mem if a == idv), None),
                "members": [{"id": a, "role": r, "edit_type": e} for (a, r, e) in mem]}
        if idv in CAND_IDS:                       # Candidates view: why-flagged driver
            out["rule"], out["driver"] = driver_text(CAND[idv])
        return out

    def export_month(self, q):
        """Per-month delete list: cut ids in THIS month → culling/library-delete-YYYY-MM.csv"""
        import csv as _csv
        if q.get("undated"):
            bk = "undated"
        else:
            y = int(q.get("y", [0])[0]); m = int(q.get("m", [0])[0])
            bk = bucket_of(y, m)
        with _dlock:
            cuts = [r[0] for r in _dconn.execute(
                "SELECT id FROM decisions WHERE state='cut' AND bucket=?", (bk,))]
        # vaulted / nsfw-flagged / edit-linked / protected-person never enter a delete
        # batch. EDIT_LINKED_IDS protects an original<->edit pair as a unit; PROTECTED_IDS
        # (people-protect) is a ride-along fix — it was enforced client-side ONLY, so a
        # protected person could previously reach a delete CSV; now hard-excluded here too.
        _excl = VAULT_IDS | NSFW_IDS | EDIT_LINKED_IDS | PROTECTED_IDS | PRODUCTION_IDS
        cuts = [i for i in cuts if i not in _excl]
        rows, tot = [], 0
        if cuts:
            want = set(cuts) | {STILL_TO_MOV[c] for c in cuts if c in STILL_TO_MOV}
            meta = _meta_files(want)
            cuts.sort(key=lambda i: -(meta.get(i, ("", 0))[1] or 0))
            for idv in cuts:                       # each cut still, then its motion clip
                fp, sz = meta.get(idv, ("", 0))
                rows.append((idv, fp, sz, "live_still" if idv in STILL_TO_MOV else "")); tot += sz
                mv = STILL_TO_MOV.get(idv)
                if mv is not None:
                    mfp, msz = meta.get(mv, ("", 0))
                    rows.append((mv, mfp, msz, "live_motion")); tot += msz
        path = os.path.join(EXPORT_DIR, f"library-delete-{bk}.csv")
        with open(path, "w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["id", "filepath", "size_bytes", "pair_role"])
            w.writerows(rows)
        return {"count": len(rows), "gb": round(tot / 1e9, 2),
                "path": path, "bucket": bk}

    def _export_manifest(self):
        """The manifest rows, computed but NOT written.

        Extracted so the preflight and the export cannot disagree. 9.4 asks for "the
        count, in words" with protections re-checked live before the door opens; a
        preflight that recomputed the set its own way would eventually report a number
        the export did not honour, which is worse than no preflight at all.

        Returns (rows, total_bytes, guard_counts).
        """
        with _dlock:
            cuts = [r[0] for r in _dconn.execute("SELECT id FROM decisions WHERE state='cut'")]
        # vaulted / nsfw-flagged / edit-linked / protected-person never exported for delete
        # (see export_month for the PROTECTED_IDS ride-along rationale).
        guards = [("vaulted", VAULT_IDS), ("flagged", NSFW_IDS),
                  ("edit-linked", EDIT_LINKED_IDS), ("protected person", PROTECTED_IDS),
                  ("work product", PRODUCTION_IDS)]
        _excl = VAULT_IDS | NSFW_IDS | EDIT_LINKED_IDS | PROTECTED_IDS | PRODUCTION_IDS
        cut_set = set(cuts)
        # What each guard actually held back, reported so the ceremony can show the
        # protections working rather than merely assert that they exist.
        held = {name: len(cut_set & ids & CAND_IDS) for name, ids in guards}
        not_flagged = len([i for i in cuts if i not in CAND_IDS])
        cuts = [i for i in cuts if i in CAND_IDS and i not in _excl]
        rows, tot = [], 0
        if cuts:
            want = set(cuts) | {STILL_TO_MOV[c] for c in cuts if c in STILL_TO_MOV}
            meta = _meta_files(want)
            cuts.sort(key=lambda i: -(meta.get(i, ("", 0))[1] or 0))
            for idv in cuts:
                fp, sz = meta.get(idv, ("", 0))
                rows.append((idv, fp, primary_rule(CAND[idv]["rules"]), sz, "")); tot += sz
                mv = STILL_TO_MOV.get(idv)
                if mv is not None:                 # cut still drags its motion clip along
                    mfp, msz = meta.get(mv, ("", 0))
                    rows.append((mv, mfp, "live_motion", msz, "live_motion")); tot += msz
        return rows, tot, {"held": held, "cut_total": len(cut_set),
                           "not_rule_flagged": not_flagged}

    def export_preflight(self):
        """Everything the export would do, without doing any of it (9.4 step 2).

        Read-only by construction: it shares _export_manifest with the real export and
        simply never reaches the writer.
        """
        rows, tot, meta = self._export_manifest()
        return {"count": len(rows), "gb": round(tot / 1e9, 2),
                "cut_total": meta["cut_total"],
                "not_rule_flagged": meta["not_rule_flagged"],
                "held_back": meta["held"],
                "path": os.path.join(EXPORT_DIR, "candidates-delete.csv")}

    def export_candidates(self):
        """Candidates slice: every CUT id that is also rule-flagged → candidates-delete.csv"""
        import csv as _csv
        rows, tot, _meta = self._export_manifest()
        path = os.path.join(EXPORT_DIR, "candidates-delete.csv")
        with open(path, "w", newline="") as f:
            w = _csv.writer(f)
            w.writerow(["id", "filepath", "rule", "size_bytes", "pair_role"])
            w.writerows(rows)
        return {"count": len(rows), "gb": round(tot / 1e9, 2), "path": path}

    def summary(self, q):
        """Period summary. Layer-1 facts always; ?gen=1 also does the venue+prose upgrade."""
        level = (q.get("level") or ["month"])[0]
        if level not in ("day", "week", "month"):
            level = "month"
        y = int((q.get("y") or ["0"])[0]) or None
        m = int((q.get("m") or ["0"])[0])
        d = int(q.get("d")[0]) if q.get("d") else None
        wk = int(q.get("wk")[0]) if q.get("wk") else None
        try:
            return SUM.get_summary(level, y, m, d, wk, bool(q.get("gen")))
        except Exception as e:
            return {"error": str(e), "facts": None, "ready": False}

    def sample(self, q):
        """~6 ids to back a card. y+m for Overview month cards, y+m+d for day cards."""
        if q.get("undated"):
            y, m = None, None
        else:
            y = int((q.get("y") or ["0"])[0]) or None
            m = int((q.get("m") or ["0"])[0])
        dv = q.get("d")
        d = int(dv[0]) if dv else None
        n = int((q.get("n") or ["0"])[0]) or None
        if q.get("best"):                       # photobook: diverse, freshly-shuffled frames (no near-dups)
            picks = portrait_diverse(y, m, d, n or 8)
            return {"ids": [i for i, _ in picks], "portrait": [p for _, p in picks]}
        ids = sample_period(y, m, d, n or 6)
        ps = portrait_ids(ids)                  # EXIF-aware per-photo orientation for carousel pillarboxing
        return {"ids": ids, "portrait": [i in ps for i in ids]}

    # ---- static / media ----
    def _html(self, s):
        b = s.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _static(self, p):
        """Serve files under ~/loupe/static/ (brand assets). Read-only, path-safe."""
        p = p.split("?", 1)[0]   # defensive: tolerate ?v= cache-busting query (no-op for query-less fetches)
        rel = os.path.normpath(p[len("/static/"):]).lstrip("/")
        if rel.startswith(".."):
            return self._json({"error": "bad path"}, 400)
        fp = os.path.join(HERE, "static", rel)
        if not os.path.isfile(fp):
            self.send_response(404); self.end_headers(); return
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        data = open(fp, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=604800")
        self.end_headers(); self.wfile.write(data)

    def _thumb(self, p, q=None):
        try:
            idv = int(p.rsplit("/", 1)[1].split(".")[0])
        except ValueError:
            return self._json({"error": "bad id"}, 400)
        fp = thumb_path(idv)
        if not os.path.exists(fp):
            if q and q.get("live"):     # living-card backdrop: LOCAL cache only — never read the NAS
                self.send_response(404); self.end_headers(); return
            # synchronous fallback (e.g. direct focus request before bg gen finished)
            conn = _ro()
            row = conn.execute(
                f"SELECT id, filepath, extension, duration_seconds FROM assets "
                f"WHERE id=? AND {EXCLUDE_SQL}", (idv,)).fetchone()
            conn.close()
            if row:
                _gen_one(idv, row["filepath"], _ext(row) in VIDEO_EXT, row["duration_seconds"])
        if not os.path.exists(fp):
            self.send_response(404); self.end_headers(); return
        data = open(fp, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _face(self, p):
        """Cropped face thumbnail for face_id (derived from the thumb cache)."""
        try:
            fid = int(p.rsplit("/", 1)[1].split(".")[0])
        except ValueError:
            return self._json({"error": "bad id"}, 400)
        data = FACES.face_crop(fid)
        if not data:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _full(self, p):
        # Raw full-res original — LAN/local only (never proxied over the CF path).
        if not self._is_local():
            return self._json({"error": "full-res disabled over remote path"}, 403)
        try:
            idv = int(p.rsplit("/", 1)[1])
        except ValueError:
            return self._json({"error": "bad id"}, 400)
        conn = _ro()
        row = conn.execute(
            f"SELECT filepath FROM assets WHERE id=? AND {EXCLUDE_SQL}", (idv,)).fetchone()
        conn.close()
        if not row:
            self.send_response(404); self.end_headers(); return
        fp = display_path(idv, row["filepath"])   # edited render if registered, else the original
        self._send_range(fp, mimetypes.guess_type(fp)[0] or "application/octet-stream")

    def _play(self, p):
        """On-demand transcoded H.264/AAC preview (<=720p), cached to disk. Served over
           the LAN AND the public CF path — the 403 gate is lifted for THIS endpoint only
           (it serves a small re-encode, never the raw original)."""
        try:
            idv = int(p.rsplit("/", 1)[1])
        except ValueError:
            return self._json({"error": "bad id"}, 400)
        conn = _ro()
        row = conn.execute(
            f"SELECT filepath, extension FROM assets WHERE id=? AND {EXCLUDE_SQL}", (idv,)).fetchone()
        conn.close()
        if not row:
            self.send_response(404); self.end_headers(); return
        if (row["extension"] or "").upper() not in VIDEO_EXT:
            return self._json({"error": "not a video"}, 400)
        out = transcode_preview(idv, row["filepath"])
        if not out:
            return self._json({"error": "transcode failed"}, 500)
        self._send_range(out, "video/mp4")

    def _preview(self, p):
        """High-res JPEG preview of an image — served over LAN AND the CF path (it's a
           derived JPEG, not the raw original). Videos: 400 (use the thumb + /api/play)."""
        try:
            idv = int(p.rsplit("/", 1)[1])
        except ValueError:
            return self._json({"error": "bad id"}, 400)
        conn = _ro()
        row = conn.execute(
            f"SELECT filepath, extension FROM assets WHERE id=? AND {EXCLUDE_SQL}", (idv,)).fetchone()
        conn.close()
        if not row:
            self.send_response(404); self.end_headers(); return
        if (row["extension"] or "").upper() in VIDEO_EXT:
            return self._json({"error": "not an image"}, 400)
        try:
            is_orig = ("original=1" in self.path or "original=true" in self.path.lower())
        except Exception:
            pass
        out = build_preview(idv, row["filepath"], original=is_orig)
        if not out:
            return self._json({"error": "preview failed"}, 500)
        self._send_range(out, "image/jpeg")

    def _send_range(self, fp, ctype):
        """Stream a file with single Range support (so <video> can seek)."""
        try:
            size = os.path.getsize(fp)
            mtime = os.stat(fp).st_mtime
        except OSError:
            self.send_response(404); self.end_headers(); return
        last_modified = email.utils.formatdate(mtime, usegmt=True)
        rng = self.headers.get("Range")
        ims = self.headers.get("If-Modified-Since")
        if not rng and ims:
            try:
                ims_epoch = email.utils.parsedate_to_datetime(ims).timestamp()
            except (TypeError, ValueError):
                ims_epoch = None
            if ims_epoch is not None and int(mtime) <= int(ims_epoch):
                self.send_response(304)
                self.send_header("Cache-Control", "public, max-age=31536000")
                self.send_header("Last-Modified", last_modified)
                self.end_headers()
                return
        try:
            with open(fp, "rb") as f:
                if rng and rng.startswith("bytes="):
                    a, _, b = rng[6:].partition("-")
                    start = int(a or 0); end = min(int(b) if b else size - 1, size - 1)
                    f.seek(start); chunk = f.read(end - start + 1)
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(chunk)))
                    self.send_header("Cache-Control", "public, max-age=31536000")
                    self.send_header("Last-Modified", last_modified)
                    self.end_headers(); self.wfile.write(chunk)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(size))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Cache-Control", "public, max-age=31536000")
                    self.send_header("Last-Modified", last_modified)
                    self.end_headers()
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk: break
                        self.wfile.write(chunk)
        except Exception as e:
            try:
                self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode())
            except Exception:
                pass


def _asset_ver(rel):
    try:
        with open(os.path.join(HERE, *rel.split("/")), "rb") as _f:
            return hashlib.sha256(_f.read()).hexdigest()[:8]
    except OSError:
        return "0"
PAGE = (open(os.path.join(HERE, "app.html"), encoding="utf-8").read()
        .replace("__CSSVER__", _asset_ver("static/app.css"))
        .replace("__JSVER__",  _asset_ver("static/app.js")))


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        PORT = int(sys.argv[1])
    # Bind the LAN interface so cloudflared (incl. the NAS replica) can reach the
    # origin at http://<nas-host>:8000. Full-res stays gated: CF path → 403,
    # LAN/local (private IP) → allowed, by design (_is_local).
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"merged photo-cull server on http://0.0.0.0:{PORT}/  (Ctrl-C to stop)", flush=True)
    srv.serve_forever()
