#!/usr/bin/env python3
"""
mac_enrich.py — Apple Photos enrichment extractor (Mac side; piece 1 of 3).

DEVELOPED on the Linux Mini, but RUNS on the MacBook where the Photos library and
osxphotos live. Produces ONE local bundle the Mini consumes via enrichment/build.py.

    osxphotos run mac_enrich.py            # full library
    osxphotos run mac_enrich.py 200        # first 200 photos only (fast validation)

Read-only w.r.t. the Mac (the live search index is COPIED, never opened read-write).
Stdlib + osxphotos only.

Network discipline (scoped — NOT a blanket claim):
  * The DEFAULT metadata extraction above makes NO network calls: it reads the live
    Photos DB + search index and writes a local bundle. Nothing is downloaded.
  * The OPT-IN `export` mode (see below) is the one exception: it may DOWNLOAD missing
    originals (download_missing=True) over the user's ALREADY-AUTHENTICATED Photos
    session. NO Apple password is handled here — it rides the logged-in macOS session.

Opt-in originals-export test mode (additive — validates the credential-free download):
    osxphotos run mac_enrich.py export "<album>" "<out_dir>" [limit]
  Exports the ORIGINAL of each photo in <album> to <out_dir> (download_missing=True),
  then writes export-test-manifest.jsonl + a stdout verdict. MacBook-only (needs a real
  Photos library + osxphotos); it cannot run on the Mini.

Bundle  ~/loupe-enrich-bundle-<YYYYMMDD-HHMMSS>.tgz  contains:
  records.jsonl   one JSON object per photo:
                    uuid, cloud_guid, fingerprint, original_filename, date (ISO8601),
                    width, height, original_filesize, score_overall, score_all,
                    persons (list[str]), live_photo, ismovie
  <index>.sqlite  the macOS scene-label search index (+ -wal/-shm sidecars if present),
                  copied verbatim for HOST-SIDE decode by build.py's leo path
  manifest.json   osxphotos/macOS versions, counts, which index matched

How build.py (piece 2) consumes this:
  * records.jsonl is the UUID bridge + persons + Apple aesthetic score. Its field set is a
    SUPERSET of build.py's CSV bridge needs (uuid, original_filename, date, persons,
    live_photo, ismovie) — plus cloud_guid/fingerprint/scores for the leo join + future use.
  * the bundled search index feeds build.py's decode_leo_labels() (lexicon + items tables).
    Labels are NOT taken from osxphotos here: .labels_normalized is EMPTY on macOS 27
    (osxphotos can't find the index), so this helper's only label job is LOCATE + COPY.

osxphotos API used (probed live, macOS 27.0 / osxphotos 0.76.1 — coded against THIS):
  PhotoInfo.uuid, .cloud_guid, .fingerprint, .original_filename, .date (tz-aware),
  .width, .height, .original_filesize, .score.overall, .score.asdict(), .persons,
  .live_photo, .ismovie. Everything is read DEFENSIVELY (missing/None -> null, never crash).
"""
import inspect
import json
import os
import platform
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import traceback
from datetime import datetime

import osxphotos

# leo/psi numeric label categories build.py's decoder keeps. Mirrors
# enrichment/common.py:WANTED_CATS — duplicated here so this helper stays standalone
# (only mac_enrich.py is copied to the MacBook; common.py is not).
WANTED_CATS = (4000, 4010, 4020, 4060, 4090, 4120, 4130)

# Schema signature build.py's decode_leo_labels() (common.py:264) requires of the
# search index:  lexicon(lexeme_id, category, content)
#                items(identifier, lexeme_ids, lexeme_scores, type)   [decoder: WHERE type=1]
SIG_TABLES = {
    "lexicon": ("lexeme_id", "category", "content"),
    "items": ("identifier", "lexeme_ids", "lexeme_scores", "type"),
}


# --------------------------------------------------------------------------- #
# small, defensive helpers
# --------------------------------------------------------------------------- #
def log(msg):
    print(msg, flush=True)


def g(obj, attr, default=None):
    """Defensive attribute read: missing attr / raising getter / None -> default.
    Note False/0 are returned as-is (only None falls through to default)."""
    try:
        v = getattr(obj, attr)
    except Exception:
        return default
    return default if v is None else v


def iso(dt):
    """tz-aware datetime -> ISO8601 string, defensively."""
    try:
        return dt.isoformat() if dt is not None else None
    except Exception:
        return None


def jsonable(v):
    """Coerce score.asdict()/persons into guaranteed-JSON-serializable values."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return str(v)


def connect_ro(path):
    """Read-only connection to a possibly-live sqlite file. Tries a plain mode=ro
    open first, then falls back to immutable=1 — a live WAL-mode DB will refuse a
    plain read-only open (it can't create the -shm). immutable=1 reads schema/pages
    without locking; we only use this for the live SOURCE, schema-probe only.
    Caller closes."""
    p = os.path.abspath(path)
    last = None
    for extra in ("", "&immutable=1"):
        con = None
        try:
            con = sqlite3.connect("file:%s?mode=ro%s" % (p, extra), uri=True)
            con.execute("SELECT 1")  # force the open to actually happen here
            return con
        except Exception as e:  # noqa: BLE001 - report, try next mode
            last = e
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
    raise last if last is not None else sqlite3.OperationalError("cannot open %s" % p)


def schema_matches(con):
    """True iff the open DB has the lexicon+items tables with the columns the leo
    decoder needs (table/column signature only — no data read)."""
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for tbl, cols in SIG_TABLES.items():
        if tbl not in tables:
            return False
        present = {r[1] for r in con.execute("PRAGMA table_info(%s)" % tbl)}  # tbl is a constant
        if not set(cols).issubset(present):
            return False
    return True


# --------------------------------------------------------------------------- #
# step 1 — LOCATE the search index
# --------------------------------------------------------------------------- #
def list_candidates(library):
    """Every *.sqlite under <library>/database/search/, else <library>/database/."""
    for d in (os.path.join(library, "database", "search"),
              os.path.join(library, "database")):
        if os.path.isdir(d):
            cands = sorted(os.path.join(d, f) for f in os.listdir(d)
                           if f.lower().endswith(".sqlite"))
            if cands:
                return d, cands
    return None, []


def locate_index(library):
    """Pick the search index whose schema matches the leo decoder. Reports every
    candidate. Never aborts: returns (chosen_path or None, infos)."""
    log("[1/locate] library: %s" % library)
    d, cands = list_candidates(library)
    if not cands:
        log("[1/locate] no *.sqlite under database/search/ or database/ — "
            "labels will NOT populate (records.jsonl still extracted)")
        return None, []
    log("[1/locate] scanning %s (%d candidate .sqlite)" % (d, len(cands)))
    chosen = None
    infos = []
    for c in cands:
        size = os.path.getsize(c) if os.path.exists(c) else 0
        matched = False
        err = None
        try:
            con = connect_ro(c)
            try:
                matched = schema_matches(con)
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001 - one bad candidate must not abort
            err = str(e)
        infos.append({"name": os.path.basename(c), "path": c, "size": size,
                      "matched": matched, "error": err})
        flag = "MATCH" if matched else ("ERR: %s" % err if err else "no")
        log("           %-30s %12d bytes  signature=%s"
            % (os.path.basename(c), size, flag))
        if matched and chosen is None:
            chosen = c
    if chosen:
        log("[1/locate] matched search index: %s" % os.path.basename(chosen))
    else:
        log("[1/locate] NO candidate matched the lexicon/items signature — "
            "labels will NOT populate. Continuing with records.jsonl only.")
    return chosen, infos


# --------------------------------------------------------------------------- #
# step 2 — COPY (WAL-safe) into staging + verify the decode signature
# --------------------------------------------------------------------------- #
def stage_index(chosen, staging):
    """Copy the chosen index + -wal/-shm sidecars into staging, then verify on the
    PRIVATE copy that the decoder's signature queries return rows. The live source is
    only ever file-copied, never opened read-write. Returns (basename or None, verify)."""
    base = os.path.basename(chosen)
    staged_main = os.path.join(staging, base)
    copied = []
    for suffix in ("", "-wal", "-shm"):
        src = chosen + suffix
        if os.path.exists(src):
            shutil.copy2(src, staged_main + suffix)
            copied.append(base + suffix)
    log("[2/copy] copied: %s" % (", ".join(copied) if copied else "(nothing)"))

    verify = {"lexicon_wanted_rows": 0, "items_type1_rows": 0, "ok": False, "error": None}
    # The staged copy is ours: a normal connection may read its WAL and (on close)
    # fold it into the main file. That makes the bundled .sqlite self-contained.
    try:
        con = sqlite3.connect(staged_main)
        try:
            qmarks = ",".join("?" * len(WANTED_CATS))
            verify["lexicon_wanted_rows"] = con.execute(
                "SELECT count(*) FROM lexicon WHERE category IN (%s)" % qmarks,
                WANTED_CATS).fetchone()[0]
            verify["items_type1_rows"] = con.execute(
                "SELECT count(*) FROM items WHERE type=1").fetchone()[0]
            verify["ok"] = (verify["lexicon_wanted_rows"] > 0
                            and verify["items_type1_rows"] > 0)
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 - a bad copy must not abort the bundle
        verify["error"] = str(e)
    log("[2/copy] decode check: lexicon(wanted)=%d items(type=1)=%d ok=%s%s"
        % (verify["lexicon_wanted_rows"], verify["items_type1_rows"], verify["ok"],
           "" if not verify["error"] else "  err=%s" % verify["error"]))
    return (base if copied else None), verify


# --------------------------------------------------------------------------- #
# step 3 — EXTRACT per-photo, STREAMING to records.jsonl
# --------------------------------------------------------------------------- #
def build_record(p):
    """One photo -> a flat, JSON-safe dict. Every field read defensively."""
    overall = score_all = None
    sc = g(p, "score")
    if sc is not None:
        ov = g(sc, "overall")
        try:
            overall = float(ov) if ov is not None else None
        except (TypeError, ValueError):
            overall = None
        try:
            score_all = jsonable(sc.asdict())
        except Exception:
            score_all = None
    persons = [str(x) for x in (g(p, "persons", []) or []) if x is not None]
    return {
        "uuid": g(p, "uuid"),
        "cloud_guid": g(p, "cloud_guid"),
        "fingerprint": g(p, "fingerprint"),
        "original_filename": g(p, "original_filename"),
        "date": iso(g(p, "date")),
        "width": g(p, "width"),
        "height": g(p, "height"),
        "original_filesize": g(p, "original_filesize"),
        "score_overall": overall,
        "score_all": score_all,
        "persons": persons,
        "live_photo": g(p, "live_photo"),
        "ismovie": g(p, "ismovie"),
    }


def extract_records(photos, jsonl_path):
    """Stream one JSON line per photo. ONE output FD; nothing heavy accumulated."""
    written = 0
    with open(jsonl_path, "w", encoding="utf-8") as out:
        for p in photos:
            try:
                rec = build_record(p)
            except Exception as e:  # noqa: BLE001 - never lose the stream over one photo
                rec = {"uuid": g(p, "uuid"), "_error": repr(e)}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
            if written % 2000 == 0:
                log("[3/extract] %d records..." % written)
                out.flush()
    log("[3/extract] %d records written -> %s" % (written, os.path.basename(jsonl_path)))
    return written


# --------------------------------------------------------------------------- #
# step 4 — BUNDLE
# --------------------------------------------------------------------------- #
def write_bundle(staging, records_written, library_count, idx_base, idx_infos,
                 verify, library_path, ts):
    idx_size = None
    if idx_base:
        sp = os.path.join(staging, idx_base)
        idx_size = os.path.getsize(sp) if os.path.exists(sp) else None
    manifest = {
        "tool": "enrichment/mac_enrich.py (piece 1 of 3)",
        "created": ts,
        "osxphotos_version": getattr(osxphotos, "__version__", "unknown"),
        "macos_version": platform.mac_ver()[0] or platform.platform(),
        "python_version": platform.python_version(),
        "library_path": library_path,
        "library_photo_count": library_count,
        "records_written": records_written,
        "search_index": {
            "filename": idx_base,
            "size": idx_size,
            "matched": bool(verify.get("ok")),
            "decode_check": verify,
            "candidates": [{"name": i["name"], "size": i["size"],
                            "matched": i["matched"], "error": i["error"]}
                           for i in idx_infos],
        },
    }
    with open(os.path.join(staging, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    out_path = os.path.join(os.path.expanduser("~"),
                            "loupe-enrich-bundle-%s.tgz" % ts)
    with tarfile.open(out_path, "w:gz") as tar:
        for name in sorted(os.listdir(staging)):
            tar.add(os.path.join(staging, name), arcname=name)
    return out_path, manifest


# --------------------------------------------------------------------------- #
# OPT-IN originals-export test mode (additive; MacBook-only)
# --------------------------------------------------------------------------- #
EXPORT_USAGE = ('usage: osxphotos run mac_enrich.py export "<album>" "<out_dir>" [limit]'
                '   (MacBook only — needs a real Photos library)')


def export_originals(album, out_dir, limit):
    """Validate the credential-free Mac download on an album-scoped subset.

    Exports the ORIGINAL of each photo whose .albums include <album> to <out_dir>
    with download_missing=True (rides the user's already-authenticated Photos session;
    NO Apple password). Writes export-test-manifest.jsonl and prints a verdict.
    Returns a process exit code (0 = something exported, non-zero otherwise)."""
    photosdb = osxphotos.PhotosDB()

    # 2. select by album membership through the existing defensive reader.
    selected = [p for p in photosdb.photos() if album in (g(p, "albums") or [])]
    if not selected:
        log('[export] no photos found in album %r — nothing to export.' % album)
        return 2
    if limit:
        selected = selected[:limit]
    log('[export] album %r: exporting %d original(s) -> %s' % (album, len(selected), out_dir))

    # 3. ensure the output dir exists.
    os.makedirs(out_dir, exist_ok=True)

    manifest_path = os.path.join(out_dir, "export-test-manifest.jsonl")
    rows = []
    n_icloud = n_local = n_sizematch = n_fail = 0
    errors = []

    with open(manifest_path, "w", encoding="utf-8") as mf:
        for p in selected:
            uuid = g(p, "uuid")
            ofn = g(p, "original_filename")
            try:
                ofs = g(p, "original_filesize")
                try:
                    ofs = int(ofs) if ofs is not None else None
                except (TypeError, ValueError):
                    ofs = None
                # None path => the original is NOT on disk => download_missing fetches it.
                was_icloud_only = g(p, "path") is None

                # Export ONE original file (no edited/live/raw variants).
                written = p.export(out_dir, download_missing=True)
                if not written:
                    raise RuntimeError("no file exported")
                exported_path = written[0]                       # trust the RETURNED path
                exported_size = os.path.getsize(exported_path)
                size_match = (exported_size == ofs) if ofs is not None else None

                if was_icloud_only:
                    n_icloud += 1
                else:
                    n_local += 1
                if size_match:
                    n_sizematch += 1
                rec = {"uuid": uuid, "original_filename": ofn, "original_filesize": ofs,
                       "exported_path": exported_path, "exported_size": exported_size,
                       "was_icloud_only": was_icloud_only, "size_match": size_match,
                       "error": None}
            except TypeError as e:
                # osxphotos 0.76.1 may not accept download_missing= — reveal the REAL
                # signature once so the first Mac run lets us fix it in one pass.
                if "download_missing" in str(e) or "unexpected keyword" in str(e):
                    log("[export] PhotoInfo.export() rejected download_missing=. Real signature:")
                    try:
                        log("    %s" % inspect.signature(osxphotos.PhotoInfo.export))
                    except Exception as se:  # noqa: BLE001
                        log("    (could not introspect signature: %s)" % se)
                    log("[export] Fix the export() kwargs to match the above, then re-run. "
                        "Do NOT guess use_photokit/use_photos_export.")
                    return 3
                rec = {"uuid": uuid, "original_filename": ofn,
                       "original_filesize": None, "exported_path": None,
                       "exported_size": None, "was_icloud_only": None,
                       "size_match": None, "error": str(e)}
                n_fail += 1
                errors.append("%s: %s" % (ofn or uuid, e))
            except Exception as e:  # noqa: BLE001 - one failure must not abort the run
                rec = {"uuid": uuid, "original_filename": ofn,
                       "original_filesize": None, "exported_path": None,
                       "exported_size": None, "was_icloud_only": None,
                       "size_match": None, "error": str(e)}
                n_fail += 1
                errors.append("%s: %s" % (ofn or uuid, e))
            mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            rows.append(rec)

    # 6. stdout verdict.
    log("\n[export] ===== verdict =====")
    log("[export] selected           : %d" % len(rows))
    log("[export] iCloud-only→fetched : %d" % n_icloud)
    log("[export] already-local copied: %d" % n_local)
    log("[export] size matches        : %d" % n_sizematch)
    log("[export] failures            : %d" % n_fail)
    for e in errors[:5]:
        log("[export]   - %s" % e)
    log("[export] manifest -> %s" % manifest_path)
    return 0 if (len(rows) - n_fail) > 0 else 1


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #
def parse_limit(argv):
    """Optional positional limit (`osxphotos run mac_enrich.py 200`). Bad/absent -> None."""
    if len(argv) > 1 and str(argv[1]).strip():
        try:
            n = int(argv[1])
        except ValueError:
            log("[arg] ignoring non-integer limit %r — running full library" % argv[1])
            return None
        if n > 0:
            return n
        log("[arg] ignoring non-positive limit %r — running full library" % argv[1])
    return None


def main():
    limit = parse_limit(sys.argv)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log("mac_enrich.py — Apple Photos enrichment extractor (run %s)" % ts)
    log("mode: %s" % ("first %d photos" % limit if limit else "FULL library"))

    photosdb = osxphotos.PhotosDB()
    library_path = g(photosdb, "library_path") or "(unknown)"

    chosen, idx_infos = locate_index(library_path)

    staging = tempfile.mkdtemp(prefix="loupe-enrich-")
    try:
        idx_base, verify = None, {"ok": False, "error": "no candidate matched"}
        if chosen:
            idx_base, verify = stage_index(chosen, staging)

        # list() tolerates either a list or a generator from .photos().
        photos = list(photosdb.photos())
        library_count = len(photos)
        log("[3/extract] library has %d photos; extracting %s"
            % (library_count, "first %d" % limit if limit else "all"))
        to_do = photos if not limit else photos[:limit]
        written = extract_records(to_do, os.path.join(staging, "records.jsonl"))

        out_path, manifest = write_bundle(
            staging, written, library_count, idx_base, idx_infos, verify, library_path, ts)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    size = os.path.getsize(out_path)
    log("\n[4/bundle] wrote %s  (%d bytes / %.1f MB)" % (out_path, size, size / 1e6))
    log("[manifest]\n%s" % json.dumps(manifest, indent=2))
    log("\nDONE. scp this .tgz back to the Mini for build.py (piece 2).")


def _dispatch(argv):
    """Positional sub-mode router (collision-proof against osxphotos run's own parser).
    `export` -> originals-export test mode; anything else -> today's metadata extraction
    (optional int limit), byte-for-byte unchanged."""
    if len(argv) > 1 and argv[1] == "export":
        album = argv[2] if len(argv) > 2 else ""
        out_dir = argv[3] if len(argv) > 3 else ""
        if not album.strip() or not out_dir.strip():
            log(EXPORT_USAGE)
            sys.exit(2)
        limit = None
        if len(argv) > 4 and str(argv[4]).strip():
            try:
                n = int(argv[4])
            except ValueError:
                log(EXPORT_USAGE)
                sys.exit(2)
            limit = n if n > 0 else None
        sys.exit(export_originals(album, out_dir, limit))
    main()


if __name__ == "__main__":
    try:
        _dispatch(sys.argv)
    except SystemExit:
        raise
    except Exception:
        log("FATAL:\n" + traceback.format_exc())
        sys.exit(1)
