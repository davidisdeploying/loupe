#!/usr/bin/env python3
"""Match elsewhere_final.csv rows back to /originals files via metadata.db (READ-ONLY).
Same logic as match_production.py."""
import csv, os, re, sqlite3, sys, json

DB = "/home/david/loupe-pipeline/metadata.db"
CSV_IN = "/home/david/loupe-pipeline/culling/elsewhere_final.csv"
OUT = "/home/david/loupe-pipeline/culling/_match_results_elsewhere.json"

def norm_stem(filename, file_size):
    """Lower-cased stem (no ext); strip a trailing -<digits> ONLY when those digits
    equal the asset's own file_size_bytes (the ingest disambiguation-suffix form)."""
    base, ext = os.path.splitext(filename)
    m = re.search(r'-(\d+)$', base)
    if m and file_size is not None and int(m.group(1)) == file_size:
        base = base[:m.start()]
    return base.lower(), ext.lower().lstrip('.')

# read-only DB connection
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

rows = list(csv.DictReader(open(CSV_IN)))
results = []
for r in rows:
    fn = r["original_filename"]
    csv_stem, csv_ext = os.path.splitext(fn)
    csv_stem_l = csv_stem.lower()
    csv_ext_l = csv_ext.lower().lstrip('.')
    size = int(r["file_size_bytes"])
    dur = float(r["duration_seconds"]) if r["duration_seconds"] else None
    epoch = int(r["capture_epoch"])

    COLS = ("SELECT id, filename, file_size_bytes, duration_seconds, capture_timestamp, "
            "file_sha256, filepath, year FROM assets ")
    cand_by_id = {}
    # (a) filename-stem match: prefix on stem, then verify normalized stem+ext agree
    for a in con.execute(COLS + "WHERE filename LIKE ? ESCAPE '\\'",
        (csv_stem_l.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%',)):
        ns, ne = norm_stem(a["filename"], a["file_size_bytes"])
        if ns == csv_stem_l and ne == csv_ext_l:
            cand_by_id[a["id"]] = a
    # (b) fallback: exact size + capture-timestamp (catches filename quirks e.g. spaces)
    for a in con.execute(COLS + "WHERE file_size_bytes = ? AND capture_timestamp = ?",
        (size, epoch)):
        cand_by_id.setdefault(a["id"], a)
    cands = list(cand_by_id.values())

    # score candidates: size exact, duration close, capture epoch match
    def score(a):
        s = 0
        if a["file_size_bytes"] == size: s += 100
        if dur is not None and a["duration_seconds"] is not None and abs(a["duration_seconds"] - dur) < 1.0: s += 10
        if a["capture_timestamp"] == epoch: s += 1
        return s

    cands.sort(key=score, reverse=True)
    # how many are a *full* match (size+duration+capture all agree)?
    def full(a):
        return (a["file_size_bytes"] == size
                and (dur is None or (a["duration_seconds"] is not None and abs(a["duration_seconds"]-dur) < 1.0))
                and a["capture_timestamp"] == epoch)
    fulls = [a for a in cands if full(a)]
    # size-exact matches (primary disambiguator)
    size_exact = [a for a in cands if a["file_size_bytes"] == size]

    status = "OK"
    chosen = None
    note = ""
    if not cands:
        status = "NO_MATCH"
    elif len(size_exact) == 1:
        chosen = size_exact[0]
        if not full(chosen): note = "size-unique; dur/capture partial"
    elif len(fulls) == 1:
        chosen = fulls[0]
    elif len(size_exact) > 1:
        # tie-break on full match
        if len(fulls) == 1:
            chosen = fulls[0]
        else:
            status = "AMBIGUOUS"
            note = f"{len(size_exact)} size-exact candidates"
    elif len(cands) == 1:
        chosen = cands[0]
        note = "single stem candidate; size differs"
        if chosen["file_size_bytes"] != size:
            status = "SIZE_MISMATCH"
    else:
        status = "AMBIGUOUS"
        note = f"{len(cands)} stem candidates, none size-exact"

    results.append({
        "uuid": r["uuid"], "original_filename": fn, "csv_size": size,
        "status": status, "note": note,
        "n_cands": len(cands), "n_size_exact": len(size_exact),
        "chosen_id": chosen["id"] if chosen else None,
        "chosen_filename": chosen["filename"] if chosen else None,
        "originals_path": chosen["filepath"] if chosen else None,
        "db_size": chosen["file_size_bytes"] if chosen else None,
        "sha256": chosen["file_sha256"] if chosen else None,
        "year": chosen["year"] if chosen else None,
    })

con.close()

ok = [r for r in results if r["status"] == "OK"]
bad = [r for r in results if r["status"] != "OK"]
print(f"TOTAL rows: {len(results)}")
print(f"OK matches: {len(ok)}")
print(f"Problems:   {len(bad)}")
for b in bad:
    print(f"  [{b['status']}] {b['original_filename']} uuid={b['uuid']} {b['note']} (cands={b['n_cands']}, size_exact={b['n_size_exact']})")

# missing sha?
nosha = [r for r in ok if not r["sha256"]]
print(f"OK matches missing sha256 in DB: {len(nosha)}")
for r in nosha:
    print(f"  NO_SHA {r['original_filename']} id={r['chosen_id']}")

# also flag any chosen path NOT under /originals (should be all-originals this pass)
notorig = [r for r in ok if r["originals_path"] and "/originals/" not in r["originals_path"]]
print(f"OK matches whose DB filepath is NOT under /originals: {len(notorig)}")
for r in notorig:
    print(f"  NOT_ORIG {r['original_filename']} -> {r['originals_path']}")

json.dump(results, open(OUT,"w"), indent=1)
print(f"wrote {OUT}")
