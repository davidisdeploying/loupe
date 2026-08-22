#!/usr/bin/env python3
"""
enrichment/common.py — shared, parameterized core for building apple-enrichment.db.

This is the committed, reproducible form of the one-off /tmp/build_enrichment.py that
originally produced apple-enrichment.db (UUID bridge + leo scene labels + named persons),
plus the separate scores load. Every path here is a parameter or an env-derived default —
NO personal absolute paths are baked in.

Provenance of the recipe (preserved verbatim from the original build):
  * Bridge   : osxphotos default 40-col CSV export of an Apple Photos library. The export's
               stderr ("Unmatched template field …") is interleaved before the real header,
               so we locate the header line and parse only rows that start with a UUID.
  * Matcher  : metadata.db has NO UUID column, so the join is filename+timestamp heuristic.
               metadata.db.capture_timestamp is MIXED true-UTC and local-Central wall-clock,
               so an exact-second tier (UTC) and a 25-hour tz-tolerant tier absorb the offset.
               Live-photo motion halves are named IMG_xxxx_HEVC.MOV → strip _HEVC to reach the
               still's IMG_xxxx.HEIC bridge entry. Ambiguous filenames with no usable date are
               REFUSED (left unbridged) rather than mis-stamped onto the wrong asset.
  * Labels   : version-aware. Prefer osxphotos' own label/search API (stable macOS, psi.sqlite).
               Fall back to a hand-rolled direct decode of the macOS-27 "leo.sqlite" search
               index (the psi.sqlite successor osxphotos can't yet read) only when the API
               can't read the index. The branch that runs is detected and logged.
  * Scores   : Apple aesthetic "overall" score, via the osxphotos Python API on a Mac, or from
               a pre-exported scores.csv (uuid,score_overall) when the API isn't available.

Discipline: metadata.db is opened mode=ro. The leo/psi index is worked on a /tmp copy. The
output is a NEW database file — this module never opens the live apple-enrichment.db.
"""
import csv
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import tarfile
import tempfile
from collections import defaultdict
from datetime import datetime, timezone

# ---- leo/psi numeric category → our label namespace ------------------------
WANTED_CATS = {4000: 'scene', 4010: 'species', 4020: 'landmark',
               4060: 'action', 4090: 'food', 4120: 'ocr', 4130: 'document'}

# Matching tolerances (seconds). TZ absorbs metadata.db's mixed UTC/local-Central epochs.
TOL = 2.0       # "exact" UTC date match
TZ = 90000.0    # 25h window; safe because recycled IMG_ filenames are months/years apart

# Confidence tiers (method -> confidence). The first five are the original CSV-path tiers;
# the two filesize tiers are added by the bundle path (match_assets_tightened) — file_size_bytes
# is an exact, strong disambiguator, so they rank just below an exact-second date match.
CONF = {'filename+date': 1.0, 'filename+filesize': 0.97, 'filesize_tiebreak': 0.96,
        'filename+date_tz': 0.95, 'filename_unique': 0.85, 'live_photo_video': 0.8,
        'filename_only': 0.6}

SCHEMA = """
CREATE TABLE asset_uuid(asset_id INTEGER PRIMARY KEY, uuid TEXT NOT NULL,
    match_method TEXT, confidence REAL, cloud_guid TEXT, fingerprint TEXT);
CREATE INDEX idx_au_uuid ON asset_uuid(uuid);
CREATE TABLE labels(asset_id INTEGER, category TEXT, term TEXT, score REAL);
CREATE INDEX idx_lbl_asset ON labels(asset_id);
CREATE INDEX idx_lbl_term  ON labels(term);
CREATE TABLE persons(asset_id INTEGER, person TEXT);
CREATE INDEX idx_p_asset ON persons(asset_id);
CREATE INDEX idx_p_person ON persons(person);
CREATE TABLE apple_score(asset_id INTEGER PRIMARY KEY, overall REAL);
CREATE TABLE provenance(k TEXT PRIMARY KEY, v TEXT);
"""


# ---------------------------------------------------------------------------
# parameterized default paths (env-derived; no personal literals)
# ---------------------------------------------------------------------------
def default_metadata_db():
    """metadata.db: $DATA_ROOT/metadata.db, else the sibling loupe-pipeline dir."""
    root = os.environ.get("DATA_ROOT")
    if root:
        return os.path.join(root, "metadata.db")
    here = os.path.dirname(os.path.abspath(__file__))            # …/loupe/enrichment
    return os.path.join(os.path.dirname(os.path.dirname(here)), "loupe-pipeline", "metadata.db")


def default_out_db():
    """Output db: a NEW file under $DATA_ROOT (or beside the app), never the live one."""
    root = os.environ.get("DATA_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "apple-enrichment.new.db")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def log(msg):
    print(msg, flush=True)


def parse_iso_to_epoch(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def base_no_ext(fn):
    return re.sub(r'\.[^.]+$', '', fn).lower()


def live_base(fn):
    # video half of a Live Photo is named e.g. IMG_0665_HEVC.MOV; the HEIC is IMG_0665.HEIC
    return re.sub(r'_hevc$', '', base_no_ext(fn))


def strip_collision_suffix(fnl, msize):
    """icloudpd renames same-original_filename collisions by appending -<filesize> immediately
    before the extension (IMG_5688.mov -> IMG_5688-42660297.MOV), and metadata.db stores the
    SUFFIXED name while osxphotos's original_filename stays clean. Return the clean name (suffix
    stripped) IFF the trailing -<digits> exactly equal this asset's file_size_bytes — the digits
    ARE the filesize key, so legitimate trailing -<number> names (OF_2022-03-28-2) are untouched.
    Returns None when there is nothing to normalize. Mirrors ingest's collision-suffix handling."""
    if msize is None:
        return None
    m = re.match(r'^(.*)-(\d+)(\.[^.]+)$', fnl)
    if m and m.group(2) == str(msize):
        return m.group(1) + m.group(3)
    return None


def ro_uri(path):
    return "file:%s?mode=ro" % os.path.abspath(path)


def temp_copy(path, label):
    """Work on a /tmp copy of a source index so the original is never touched/locked."""
    fd, tmp = tempfile.mkstemp(prefix=label + "-", suffix=".sqlite")
    os.close(fd)
    shutil.copy2(path, tmp)
    return tmp


# ---------------------------------------------------------------------------
# 1. bridge CSV (osxphotos default 40-col export)
# ---------------------------------------------------------------------------
def parse_bridge(bridge_csv):
    """Return list of bridge dicts. Skips the leaked osxphotos --field stderr that
    precedes the real header; parses only rows whose first column is a UUID."""
    with open(bridge_csv, newline='', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    try:
        start = next(i for i, l in enumerate(lines)
                     if l.startswith('uuid,filename,original_filename,'))
    except StopIteration:
        raise SystemExit("ERROR: could not find the osxphotos CSV header "
                         "(uuid,filename,original_filename,…) in %s" % bridge_csv)
    hdr = next(csv.reader([lines[start]]))
    H = {c: i for i, c in enumerate(hdr)}
    need = ('uuid', 'original_filename', 'date', 'persons', 'live_photo', 'ismovie')
    missing = [c for c in need if c not in H]
    if missing:
        raise SystemExit("ERROR: bridge CSV missing columns %s" % missing)
    bridge = []
    for r in csv.reader(lines[start + 1:]):
        if not r or len(r) < len(hdr):
            continue
        uuid = r[H['uuid']]
        if not re.match(r'^[0-9A-Fa-f]{8}-', uuid):
            continue
        ofn = r[H['original_filename']]
        persons_raw = r[H['persons']]
        persons = ([p.strip() for p in persons_raw.split(',')
                    if p.strip() and p.strip() != '_UNKNOWN_'] if persons_raw else [])
        bridge.append(dict(
            uuid=uuid, ofn=ofn, ofn_l=ofn.lower(),
            epoch=parse_iso_to_epoch(r[H['date']]),
            live=(r[H['live_photo']] == 'True'),
            ismovie=(r[H['ismovie']] == 'True'),
            persons=persons))
    return bridge


def bridge_indices(bridge):
    by_fn = defaultdict(list)
    by_base = defaultdict(list)     # live-photo video halves
    for b in bridge:
        by_fn[b['ofn_l']].append(b)
        if b['live']:
            by_base[base_no_ext(b['ofn'])].append(b)
    fn_uniq_bridge = {k for k, v in by_fn.items() if len(v) == 1}
    return by_fn, by_base, fn_uniq_bridge


# ---------------------------------------------------------------------------
# 2. metadata assets + the filename+timestamp matcher
# ---------------------------------------------------------------------------
def load_assets(metadata_db):
    con = sqlite3.connect(ro_uri(metadata_db), uri=True)
    rows = con.execute(
        "SELECT id, filename, capture_timestamp, is_live_photo_video, extension "
        "FROM assets").fetchall()
    con.close()
    return rows


def _pick_by_date(cands, cap):
    if cap is None:
        return None, None
    best = None
    bestd = None
    for b in cands:
        if b['epoch'] is None:
            continue
        d = abs(b['epoch'] - cap)
        if bestd is None or d < bestd:
            best, bestd = b, d
    return best, bestd


def match_assets(assets, by_fn, by_base, fn_uniq_bridge):
    """Heuristic UUID bridge keyed to metadata.db assets.id. Returns
    (matches: asset_id -> (uuid, method, confidence), stats: dict).
    Ambiguous filenames with no usable timestamp are REFUSED, not guessed."""
    fn_count_meta = defaultdict(int)
    for _id, fn, *_ in assets:
        if fn:
            fn_count_meta[fn.lower()] += 1
    matches = {}
    stats = defaultdict(int)
    for _id, fn, cap, is_lpv, ext in assets:
        if not fn:
            stats['no_filename'] += 1
            continue
        fnl = fn.lower()
        cands = by_fn.get(fnl)
        chosen = method = None
        if cands:
            b, d = _pick_by_date(cands, cap)
            uniq = (fnl in fn_uniq_bridge and fn_count_meta[fnl] == 1)
            if b is not None and d is not None and d <= TOL:
                chosen, method = b, 'filename+date'
            elif b is not None and d is not None and d <= TZ:
                chosen, method = b, 'filename+date_tz'   # same name, within tz offset
            elif uniq:
                chosen, method = cands[0], 'filename_unique'   # globally unique name; trust it
            elif len(cands) == 1 and cap is None:
                chosen, method = cands[0], 'filename_only'      # single candidate, no date to check
            else:
                stats['ambiguous_unresolved'] += 1              # refuse rather than mis-stamp
                continue
        else:
            # no filename match: try the live-photo motion half (IMG_xxxx_HEVC.MOV → still)
            if (is_lpv == 1) or (ext and ext.lower() in ('mov', 'mp4', 'm4v')):
                bc = by_base.get(live_base(fn))
                if bc:
                    b, d = _pick_by_date(bc, cap)
                    if b is not None:
                        chosen, method = b, 'live_photo_video'
        if chosen:
            matches[_id] = (chosen['uuid'], method, CONF[method])
            stats[method] += 1
        else:
            stats['unmatched'] += 1
    return matches, stats


def uuid_to_assets(matches):
    u2a = defaultdict(list)
    for aid, (u, m, c) in matches.items():
        u2a[u].append(aid)
    return u2a


# ---------------------------------------------------------------------------
# bundle path (mac_enrich.py records.jsonl) — additive to the CSV path above.
# Same bridge dict shape as parse_bridge (so bridge_indices is reused verbatim), plus
# filesize/cloud_guid/fingerprint/score. The matcher below tightens with filesize.
# ---------------------------------------------------------------------------
def open_bundle(path):
    """Resolve a bundle (an unpacked dir OR a .tgz) to (records_jsonl, leo_sqlite,
    manifest, tmpdir). If a .tgz, it is unpacked to a temp dir the caller removes. The
    leo filename is taken from the manifest (fallback leo.sqlite); a missing index -> None
    (labels just won't populate)."""
    tmp = None
    if os.path.isdir(path):
        d = path
    else:
        tmp = tempfile.mkdtemp(prefix="loupe-bundle-")
        with tarfile.open(path) as t:
            try:
                t.extractall(tmp, filter='data')   # py3.12+: refuse unsafe members
            except TypeError:
                t.extractall(tmp)                  # older pythons: no filter kwarg
        d = tmp
    manifest = {}
    mp = os.path.join(d, "manifest.json")
    if os.path.exists(mp):
        with open(mp, encoding='utf-8') as f:
            manifest = json.load(f)
    records = os.path.join(d, "records.jsonl")
    if not os.path.exists(records):
        raise SystemExit("ERROR: bundle has no records.jsonl: %s" % d)
    leo_name = (manifest.get("search_index") or {}).get("filename") or "leo.sqlite"
    leo = os.path.join(d, leo_name)
    if not os.path.exists(leo):
        leo = None
    return records, leo, manifest, tmp


def parse_bundle(records_path):
    """Parse the Mac helper's records.jsonl (one JSON object per line) into bridge dicts,
    parse_bridge-shaped plus filesize/cloud_guid/fingerprint/score. Persons: _UNKNOWN_ and
    blanks stripped (identical policy to the CSV path)."""
    bridge = []
    with open(records_path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            uuid = r.get('uuid')
            if not uuid:
                continue
            ofn = r.get('original_filename') or ''
            persons = [p.strip() for p in (r.get('persons') or [])
                       if isinstance(p, str) and p.strip() and p.strip() != '_UNKNOWN_']
            bridge.append(dict(
                uuid=uuid, cloud_guid=r.get('cloud_guid'), fingerprint=r.get('fingerprint'),
                ofn=ofn, ofn_l=ofn.lower(),
                epoch=parse_iso_to_epoch(r.get('date')),
                filesize=r.get('original_filesize'),
                live=bool(r.get('live_photo')), ismovie=bool(r.get('ismovie')),
                score=r.get('score_overall'), persons=persons))
    return bridge


def load_assets_ext(metadata_db):
    """Like load_assets, plus file_size_bytes (6th column) for the filesize-tightened
    bundle matcher. load_assets is left untouched for the CSV path."""
    con = sqlite3.connect(ro_uri(metadata_db), uri=True)
    rows = con.execute(
        "SELECT id, filename, capture_timestamp, is_live_photo_video, extension, "
        "file_size_bytes FROM assets").fetchall()
    con.close()
    return rows


def match_assets_tightened(assets, by_fn, by_base, fn_uniq_bridge):
    """Filesize-tightened UUID bridge for the bundle path. Mirrors match_assets' tiers and
    adds two, using metadata.db.file_size_bytes (6th asset column) vs the record's
    original_filesize as an EXACT disambiguator:
        filename+filesize  — single filename candidate, exact filesize, date diverged/absent.
        filesize_tiebreak  — several metadata rows share the filename; exact filesize picks
                             exactly one (recovers cases the date-only matcher REFUSED).
    Discipline preserved: filesize PROMOTES, never VETOES — a clean filename+exact-second-date
    match is never refused because the filesize differs (edits/renditions legitimately differ),
    and identical-name+identical-size duplicates are still REFUSED rather than guessed."""
    fn_count_meta = defaultdict(int)
    for row in assets:
        fn = row[1]
        if fn:
            fn_count_meta[fn.lower()] += 1
    matches = {}
    stats = defaultdict(int)
    for _id, fn, cap, is_lpv, ext, msize in assets:
        if not fn:
            stats['no_filename'] += 1
            continue
        fnl = fn.lower()
        cands = by_fn.get(fnl)
        if not cands:
            # icloudpd collision-renamed original (IMG_5688-42660297.MOV): the bridge keys on
            # osxphotos's clean original_filename, so the suffixed metadata name misses. Retry the
            # lookup under the normalized clean name — only when the suffix == this asset's filesize.
            norm = strip_collision_suffix(fnl, msize)
            if norm is not None:
                cands = by_fn.get(norm)
        chosen = method = None
        if cands:
            b, d = _pick_by_date(cands, cap)
            fsz_exact = ([c for c in cands if c['filesize'] == msize]
                         if msize is not None else [])
            uniq = (fnl in fn_uniq_bridge and fn_count_meta[fnl] == 1)
            # Filesize-exact candidates that are THEMSELVES exact-second date matches — used
            # only to arbitrate ties inside the date tier, never to override a date match.
            fsz_date = [c for c in fsz_exact if cap is not None and c['epoch'] is not None
                        and abs(c['epoch'] - cap) <= TOL]
            if b is not None and d is not None and d <= TOL:
                # Exact-second date wins, always. When a recycled filename yields several
                # candidates all within TOL, prefer a filesize-exact one (same-tier promotion
                # among equally-good date matches; never a veto). True duplicates that are
                # filesize-AND-second-identical stay indistinguishable — pick the date-closest.
                chosen = (_pick_by_date(fsz_date, cap)[0] if fsz_date else b)
                method = 'filename+date'
            elif len(fsz_exact) == 1:
                chosen = fsz_exact[0]                               # exact filesize identity
                method = 'filesize_tiebreak' if len(cands) > 1 else 'filename+filesize'
            elif b is not None and d is not None and d <= TZ:
                chosen, method = b, 'filename+date_tz'
            elif uniq:
                chosen, method = cands[0], 'filename_unique'
            elif len(cands) == 1 and cap is None:
                chosen, method = cands[0], 'filename_only'
            else:
                stats['ambiguous_unresolved'] += 1                  # refuse rather than mis-stamp
                continue
        else:
            # no filename match: live-photo motion half (IMG_xxxx_HEVC.MOV → still)
            if (is_lpv == 1) or (ext and ext.lower() in ('mov', 'mp4', 'm4v')):
                bc = by_base.get(live_base(fn))
                if bc:
                    fsz_exact = ([c for c in bc if c['filesize'] == msize]
                                 if msize is not None else [])
                    if len(fsz_exact) == 1:
                        chosen, method = fsz_exact[0], 'live_photo_video'
                    else:
                        b, d = _pick_by_date(bc, cap)
                        if b is not None:
                            chosen, method = b, 'live_photo_video'
        if chosen:
            matches[_id] = (chosen['uuid'], method, CONF[method])
            stats[method] += 1
        else:
            stats['unmatched'] += 1
    return matches, stats


def build_scores_bundle(bridge, matches):
    """Apple aesthetic 'overall' carried in the bundle records, mapped to matched assets."""
    uuid_score = {b['uuid']: b['score'] for b in bridge if b.get('score') is not None}
    rows = []
    for aid, (u, m, c) in matches.items():
        s = uuid_score.get(u)
        if s is not None:
            rows.append((aid, float(s)))
    return rows


# ---------------------------------------------------------------------------
# 3. labels — version-aware (osxphotos API preferred, leo direct-decode fallback)
# ---------------------------------------------------------------------------
def _f16(b):
    return struct.unpack('<e', b)[0]


def decode_leo_labels(leo_db, u2a):
    """Hand-rolled decode of the leo.sqlite/psi.sqlite search index.
    Labels are plain text in lexicon.content; items.lexeme_ids is a packed LE uint32
    array joined to lexicon.lexeme_id; items.lexeme_scores is a parallel float16 array.
    Returns label_rows: list of (asset_id, category_name, term, score)."""
    lc = sqlite3.connect(ro_uri(leo_db), uri=True)
    lex_by_id = defaultdict(list)
    seen_pair = set()
    for lid, cat, content in lc.execute("SELECT lexeme_id,category,content FROM lexicon"):
        if cat in WANTED_CATS and content and (lid, cat) not in seen_pair:
            seen_pair.add((lid, cat))
            lex_by_id[lid].append((cat, content))
    label_rows = []
    leo_items = leo_hit = 0
    for uuid, lids_blob, lsc_blob in lc.execute(
            "SELECT identifier, lexeme_ids, lexeme_scores FROM items WHERE type=1"):
        leo_items += 1
        aids = u2a.get(uuid)
        if not aids:
            continue
        leo_hit += 1
        ids = struct.unpack('<%dI' % (len(lids_blob) // 4), lids_blob)
        nsc = len(lsc_blob) // 2
        for i, lid in enumerate(ids):
            terms = lex_by_id.get(lid)
            if not terms:
                continue
            try:
                sc = _f16(lsc_blob[i * 2:i * 2 + 2]) if i < nsc else None
            except Exception:
                sc = None
            seen = set()
            for cat, content in terms:
                if cat in seen:
                    continue
                seen.add(cat)
                for aid in aids:
                    label_rows.append((aid, WANTED_CATS[cat], content, sc))
    lc.close()
    log("[leo]   %d/%d leo type=1 items mapped to our assets; %d label rows"
        % (leo_hit, leo_items, len(label_rows)))
    return label_rows


def osxphotos_available(library=None):
    """True iff the osxphotos Python API can be imported AND open the library's search
    index (psi.sqlite on stable macOS). On Linux / macOS-27-beta this returns False, so
    the leo direct-decode fallback runs. The probe never raises."""
    try:
        import osxphotos  # noqa: F401
    except Exception:
        return False
    try:
        db = osxphotos.PhotosDB(library) if library else osxphotos.PhotosDB()
        # search_info / labels require a readable psi.sqlite; touching one photo proves it.
        for p in db.photos():
            _ = p.labels
            break
        return True
    except Exception:
        return False


def osxphotos_labels(u2a, library=None):
    """Labels via the osxphotos API (stable-macOS path). Maps each PhotoInfo by UUID to our
    asset ids. Apple's API exposes a flat label list (and richer search_info) but not the
    leo numeric category; we tag these 'scene' to match the dominant category, which is what
    the app consumes. Returns label_rows."""
    import osxphotos
    db = osxphotos.PhotosDB(library) if library else osxphotos.PhotosDB()
    rows = []
    hit = 0
    for p in db.photos():
        aids = u2a.get(p.uuid)
        if not aids:
            continue
        hit += 1
        terms = list(dict.fromkeys((p.labels or []) + (p.labels_normalized or [])))
        for term in terms:
            for aid in aids:
                rows.append((aid, 'scene', term, None))
    log("[osxphotos-labels] %d photos mapped; %d label rows" % (hit, len(rows)))
    return rows


def build_labels(u2a, leo_db=None, library=None):
    """Version-aware label builder. Detects and logs which path runs."""
    if osxphotos_available(library):
        log("[labels] path = osxphotos API (stable macOS / psi.sqlite readable)")
        return osxphotos_labels(u2a, library), 'osxphotos-api'
    if leo_db:
        log("[labels] path = leo.sqlite direct-decode "
            "(osxphotos can't read the index — macOS-27 beta / Linux extract)")
        return decode_leo_labels(leo_db, u2a), 'leo-direct-decode'
    log("[labels] no label source available (no osxphotos library, no leo index) — skipping")
    return [], 'none'


# ---------------------------------------------------------------------------
# 4. persons (named only; _UNKNOWN_ already dropped at parse time)
# ---------------------------------------------------------------------------
def build_persons(bridge, matches):
    uuid_persons = {b['uuid']: b['persons'] for b in bridge if b['persons']}
    rows = []
    for aid, (u, m, c) in matches.items():
        for p in uuid_persons.get(u, []):
            rows.append((aid, p))
    return rows


# ---------------------------------------------------------------------------
# 5. apple aesthetic scores (osxphotos API on Mac, else scores.csv)
# ---------------------------------------------------------------------------
def load_scores_csv(scores_csv, u2a):
    """Load Apple aesthetic 'overall' from a pre-exported scores.csv (uuid,score_overall)."""
    rows = []
    seen = blank = nobridge = 0
    with open(scores_csv, newline='', encoding='utf-8', errors='replace') as f:
        for r in csv.DictReader(f):
            u = (r.get('uuid') or '').strip()
            s = (r.get('score_overall') or '').strip()
            if not u:
                continue
            seen += 1
            try:
                val = float(s)
            except ValueError:
                blank += 1
                continue
            aids = u2a.get(u)
            if not aids:
                nobridge += 1
                continue
            for aid in aids:
                rows.append((aid, val))
    log("[scores] csv uuids %d; skipped blank %d, not-in-bridge %d; %d score rows"
        % (seen, blank, nobridge, len(rows)))
    return rows


def load_scores_osxphotos(u2a, library=None):
    """Apple aesthetic 'overall' via the osxphotos API (Mac path)."""
    import osxphotos
    db = osxphotos.PhotosDB(library) if library else osxphotos.PhotosDB()
    rows = []
    for p in db.photos():
        aids = u2a.get(p.uuid)
        if not aids:
            continue
        sc = getattr(p, 'score', None)
        overall = getattr(sc, 'overall', None) if sc else None
        if overall is None:
            continue
        for aid in aids:
            rows.append((aid, float(overall)))
    log("[scores] osxphotos API → %d score rows" % len(rows))
    return rows


# ---------------------------------------------------------------------------
# write the new db
# ---------------------------------------------------------------------------
def write_db(out_db, matches, label_rows, person_rows, score_rows, provenance,
             uuid_extra=None):
    """uuid_extra: optional {uuid: (cloud_guid, fingerprint)} (bundle path). The CSV path
    passes nothing, so those columns are NULL — additive, the 3-file output is unchanged
    except for the two new nullable columns."""
    if os.path.exists(out_db):
        raise SystemExit("ERROR: output %s already exists — refusing to overwrite. "
                         "Remove it or pass a different --out." % out_db)
    uuid_extra = uuid_extra or {}
    db = sqlite3.connect(out_db)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO asset_uuid VALUES(?,?,?,?,?,?)",
                   [(aid, u, m, c) + uuid_extra.get(u, (None, None))
                    for aid, (u, m, c) in matches.items()])
    db.executemany("INSERT INTO labels VALUES(?,?,?,?)", label_rows)
    db.executemany("INSERT INTO persons VALUES(?,?)", person_rows)
    db.executemany("INSERT OR REPLACE INTO apple_score(asset_id,overall) VALUES(?,?)", score_rows)
    db.executemany("INSERT INTO provenance VALUES(?,?)", list(provenance.items()))
    db.commit()
    db.close()
