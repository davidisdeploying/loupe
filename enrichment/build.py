#!/usr/bin/env python3
"""
build.py — reproducible builder for apple-enrichment.db.

Turns the loose Apple-Photos extraction inputs into a single enrichment database keyed to
metadata.db.assets.id, by left-joining four signals onto the UUID bridge:

    bridge (uuid↔asset_id)  →  labels (version-aware)  +  persons  +  apple aesthetic score

Every path is a parameter with an env-derived default; nothing personal is baked in.

Inputs (any subset; a stage with no input is skipped):
    --bridge    osxphotos default CSV export (uuid,filename,original_filename,date,persons,…)
    --metadata  metadata.db (opened READ-ONLY)                [default: $DATA_ROOT/metadata.db]
    --leo       leo.sqlite / psi.sqlite search-index copy (labels, leo-decode fallback path)
    --scores    scores.csv (uuid,score_overall) — Apple aesthetic, when no Mac API available
    --library   Apple Photos .photoslibrary path — enables the osxphotos API (labels + scores)
    --out       output db (a NEW file)                        [default: $DATA_ROOT/apple-enrichment.new.db]

Discipline:
    * metadata.db is opened mode=ro.
    * --leo is copied to /tmp and worked on the copy (the source index is never touched).
    * --out must NOT exist (refuses to overwrite); the live apple-enrichment.db is never opened.

Examples:
    # Linux, loose inputs (leo-decode + scores.csv path). Inputs live beside this
    # module in enrichment/inputs/ -- see that directory's README. They were loose
    # in ~ on delta until 2026-08-07; consolidating them here is what keeps them
    # travelling with the code.
    python3 build.py --bridge enrichment/inputs/photos-bridge.csv \\
                     --leo enrichment/inputs/leo-copy.sqlite \\
                     --scores enrichment/inputs/scores.csv \\
                     --out /tmp/apple-enrichment.rebuilt.db

    # macOS with a live Photos library (osxphotos API path for labels + scores):
    python3 build.py --bridge bridge.csv --library ~/Pictures/Photos\\ Library.photoslibrary \\
                     --out apple-enrichment.new.db
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C


def _summary(out_db):
    import sqlite3
    db = sqlite3.connect(out_db)
    C.log("\n[written] %s" % out_db)
    for t in ('asset_uuid', 'labels', 'persons', 'apple_score'):
        C.log("   %-12s %d rows" % (t, db.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]))
    C.log("   distinct assets with labels:  %d"
          % db.execute("SELECT COUNT(DISTINCT asset_id) FROM labels").fetchone()[0])
    C.log("   distinct assets with persons: %d"
          % db.execute("SELECT COUNT(DISTINCT asset_id) FROM persons").fetchone()[0])
    db.close()
    C.log("DONE")


def build_from_bundle(args):
    """Bundle path: build from the Mac helper's records.jsonl + leo.sqlite (uuid-keyed
    labels, filesize-tightened bridge)."""
    records_path, leo_path, manifest, tmp = C.open_bundle(args.bundle)
    try:
        C.log("metadata.db : %s (read-only)" % args.metadata)
        C.log("output      : %s" % args.out)
        C.log("bundle      : %s  (osxphotos %s, %s records, leo=%s)"
              % (args.bundle, manifest.get('osxphotos_version', '?'),
                 manifest.get('records_written', '?'),
                 os.path.basename(leo_path) if leo_path else 'NONE'))

        # 1. bridge + filesize-tightened match --------------------------------
        bridge = C.parse_bundle(records_path)
        C.log("[bundle] %d records; %d w/ >=1 named person; %d live; %d movie"
              % (len(bridge), sum(1 for b in bridge if b['persons']),
                 sum(1 for b in bridge if b['live']),
                 sum(1 for b in bridge if b['ismovie'])))
        by_fn, by_base, fn_uniq = C.bridge_indices(bridge)
        assets = C.load_assets_ext(args.metadata)
        C.log("[meta]  %d assets loaded (filesize-aware)" % len(assets))
        matches, stats = C.match_assets_tightened(assets, by_fn, by_base, fn_uniq)
        C.log("[match] %d/%d assets matched to a UUID" % (len(matches), len(assets)))
        for k in sorted(stats):
            C.log("          %-24s %d" % (k, stats[k]))
        u2a = C.uuid_to_assets(matches)

        # 2. labels (leo direct-decode; WAL already folded by the helper) -----
        if leo_path:
            label_rows, label_path = C.build_labels(u2a, leo_db=leo_path)
        else:
            label_rows, label_path = [], 'none (no leo in bundle)'

        # 3. persons ----------------------------------------------------------
        person_rows = C.build_persons(bridge, matches)
        C.log("[persons] %d (asset,person) rows; %d assets with >=1 named person"
              % (len(person_rows), len(set(a for a, _ in person_rows))))

        # 4. scores (from the bundle records) ---------------------------------
        score_rows = C.build_scores_bundle(bridge, matches)
        C.log("[scores] %d score rows (bundle score_overall)" % len(score_rows))

        # 5. write ------------------------------------------------------------
        uuid_extra = {b['uuid']: (b.get('cloud_guid'), b.get('fingerprint')) for b in bridge}
        prov = {
            'source': 'mac bundle (enrichment/mac_enrich.py)',
            'bundle': os.path.abspath(args.bundle),
            'manifest_created': manifest.get('created', ''),
            'osxphotos_version': manifest.get('osxphotos_version', ''),
            'macos_version': manifest.get('macos_version', ''),
            'records': str(manifest.get('records_written', len(bridge))),
            'metadata_db': "%s (read-only)" % args.metadata,
            'label_path': label_path,
            'label_join_field': 'uuid',
            'apple_score': 'bundle score_overall',
            'label_categories': ','.join("%d=%s" % (k, v) for k, v in C.WANTED_CATS.items()),
            'built_by': 'enrichment/build.py --bundle (reproducible)',
        }
        C.write_db(args.out, matches, label_rows, person_rows, score_rows, prov,
                   uuid_extra=uuid_extra)
    finally:
        if tmp and os.path.isdir(tmp):
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    _summary(args.out)


def build_from_csv(args):
    """Original 3-file CSV path — unchanged behaviour (bridge CSV + leo/scores/library)."""
    C.log("metadata.db : %s (read-only)" % args.metadata)
    C.log("output      : %s" % args.out)

    # 1. bridge + match -------------------------------------------------------
    bridge = C.parse_bridge(args.bridge)
    C.log("[bridge] parsed %d Photos assets; %d have >=1 named person; %d live photos"
          % (len(bridge), sum(1 for b in bridge if b['persons']),
             sum(1 for b in bridge if b['live'])))
    by_fn, by_base, fn_uniq = C.bridge_indices(bridge)
    assets = C.load_assets(args.metadata)
    C.log("[meta]  %d assets loaded" % len(assets))
    matches, stats = C.match_assets(assets, by_fn, by_base, fn_uniq)
    C.log("[match] %d/%d assets matched to a UUID" % (len(matches), len(assets)))
    for k in sorted(stats):
        C.log("          %-24s %d" % (k, stats[k]))
    u2a = C.uuid_to_assets(matches)

    # 2. labels (version-aware) ----------------------------------------------
    leo_copy = None
    if args.leo:
        leo_copy = C.temp_copy(args.leo, "leo-work")
    try:
        label_rows, label_path = C.build_labels(u2a, leo_db=leo_copy, library=args.library)
    finally:
        if leo_copy and os.path.exists(leo_copy):
            os.remove(leo_copy)

    # 3. persons --------------------------------------------------------------
    person_rows = C.build_persons(bridge, matches)
    C.log("[persons] %d (asset,person) rows; %d assets with >=1 named person"
          % (len(person_rows), len(set(a for a, _ in person_rows))))

    # 4. scores ---------------------------------------------------------------
    if args.library and C.osxphotos_available(args.library):
        score_rows = C.load_scores_osxphotos(u2a, args.library)
        score_src = "osxphotos API"
    elif args.scores:
        score_rows = C.load_scores_csv(args.scores, u2a)
        score_src = "scores.csv: %s" % args.scores
    else:
        score_rows = []
        score_src = "none"

    # 5. write ----------------------------------------------------------------
    prov = {
        'bridge_csv': args.bridge,
        'metadata_db': "%s (read-only)" % args.metadata,
        'leo_snapshot': (args.leo or '') + (' (worked on /tmp copy)' if args.leo else ''),
        'label_path': label_path,
        'apple_score': score_src,
        'photos_library': args.library or '',
        'label_categories': ','.join("%d=%s" % (k, v) for k, v in C.WANTED_CATS.items()),
        'built_by': 'enrichment/build.py (reproducible)',
    }
    C.write_db(args.out, matches, label_rows, person_rows, score_rows, prov)
    _summary(args.out)


def main():
    ap = argparse.ArgumentParser(description="Build apple-enrichment.db (reproducible).")
    ap.add_argument("--bundle", help="Mac helper bundle (dir or .tgz): records.jsonl + "
                                     "leo.sqlite + manifest. Filesize-tightened, uuid-keyed.")
    ap.add_argument("--bridge", help="osxphotos CSV export (uuid↔file bridge + persons)")
    ap.add_argument("--metadata", default=C.default_metadata_db(),
                    help="metadata.db (read-only). Default: $DATA_ROOT/metadata.db or sibling.")
    ap.add_argument("--leo", help="leo.sqlite/psi.sqlite copy (labels, leo-decode fallback)")
    ap.add_argument("--scores", help="scores.csv (uuid,score_overall) Apple aesthetic")
    ap.add_argument("--library", help="Apple Photos .photoslibrary (enables osxphotos API)")
    ap.add_argument("--out", default=C.default_out_db(), help="output db (must not exist)")
    ap.add_argument("--force", action="store_true", help="remove --out first if it exists")
    args = ap.parse_args()

    if not args.bundle and not args.bridge:
        ap.error("provide --bundle (Mac helper bundle) or --bridge (osxphotos CSV)")
    if args.force and os.path.exists(args.out):
        os.remove(args.out)

    if args.bundle:
        build_from_bundle(args)
    else:
        build_from_csv(args)


if __name__ == "__main__":
    main()
