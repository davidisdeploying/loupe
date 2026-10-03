#!/usr/bin/env python3
"""
build_edits_db.py — (re)build edits.db, loupe's app-owned edit-relationship sidecar.

edits.db models original<->edit VARIANT GROUPS (a relationship between TWO library
assets), mirroring the pairs.db / renders.db idiom: an app-owned rw store that the
server rebuilds at import time into in-memory maps and decorates onto lean()/item().
It is DISTINCT from renders.db (a display attribute of ONE asset) — see the design
note sessions/2026-07-03-edit-aware-design.md.

Layer 1 / DECISION A: backfill the 8 Fable-visual-audit pairs (the audit 7 + the
flagship pair 25536<->104718). HEIC = original, JPEG = edit; confidence/role_confidence
1.0; method 'fable-visual'; source 'fable-audit-2026-07-03'. 25536's edit_type is NULL
(no verdict note was recorded for it — we do not launder a guess into seed data); its
group carries an explanatory notes line.

Deterministic + idempotent: DROPs and rebuilds the two tables, uses a frozen created_at,
so repeated runs are bit-identical. metadata.db is NOT touched (this script only writes
the app-owned sidecar); the pair ids were verified against metadata.db (mode=ro) —
filepath + shared capture_timestamp — before this table was authored. ZERO NAS/CIFS I/O.
"""

import calendar
import os
import sqlite3

APP_DATA = os.environ.get("DATA_ROOT") or os.path.dirname(os.path.abspath(__file__))
EDITS_DB = os.path.join(APP_DATA, "edits.db")

# Frozen build stamp (2026-07-03 00:00:00 UTC) so a rebuild is bit-identical.
CREATED_AT = calendar.timegm((2026, 7, 3, 0, 0, 0, 0, 0, 0))
SOURCE = "fable-audit-2026-07-03"
METHOD = "fable-visual"

# (jpeg_edit_id, edit_type, heic_original_id) — all verified vs metadata.db ro
# (identical capture_timestamp per pair). Two HEICs carry a -nas1 placement suffix
# (104719, 104723); the ids below are the resolved asset ids, not reconstructed stems.
PAIRS = [
    (25875, "bw", 104719),
    (28826, "bw", 104723),
    (30306, "filter", 104727),
    (32367, "filter", 104740),
    (33973, "filter", 104747),
    (34286, "filter", 104755),
    (36607, "filter", 104792),
    # Flagship pair from the Fable pass: edit verdict certain, edit_type NOT recorded.
    (25536, None, 104718),
]
FLAGSHIP_JPEG = 25536
FLAGSHIP_NOTES = ("flagship pair from the Fable pass; edit verdict certain, "
                  "type not recorded")


def main():
    db = sqlite3.connect(EDITS_DB)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("DROP TABLE IF EXISTS variant_members")
    db.execute("DROP TABLE IF EXISTS variant_groups")
    db.execute("""CREATE TABLE variant_groups(
        group_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        source     TEXT NOT NULL,
        notes      TEXT
    )""")
    db.execute("""CREATE TABLE variant_members(
        asset_id        INTEGER PRIMARY KEY,
        group_id        INTEGER NOT NULL,
        role            TEXT NOT NULL CHECK(role IN ('original','edit','unknown')),
        edit_type       TEXT,
        confidence      REAL,
        role_confidence REAL,
        method          TEXT NOT NULL,
        created_at      INTEGER NOT NULL
    )""")
    db.execute("CREATE INDEX idx_members_group ON variant_members(group_id)")

    for jpeg_id, edit_type, heic_id in PAIRS:
        notes = FLAGSHIP_NOTES if jpeg_id == FLAGSHIP_JPEG else None
        cur = db.execute(
            "INSERT INTO variant_groups(created_at, source, notes) VALUES(?,?,?)",
            (CREATED_AT, SOURCE, notes))
        gid = cur.lastrowid
        # HEIC = original (no edit_type), JPEG = edit (carries the edit_type)
        db.execute(
            "INSERT INTO variant_members VALUES(?,?,?,?,?,?,?,?)",
            (heic_id, gid, "original", None, 1.0, 1.0, METHOD, CREATED_AT))
        db.execute(
            "INSERT INTO variant_members VALUES(?,?,?,?,?,?,?,?)",
            (jpeg_id, gid, "edit", edit_type, 1.0, 1.0, METHOD, CREATED_AT))

    db.commit()
    g = db.execute("SELECT COUNT(*) FROM variant_groups").fetchone()[0]
    m = db.execute("SELECT COUNT(*) FROM variant_members").fetchone()[0]
    db.close()
    print(f"edits.db: {g} groups / {m} members written to {EDITS_DB}")


if __name__ == "__main__":
    main()
