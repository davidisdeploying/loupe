#!/usr/bin/env python3
"""
Photo library metadata ingest -> SQLite.

ITERATION 3 (this version): adds perceptual hashing (pHash + dHash, 16-char
hex) and Laplacian-variance blur scoring for image files (HEIC/HEIF/JPG/JPEG/
PNG/GIF/TIFF; skipped for video). These are computed in the SAME per-file pass
as the sha256: for an image we read the raw bytes ONCE and serve both the
sha256 and the PIL decode from those bytes (one CIFS round-trip, no double
read). STILL deferred: video duration_seconds (needs ffprobe).

ITERATION 2: EXIF + file metadata, sha256 content hashing, and Live Photo
pairing -- all parallelized over a ThreadPoolExecutor for the I/O-bound work.

Resume is cheap (skip rows with non-null processed_at and matching size+mtime)
and never re-hashes an unchanged file (sha256 is reused when size+mtime match).

The DB lives on the LOCAL SSD (default ~/loupe-pipeline/metadata.db), never on
the NAS -- CIFS chokes on SQLite locking and small writes.

Usage:
    python3 ingest.py --root "$LIBRARY_ROOT/originals/2002"
    python3 ingest.py --root "$LIBRARY_ROOT/originals" --db "$DATA_ROOT/metadata.db"
"""

import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone

import cv2
import imagehash
import numpy as np
from PIL import Image
from pillow_heif import register_heif_opener

# Teach Pillow to open HEIC/HEIF (must run before any Image.open on HEIC).
register_heif_opener()

# --- Configuration ---------------------------------------------------------

# Media file extensions we ingest (lowercase, no dot).
# cr2/dng/cr3 are RAW (fs+EXIF+sha only -- no PIL decoder here, so they are NOT
# in IMAGE_EXTENSIONS and take the sha-only path, leaving phash/dhash/blur NULL;
# perceptual-hash backfill is a separate future job). webp is a decodable raster
# and IS in IMAGE_EXTENSIONS. The existing try/except around
# compute_image_features (in process_file) already guards a non-decoding image:
# on failure the fs+exif+sha row still stands with phash/dhash/blur NULL.
MEDIA_EXTENSIONS = {"heic", "heif", "jpg", "jpeg", "png", "mov", "mp4", "m4v", "gif", "tiff",
                    "cr2", "dng", "cr3", "webp", "avif"}

# Image extensions (UPPERCASE, matching the stored `extension` column) that get
# pHash/dHash/blur features. Everything else (MOV/MP4/M4V and RAW CR2/DNG/CR3)
# leaves them NULL.
IMAGE_EXTENSIONS = {"HEIC", "HEIF", "JPG", "JPEG", "PNG", "GIF", "TIFF", "WEBP", "AVIF"}

# Longest-side pixels for the blur computation. Resizing to a fixed size makes
# the Laplacian-variance score comparable across differing source resolutions
# (and bounds per-image compute); pHash/dHash are resolution-robust already.
BLUR_RESIZE_LONG_EDGE = 1024

# Portable roots — env-unset reproduces the historical layout (see ONBOARDING.md):
#   LIBRARY_ROOT — read-only source tree of original media (the NAS mount).
#   DATA_ROOT    — where metadata.db is written (this script's dir by default).
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.environ.get("DATA_ROOT", HERE)
LIBRARY_ROOT = os.environ.get("LIBRARY_ROOT", os.path.join(os.sep, "mnt", "nas", "photos"))

# Optional CIFS-mount guard. OFF by default; set LOUPE_REQUIRE_MOUNT=1 to abort when the
# NAS isn't mounted (enable it in your service env / shell profile to enforce). The sentinel
# file path is itself overridable via the MOUNT_SENTINEL env.
MOUNT_SENTINEL = os.environ.get(
    "MOUNT_SENTINEL", os.path.join(LIBRARY_ROOT, "originals", ".mounted"))
REQUIRE_MOUNT = os.environ.get("LOUPE_REQUIRE_MOUNT", "") not in ("", "0", "false", "False")

DEFAULT_DB = os.path.join(DATA_ROOT, "metadata.db")

# Vendored, no-install exiftool (Perl distribution) shipped with this project.
VENDORED_EXIFTOOL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "vendor", "exiftool-dist", "exiftool"
)

PROGRESS_EVERY = 100      # report progress every N files
COMMIT_EVERY = 100        # commit the DB transaction every N files
MOUNT_RECHECK_EVERY = 1000  # re-verify the CIFS mount every N files
def _envint(name, default):
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default
DEFAULT_WORKERS = _envint("INGEST_WORKERS", os.cpu_count() or 4)   # I/O-bound; tune with --workers
SHA_CHUNK = 1 << 20       # 1 MiB read chunks (keep memory low for big videos)

# Counters file consumed by `fleet_run --metrics-file` (folded into each
# heartbeat). Stable path so the jobs-panel wiring never has to guess.
METRICS_FILE = os.environ.get(
    "INGEST_METRICS_FILE", os.path.join(DATA_ROOT, "avif_metrics.json"))


def write_metrics(state, pairs=None, path=METRICS_FILE):
    """Publish run counters as a small JSON object, atomically (temp +
    os.replace). Best-effort by contract: metrics must NEVER break ingest,
    so every failure is swallowed and logged."""
    try:
        payload = {
            "processed": state["processed"],
            "skipped": state["skipped"],
            "errors": state["errors"],
        }
        if pairs is not None:
            payload["pairs"] = pairs
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - metrics are advisory only
        print(f"  metrics write failed (ignored): {e}", flush=True)


# --- exiftool discovery ----------------------------------------------------

def find_exiftool():
    """Prefer a system exiftool on PATH; fall back to the vendored copy."""
    from shutil import which
    sys_et = which("exiftool")
    if sys_et:
        return [sys_et]
    if os.path.exists(VENDORED_EXIFTOOL):
        # Invoke via perl explicitly so we don't depend on the +x bit / shebang.
        return ["perl", VENDORED_EXIFTOOL]
    sys.exit(
        "ERROR: exiftool not found on PATH and vendored copy missing at "
        f"{VENDORED_EXIFTOOL}"
    )


# --- Schema ----------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath              TEXT UNIQUE NOT NULL,
    filename              TEXT,
    file_size_bytes       INTEGER,
    file_mtime            INTEGER,
    file_sha256           TEXT,          -- deferred (content hashing iteration)
    mime_type             TEXT,
    extension             TEXT,
    year                  INTEGER,
    month                 INTEGER,

    -- EXIF / metadata
    capture_timestamp     INTEGER,
    gps_lat               REAL,
    gps_lon               REAL,
    camera_make           TEXT,
    camera_model          TEXT,
    lens_model            TEXT,
    iso                   INTEGER,
    shutter_speed         REAL,          -- exposure time in seconds
    aperture              REAL,          -- f-number
    width_pixels          INTEGER,
    height_pixels         INTEGER,
    orientation           INTEGER,

    -- Live Photo / shared album detection
    is_live_photo_still   INTEGER,       -- deferred (pairing iteration)
    is_live_photo_video   INTEGER,       -- deferred
    live_photo_partner_id INTEGER,       -- deferred
    is_shared_album       INTEGER,

    -- Quality / similarity
    phash                 TEXT,          -- deferred (hashing iteration)
    dhash                 TEXT,          -- deferred
    blur_laplacian        REAL,          -- deferred

    -- Video-specific
    duration_seconds      REAL,          -- deferred (video iteration)

    -- Processing tracking
    processed_at          INTEGER,
    processing_errors     TEXT
);

CREATE INDEX IF NOT EXISTS idx_year                ON assets(year);
CREATE INDEX IF NOT EXISTS idx_month               ON assets(month);
CREATE INDEX IF NOT EXISTS idx_capture_timestamp   ON assets(capture_timestamp);
CREATE INDEX IF NOT EXISTS idx_gps                 ON assets(gps_lat, gps_lon);
CREATE INDEX IF NOT EXISTS idx_phash               ON assets(phash);
CREATE INDEX IF NOT EXISTS idx_sha256              ON assets(file_sha256);
CREATE INDEX IF NOT EXISTS idx_shared_album        ON assets(is_shared_album);
CREATE INDEX IF NOT EXISTS idx_live_still          ON assets(is_live_photo_still);
CREATE INDEX IF NOT EXISTS idx_live_video          ON assets(is_live_photo_video);
"""


def init_db(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")   # better resilience to interruption
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --- Metadata extraction ---------------------------------------------------

# exiftool date strings look like "2002:11:22 18:05:08", optionally with a
# fractional part and/or timezone: "2024:01:02 03:04:05.67+05:30" / "...Z".
_DT_RE = re.compile(
    r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
    r"(?:\.\d+)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_exif_datetime(value):
    """
    Parse an exiftool date string to a unix timestamp (int seconds).

    Timezone-aware strings are honored. Naive strings (no offset) are common in
    iCloud exports; we interpret them as UTC so the value is stable and sortable.
    Returns None on anything unparseable or zeroed ("0000:00:00 00:00:00").
    """
    if not value or not isinstance(value, str):
        return None
    m = _DT_RE.match(value.strip())
    if not m:
        return None
    year, mon, day, hh, mm, ss, tz = m.groups()
    if year == "0000" or mon == "00" or day == "00":
        return None
    try:
        dt = datetime(int(year), int(mon), int(day), int(hh), int(mm), int(ss))
    except ValueError:
        return None
    if tz and tz != "Z":
        sign = 1 if tz[0] == "+" else -1
        tz = tz[1:].replace(":", "")
        offset_min = sign * (int(tz[:2]) * 60 + int(tz[2:]))
        from datetime import timedelta
        dt = dt.replace(tzinfo=timezone(timedelta(minutes=offset_min)))
    else:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def first_present(d, keys):
    """Return the first non-empty value among `keys` in dict `d`, else None."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", "-"):
            return v
    return None


def to_number(v):
    """Coerce an exiftool value to float, or None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run_exiftool_batch(filepaths, exiftool_cmd):
    """
    Run exiftool ONCE over many files, amortizing the (heavy) Perl-interpreter
    startup across the whole batch instead of paying it per file.

    Returns {normpath(SourceFile): tags_dict}. Files exiftool could not read are
    simply absent from the map (the caller records a per-file error and still
    keeps the filesystem-derived row). File paths are fed on STDIN via `-@ -`,
    one per line, which sidesteps ARG_MAX limits for large directories.
    Raises only on a wholesale failure (timeout / unparseable output).
    """
    if not filepaths:
        return {}
    proc = subprocess.run(
        exiftool_cmd + [
            "-j",                 # JSON output (array of per-file objects)
            "-n",                 # numeric values (no print-conversion)
            "-G",                 # group prefixes on tag names (EXIF:, File:, ...)
            "-api", "largefilesupport=1",
            "-charset", "filename=UTF8",
            "-@", "-",            # read the file list from stdin, one path per line
        ],
        input="\n".join(filepaths) + "\n",
        capture_output=True,
        encoding="utf-8",
        # Generous, file-count-scaled ceiling; batched is ~0.11s/file in practice.
        timeout=max(120, 5 * len(filepaths)),
    )
    out = proc.stdout.strip()
    if not out:
        # A bad single file does NOT empty the batch; empty output means the
        # whole invocation failed (e.g. exiftool missing). Surface it.
        raise RuntimeError(
            f"exiftool batch produced no output (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:200]}"
        )
    data = json.loads(out)
    result = {}
    for obj in data:
        sf = obj.get("SourceFile")
        if sf:
            result[os.path.normpath(sf)] = obj
    return result


def build_fs_row(filepath, st):
    """
    Build the filesystem-derived part of a row (no exiftool). `st` is a prior
    os.stat result, passed in so we don't re-stat over CIFS. The exiftool-derived
    fields are filled later by apply_tags(); on exiftool failure the row still
    stands on these fields alone.
    """
    filename = os.path.basename(filepath)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # Year/month from the {YYYY}/{MM}/ path layout.
    parts = filepath.split(os.sep)
    year = month = None
    for i, p in enumerate(parts):
        if re.fullmatch(r"\d{4}", p) and 1900 <= int(p) <= 2100:
            year = int(p)
            if i + 1 < len(parts) and re.fullmatch(r"\d{2}", parts[i + 1]):
                month = int(parts[i + 1])
            break

    # Shared-album heuristic (filename-based, case-sensitive per brief).
    is_shared = 1 if (filename.startswith("od_") or "_singular_display" in filename) else 0

    return {
        "filepath": filepath,
        "filename": filename,
        "file_size_bytes": st.st_size,
        "file_mtime": int(st.st_mtime),
        "extension": ext.upper(),
        "year": year,
        "month": month,
        "is_shared_album": is_shared,
        # Live Photo flags default to 0; the global pairing pass sets matches to 1.
        "is_live_photo_still": 0,
        "is_live_photo_video": 0,
        "live_photo_partner_id": None,
        "processed_at": int(time.time()),
        "processing_errors": None,
    }


def apply_tags(row, tags):
    """Fill the exiftool-derived columns on `row` from a parsed tag dict."""
    row["mime_type"] = tags.get("File:MIMEType")

    # Capture time: prefer the original-shot tag, then create/media tags.
    cap = first_present(tags, [
        "EXIF:DateTimeOriginal",
        "EXIF:CreateDate",
        "QuickTime:CreateDate",       # videos
        "XMP:DateTimeOriginal",
        "XMP:CreateDate",
        "Composite:SubSecDateTimeOriginal",
    ])
    row["capture_timestamp"] = parse_exif_datetime(cap)

    # GPS -- Composite tags fold the N/S/E/W ref into a signed decimal.
    row["gps_lat"] = to_number(first_present(tags, ["Composite:GPSLatitude", "EXIF:GPSLatitude"]))
    row["gps_lon"] = to_number(first_present(tags, ["Composite:GPSLongitude", "EXIF:GPSLongitude"]))

    row["camera_make"] = first_present(tags, ["EXIF:Make", "QuickTime:Make"])
    row["camera_model"] = first_present(tags, ["EXIF:Model", "QuickTime:Model"])
    row["lens_model"] = first_present(tags, ["EXIF:LensModel", "Composite:LensID", "XMP:Lens"])

    iso = to_number(first_present(tags, ["EXIF:ISO", "Composite:ISO"]))
    row["iso"] = int(iso) if iso is not None else None
    row["shutter_speed"] = to_number(first_present(tags, ["EXIF:ExposureTime", "Composite:ShutterSpeed"]))
    row["aperture"] = to_number(first_present(tags, ["EXIF:FNumber", "Composite:Aperture"]))

    w = to_number(first_present(tags, ["File:ImageWidth", "EXIF:ExifImageWidth", "QuickTime:ImageWidth", "RIFF:ImageWidth"]))
    h = to_number(first_present(tags, ["File:ImageHeight", "EXIF:ExifImageHeight", "QuickTime:ImageHeight", "RIFF:ImageHeight"]))
    row["width_pixels"] = int(w) if w is not None else None
    row["height_pixels"] = int(h) if h is not None else None

    orient = to_number(tags.get("EXIF:Orientation"))
    row["orientation"] = int(orient) if orient is not None else None


def sha256_file(filepath):
    """Streaming sha256 of the whole file (chunked to bound memory)."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(SHA_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _append_error(row, note):
    """Append a note to row['processing_errors'], capped at 500 chars."""
    prev = row.get("processing_errors")
    row["processing_errors"] = (f"{prev}; {note}" if prev else note)[:500]


def compute_image_features(data):
    """
    From already-read image bytes, compute (phash, dhash, blur_laplacian):
      - phash/dhash: 16-char hex strings (8x8 -> 64-bit hashes).
      - blur_laplacian: variance of the Laplacian on a grayscale image whose
        longest edge is normalized to BLUR_RESIZE_LONG_EDGE (higher = sharper).
    Decoding the bytes (not re-reading the file) keeps this to one CIFS read.
    Raises on decode failure; the caller records it in processing_errors.
    """
    with Image.open(io.BytesIO(data)) as im:
        # draft() lets the JPEG decoder downscale during decode (huge speedup on
        # big JPEGs); it's a no-op for HEIC/PNG. pHash is robust to it.
        im.draft("RGB", (BLUR_RESIZE_LONG_EDGE, BLUR_RESIZE_LONG_EDGE))
        im = im.convert("RGB")
        phash = str(imagehash.phash(im, hash_size=8))
        dhash = str(imagehash.dhash(im, hash_size=8))

        gray = im.convert("L")
        w, h = gray.size
        scale = BLUR_RESIZE_LONG_EDGE / max(w, h)
        if scale < 1.0:
            gray = gray.resize((max(1, round(w * scale)), max(1, round(h * scale))))
        arr = np.asarray(gray, dtype=np.float64)
        blur = float(cv2.Laplacian(arr, cv2.CV_64F).var())
    return phash, dhash, blur


# Sentinel: tell the worker to fetch exiftool tags itself (per-file --no-batch
# fallback). Distinct from None, which means "exiftool ran but returned nothing".
FETCH_TAGS = object()


def process_file(filepath, st, tags, reuse_sha, exiftool_cmd=None):
    """
    Worker run in the thread pool: per-file heavy I/O (full-file sha256 +, for
    images, pHash/dHash/blur). exiftool is normally NOT called here -- its tags
    are fetched once per directory by the caller and passed in as `tags` (a dict,
    or None if exiftool returned nothing for this file). In the --no-batch
    fallback `tags` is FETCH_TAGS and the worker calls exiftool for this one file
    (paying Perl startup per file, but parallelized across workers). `reuse_sha`
    is a prior hash to trust when size+mtime are unchanged, so we never re-hash an
    unchanged file. Returns a complete row dict; never raises -- failures fold
    into processing_errors.

    For image files we read the raw bytes ONCE and feed them to both the sha256
    and the perceptual-feature decode -- a single CIFS read serves both.
    """
    if tags is FETCH_TAGS:
        try:
            tags = run_exiftool_batch([filepath], exiftool_cmd).get(os.path.normpath(filepath))
        except Exception as e:  # noqa: BLE001 - record, keep going
            tags = None
            _exiftool_err = f"exiftool: {type(e).__name__}: {e}"
        else:
            _exiftool_err = None
    else:
        _exiftool_err = None

    row = build_fs_row(filepath, st)
    if _exiftool_err:
        _append_error(row, _exiftool_err)
    elif tags is None:
        _append_error(row, "exiftool: no metadata returned for file")
    else:
        try:
            apply_tags(row, tags)
        except Exception as e:  # noqa: BLE001 - malformed tag value; keep the row
            _append_error(row, f"apply_tags: {type(e).__name__}: {e}")

    is_image = row.get("extension") in IMAGE_EXTENSIONS

    if is_image:
        # One read serves both the content hash and the perceptual decode.
        data = None
        try:
            with open(filepath, "rb") as f:
                data = f.read()
        except Exception as e:  # noqa: BLE001
            _append_error(row, f"read: {type(e).__name__}: {e}")

        if data is not None:
            row["file_sha256"] = reuse_sha if reuse_sha else hashlib.sha256(data).hexdigest()
            try:
                row["phash"], row["dhash"], row["blur_laplacian"] = compute_image_features(data)
            except Exception as e:  # noqa: BLE001 - bad/corrupt image; keep the row
                _append_error(row, f"features: {type(e).__name__}: {e}")
        elif reuse_sha:
            row["file_sha256"] = reuse_sha   # couldn't read now, but trust prior hash
    else:
        # Video (or unknown): hash only, streamed to bound memory on big files.
        if reuse_sha:
            row["file_sha256"] = reuse_sha
        else:
            try:
                row["file_sha256"] = sha256_file(filepath)
            except Exception as e:  # noqa: BLE001
                _append_error(row, f"sha256: {type(e).__name__}: {e}")
    return row


# --- DB upsert -------------------------------------------------------------

def upsert(conn, row):
    cols = list(row.keys())
    placeholders = ", ".join("?" for _ in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "filepath")
    sql = (
        f"INSERT INTO assets ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(filepath) DO UPDATE SET {updates}"
    )
    conn.execute(sql, [row[c] for c in cols])


def fetch_existing(conn, filepath):
    """
    Return (size, mtime, sha256, processed_at, phash, extension, errors) for
    filepath, or None. The extra columns let resume detect rows that are done
    for an older iteration but still missing this iteration's features.
    """
    cur = conn.execute(
        "SELECT file_size_bytes, file_mtime, file_sha256, processed_at, "
        "phash, extension, processing_errors "
        "FROM assets WHERE filepath=?",
        (filepath,),
    )
    return cur.fetchone()


def pair_live_photos(conn):
    """
    Global Live Photo pairing pass. A HEIC still is paired with a MOV in the
    SAME directory whose basename matches the HEIC's, with or without a `_HEVC`
    suffix (e.g. IMG_3277.HEIC <-> IMG_3277.MOV or IMG_3277_HEVC.MOV).

    Idempotent: recomputes flags over the whole DB, so it self-heals when the
    two halves of a pair were ingested in different runs.
    """
    rows = conn.execute(
        "SELECT id, filepath, filename, extension FROM assets "
        "WHERE extension IN ('HEIC','MOV')"
    ).fetchall()

    by_dir = defaultdict(list)
    for asset_id, filepath, filename, ext in rows:
        by_dir[os.path.dirname(filepath)].append((asset_id, filename, ext))

    pairs = []  # (heic_id, mov_id)
    for items in by_dir.values():
        movs = {}   # normalized stem -> mov id
        heics = []  # (id, normalized stem)
        for asset_id, filename, ext in items:
            stem = filename.rsplit(".", 1)[0].lower()
            if ext == "MOV":
                movs[stem] = asset_id
                if stem.endswith("_hevc"):       # also index the bare stem
                    movs[stem[:-5]] = asset_id
            elif ext == "HEIC":
                heics.append((asset_id, stem))
        for hid, hstem in heics:
            mid = movs.get(hstem)
            if mid is not None:
                pairs.append((hid, mid))

    # Reset first so de-paired files (deletions/renames) don't keep stale flags.
    conn.execute(
        "UPDATE assets SET is_live_photo_still=0, is_live_photo_video=0, "
        "live_photo_partner_id=NULL WHERE extension IN ('HEIC','MOV')"
    )
    for hid, mid in pairs:
        conn.execute(
            "UPDATE assets SET is_live_photo_still=1, live_photo_partner_id=? WHERE id=?",
            (mid, hid),
        )
        conn.execute(
            "UPDATE assets SET is_live_photo_video=1, live_photo_partner_id=? WHERE id=?",
            (hid, mid),
        )
    return len(pairs)


# --- Walking & mount checks ------------------------------------------------

def check_mount():
    if REQUIRE_MOUNT and not os.path.exists(MOUNT_SENTINEL):
        sys.exit(
            f"ERROR: NAS mount sentinel missing ({MOUNT_SENTINEL}). "
            "Is the CIFS share mounted? Aborting. "
            "(Set LOUPE_REQUIRE_MOUNT=0 to disable this guard.)"
        )


def iter_media_files(root):
    """Yield media file paths under root, lazily (no big in-memory list)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext in MEDIA_EXTENSIONS:
                yield os.path.join(dirpath, name)


def iter_media_dirs(root):
    """
    Yield (dirpath, [sorted media filepaths]) one directory at a time. Grouping
    by directory lets us run exiftool ONCE per directory (batched) instead of
    once per file. Only the file list of a single directory is held in memory.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        media = [
            os.path.join(dirpath, name)
            for name in sorted(filenames)
            if (name.rsplit(".", 1)[-1].lower() if "." in name else "") in MEDIA_EXTENSIONS
        ]
        if media:
            yield dirpath, media


def count_media_files(root):
    """Lightweight pre-pass: count files for progress %, without stat-ing them."""
    n = 0
    for _ in iter_media_files(root):
        n += 1
    return n


# --- Main ------------------------------------------------------------------

def fmt_eta(seconds):
    if seconds is None or seconds < 0:
        return "?:??"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def main():
    ap = argparse.ArgumentParser(description="Photo metadata ingest -> SQLite")
    ap.add_argument("--root", required=True, help="Directory to walk (on the NAS)")
    ap.add_argument("--db", default=DEFAULT_DB, help=f"SQLite DB path (default {DEFAULT_DB})")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"Thread pool size for I/O-bound work (default {DEFAULT_WORKERS})")
    ap.add_argument("--no-batch", action="store_true",
                    help="Fallback: call exiftool once per file (on workers) instead "
                         "of batching one invocation per directory. Slower; for "
                         "debugging or A/B timing.")
    args = ap.parse_args()

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        sys.exit(f"ERROR: --root is not a directory: {root}")

    check_mount()
    exiftool_cmd = find_exiftool()
    conn = init_db(args.db)

    print(f"Counting media files under {root} ...", flush=True)
    total = count_media_files(root)
    print(f"Found {total} media files. DB: {args.db} | workers: {args.workers}", flush=True)

    start = time.time()
    # Mutable counters shared with the submit closure.
    state = {"seen": 0, "processed": 0, "skipped": 0, "errors": 0, "last_report": 0}

    def maybe_report(force=False):
        s = state
        if not force and (s["processed"] + s["skipped"]) - s["last_report"] < PROGRESS_EVERY:
            return
        s["last_report"] = s["processed"] + s["skipped"]
        elapsed = time.time() - start
        rate = s["processed"] / elapsed if (elapsed > 0 and s["processed"]) else 0
        remaining = (total - s["seen"]) / rate if rate > 0 else None
        pct = (s["seen"] / total * 100) if total else 100
        print(
            f"Processed {s['seen']}/{total} ({pct:.1f}%), "
            f"new: {s['processed']}, skipped: {s['skipped']}, errors: {s['errors']}, "
            f"est. time remaining: {fmt_eta(remaining)}",
            flush=True,
        )
        write_metrics(state)

    futures = {}              # future -> filepath
    window = max(2, args.workers * 2)   # bound in-flight work (and memory)

    def drain(block):
        """Reap finished per-file futures: upsert rows, update counters.
        block=True waits for at least one to finish; block=False reaps only
        those already done (non-blocking opportunistic reap)."""
        if not futures:
            return
        if block:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
        else:
            done = [f for f in list(futures) if f.done()]
        for fut in done:
            futures.pop(fut)
            row = fut.result()   # process_file never raises
            if row.get("processing_errors"):
                state["errors"] += 1
            upsert(conn, row)
            state["processed"] += 1
            if state["processed"] % COMMIT_EVERY == 0:
                conn.commit()
            maybe_report()

    def plan_directory(filepaths):
        """Resume/skip decisions for one directory's files (main thread).
        Returns the list of (filepath, st, reuse_sha) that still need work; the
        skipped ones never reach exiftool."""
        to_process = []
        for filepath in filepaths:
            state["seen"] += 1
            if state["seen"] % MOUNT_RECHECK_EVERY == 0:
                check_mount()
            try:
                st = os.stat(filepath)
            except OSError as e:
                state["errors"] += 1
                print(f"  stat failed, skipping: {filepath} ({e})", flush=True)
                continue
            size, mtime = st.st_size, int(st.st_mtime)

            existing = fetch_existing(conn, filepath)
            reuse_sha = None
            if existing is not None:
                db_size, db_mtime, db_sha, processed_at, db_phash, db_ext, db_errs = existing
                unchanged = (db_size == size and db_mtime == mtime)
                if processed_at is not None and unchanged:
                    # "Done" means: processed AND, for images, features present.
                    # A prior recorded error counts as done so we don't retry an
                    # un-decodable file on every run.
                    is_image = db_ext in IMAGE_EXTENSIONS
                    features_done = (not is_image) or db_phash is not None or db_errs is not None
                    if features_done:
                        state["skipped"] += 1
                        maybe_report()
                        continue
                    # else: fall through to reprocess (backfill features).
                if unchanged and db_sha:
                    reuse_sha = db_sha       # don't re-hash an unchanged file
            to_process.append((filepath, st, reuse_sha))
        return to_process

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for dirpath, filepaths in iter_media_dirs(root):
            to_process = plan_directory(filepaths)
            if not to_process:
                continue   # entire directory already done -> exiftool never runs

            if args.no_batch:
                # Fallback: each worker fetches its own file's tags (per-file
                # exiftool startup, but parallel across workers).
                tags_map = None
            else:
                # ONE exiftool invocation for this directory's not-done files.
                # While this subprocess blocks the main thread, the GIL is
                # released, so the pool workers keep draining the PREVIOUS
                # directory's per-file I/O: exiftool-batch(dir N+1) overlaps
                # file-I/O(dir N).
                try:
                    tags_map = run_exiftool_batch(
                        [fp for fp, _, _ in to_process], exiftool_cmd
                    )
                except Exception as e:  # noqa: BLE001 - degrade: keep fs+feature data
                    tags_map = {}
                    print(f"  exiftool batch failed for {dirpath}: {e}", flush=True)

            for filepath, st, reuse_sha in to_process:
                if tags_map is None:
                    tags = FETCH_TAGS
                else:
                    tags = tags_map.get(os.path.normpath(filepath))
                fut = executor.submit(process_file, filepath, st, tags, reuse_sha, exiftool_cmd)
                futures[fut] = filepath
                while len(futures) >= window:   # bound in-flight work + memory
                    drain(block=True)
            drain(block=False)   # opportunistic reap before the next dir's batch

        while futures:           # final drain
            drain(block=True)

    conn.commit()
    maybe_report(force=True)

    print("Pairing Live Photos ...", flush=True)
    n_pairs = pair_live_photos(conn)
    conn.commit()
    conn.close()
    write_metrics(state, pairs=n_pairs)

    elapsed = time.time() - start
    print(
        f"\nDone. {state['seen']} files seen, {state['processed']} processed, "
        f"{state['skipped']} skipped (resume), {state['errors']} errors, "
        f"{n_pairs} Live Photo pairs, in {fmt_eta(elapsed)}.",
        flush=True,
    )


if __name__ == "__main__":
    main()
