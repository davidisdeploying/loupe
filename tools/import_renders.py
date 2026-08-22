#!/usr/bin/env python3
"""Stage 2 of the edited-render augment: populate the app-owned renders.db from a
directory of edited render files and flip the DISPLAYED file to the render.

Each render file is named <uuid>.<ext> where the stem is the photo's Apple UUID.
We resolve uuid -> original asset_id through the apple-enrichment.db `asset_uuid`
bridge (asset_id PK, uuid TEXT), upsert (asset_id, render_path, uuid, render_bytes,
built_at) into the Stage-1 renders.db, then bust the <=3 id-keyed cache files so the
serve sites regenerate from display_path() (Stage 1's chokepoint).

Standalone + additive: NO server.py edits. The running server only READS renders.db
at startup (_rebuild_renders closes its connection), so this script is the writer.
A timestamped backup of renders.db is taken before the first write.

Path constants mirror server.py (derived from loupe_common, not imported, to avoid
running server.py's import-time side effects).

Usage:
  python3 tools/import_renders.py                 # scan ~/loupe/renders/, write
  python3 tools/import_renders.py --dir <path>    # scan a different dir
  python3 tools/import_renders.py --dry-run       # resolve + report, write nothing
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from loupe_common import APP_DATA, METADATA_DB, V2  # noqa: E402

# --- paths (mirror server.py) ---------------------------------------------
RENDERS_DB    = os.path.join(APP_DATA, "renders.db")
ENRICH_DB     = os.path.join(APP_DATA, "apple-enrichment.db")
THUMBS        = os.path.join(V2, "culling", "contactsheets", "thumbs")
PLAY_CACHE    = os.path.join(APP_DATA, "cache", "play")
PREVIEW_CACHE = os.path.join(APP_DATA, "cache", "preview")

DEFAULT_DIR = os.path.join(APP_DATA, "renders")
# Render files we accept (the slice is .mov; allow the common edited-render ext).
RENDER_EXTS = {".mov", ".mp4", ".jpg", ".jpeg", ".heic", ".png"}
# Image-class render exts (the rest of RENDER_EXTS are video); used to break a
# same-uuid tie by matching the render's media class to the asset's mime_type.
IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".png"}


def _ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def resolve_uuid(uuid, render_ext, enrich, meta):
    """uuid -> (asset_id, reason). reason is None on success, else a refuse/gap tag.

    - exactly 1 asset_id        -> bind it
    - multiple asset_ids        -> prefer the still (NOT is_live_photo_video);
                                   exactly 1 still -> bind; >=2 stills -> break the
                                   tie by matching the render's media class to the
                                   asset mime_type (image render -> image asset,
                                   video render -> video asset); exactly 1 match ->
                                   bind, else REFUSE; 0 stills -> REFUSE (no anchor)
    - 0 asset_ids               -> unresolved (gap)

    The class tiebreak rescues Live Photos whose `_Original.MOV` component is
    mis-flagged is_live_photo_video=0, so both the JPG still and the MOV land in
    `stills` — the edited .jpeg render binds to the image asset, not the MOV.
    """
    rows = enrich.execute(
        "SELECT asset_id FROM asset_uuid WHERE uuid = ? COLLATE NOCASE", (uuid,)
    ).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return None, "unresolved"
    if len(ids) == 1:
        return ids[0], None
    # multiple assets share this uuid (live photo still + video, or dup import)
    qm = ",".join("?" * len(ids))
    meta_rows = meta.execute(
        f"SELECT id, is_live_photo_video, mime_type FROM assets WHERE id IN ({qm})", ids
    ).fetchall()
    stills = [(r[0], r[2] or "") for r in meta_rows if not r[1]]
    if len(stills) == 1:
        return stills[0][0], None
    if len(stills) >= 2:
        want = "image/" if render_ext in IMAGE_EXTS else "video/"
        matches = [sid for sid, mime in stills if mime.startswith(want)]
        if len(matches) == 1:
            return matches[0], None
        return None, f"refused-dup-import (asset_ids={sorted(s for s, _ in stills)})"
    return None, f"refused-no-still (asset_ids={sorted(ids)})"


def bust_caches(asset_id):
    """Remove the <=3 id-keyed cache files so they regenerate from display_path."""
    busted = []
    for p in (os.path.join(THUMBS, f"{asset_id}.jpg"),
              os.path.join(PREVIEW_CACHE, f"{asset_id}.jpg"),
              os.path.join(PLAY_CACHE, f"{asset_id}.mp4")):
        if os.path.exists(p):
            os.remove(p)
            busted.append(p)
    return busted


def main():
    ap = argparse.ArgumentParser(description="Import edited renders into renders.db")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="render dir (default ~/loupe/renders/)")
    ap.add_argument("--dry-run", action="store_true", help="resolve + report, write nothing")
    args = ap.parse_args()

    render_dir = os.path.abspath(args.dir)
    if not os.path.isdir(render_dir):
        sys.exit(f"render dir not found: {render_dir}")

    files = sorted(
        f for f in os.listdir(render_dir)
        if os.path.splitext(f)[1].lower() in RENDER_EXTS
        and os.path.isfile(os.path.join(render_dir, f))
    )
    if not files:
        sys.exit(f"no render files in {render_dir}")

    print(f"scanning {render_dir} — {len(files)} render file(s)")

    enrich = _ro(ENRICH_DB)
    meta = _ro(METADATA_DB)

    bound, refused, unresolved = [], [], []
    for f in files:
        uuid = Path(f).stem.upper()           # stored uuids are upper; normalize
        abspath = os.path.join(render_dir, f)
        render_ext = os.path.splitext(f)[1].lower()
        asset_id, reason = resolve_uuid(uuid, render_ext, enrich, meta)
        if asset_id is None:
            (unresolved if reason == "unresolved" else refused).append((uuid, f, reason))
            continue
        bound.append((asset_id, uuid, abspath, os.path.getsize(abspath)))

    enrich.close()
    meta.close()

    # --- write (unless dry-run) -------------------------------------------
    busted_total = []
    if bound and not args.dry_run:
        if os.path.exists(RENDERS_DB):
            bak = f"{RENDERS_DB}.bak.{int(time.time())}"
            shutil.copy2(RENDERS_DB, bak)
            print(f"backed up renders.db -> {bak}")
        db = sqlite3.connect(RENDERS_DB)
        db.execute("""CREATE TABLE IF NOT EXISTS renders(
            asset_id INTEGER PRIMARY KEY, render_path TEXT NOT NULL,
            uuid TEXT, render_bytes INTEGER, built_at INTEGER)""")
        now = int(time.time())
        for asset_id, uuid, abspath, nbytes in bound:
            db.execute(
                """INSERT INTO renders(asset_id, render_path, uuid, render_bytes, built_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(asset_id) DO UPDATE SET
                     render_path=excluded.render_path, uuid=excluded.uuid,
                     render_bytes=excluded.render_bytes, built_at=excluded.built_at""",
                (asset_id, abspath, uuid, nbytes, now))
            busted_total += [(asset_id, p) for p in bust_caches(asset_id)]
        db.commit()
        db.close()

    # --- summary ----------------------------------------------------------
    print()
    print(f"bound {len(bound)}, refused {len(refused)}, unresolved {len(unresolved)}"
          + ("   [DRY-RUN — nothing written]" if args.dry_run else ""))
    for asset_id, uuid, abspath, nbytes in bound:
        print(f"  BOUND   asset_id={asset_id}  uuid={uuid}  bytes={nbytes}")
    for uuid, f, reason in refused:
        print(f"  REFUSED uuid={uuid}  file={f}  reason={reason}")
    for uuid, f, reason in unresolved:
        print(f"  GAP     uuid={uuid}  file={f}  (no asset for this uuid)")
    if busted_total:
        print(f"\nbusted {len(busted_total)} cache file(s):")
        for asset_id, p in busted_total:
            print(f"  id={asset_id}  {p}")


if __name__ == "__main__":
    main()
