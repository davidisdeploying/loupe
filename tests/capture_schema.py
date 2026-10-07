#!/usr/bin/env python3
"""Capture tests/schema_baseline.json — the schema of every Loupe database.

P5 schema versioning, done read-only. Writing a `schema_version` row into each database
would be a migration of eleven live stores holding irreproducible judgment; recording a
fingerprint outside them gets the same drift detection with no write.

Run this after a DELIBERATE schema change and say why in the commit.
"""
import hashlib
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
STATE = os.environ.get("DATA_ROOT", "/data/loupe/state")

DATABASES = {
    "metadata.db": os.path.join(STATE, "metadata.db"),
    "decisions.db": os.path.join(STATE, "decisions.db"),
    "vault.db": os.path.join(STATE, "vault.db"),
    "edits.db": os.path.join(STATE, "edits.db"),
    "renders.db": os.path.join(STATE, "renders.db"),
    "faces.db": os.path.join(STATE, "faces.db"),
    "clusters.db": os.path.join(STATE, "clusters.db"),
    "nsfw.db": os.path.join(STATE, "nsfw.db"),
    "pairs.db": os.path.join(STATE, "pairs.db"),
    "summaries.db": os.path.join(STATE, "summaries.db"),
    "apple-enrichment.db": os.path.join(STATE, "apple-enrichment.db"),
    "stage5/embeddings_siglip2.db": os.path.join(REPO, "stage5", "embeddings_siglip2.db"),
}


def fingerprint(path):
    """Normalised schema: object names and their SQL, order-independent.

    sqlite_master.sql is the authoritative definition; sorting it makes the hash stable
    against creation order, which differs between a fresh build and a restored copy."""
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        rows = con.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
    finally:
        con.close()
    objects = {}
    for typ, name, sql in rows:
        objects.setdefault(typ, {})[name] = " ".join((sql or "").split())
    blob = json.dumps(objects, sort_keys=True).encode()
    return {"objects": objects, "sha256": hashlib.sha256(blob).hexdigest()}


def main():
    out, missing = {}, []
    for label, path in sorted(DATABASES.items()):
        if not os.path.exists(path):
            missing.append(label)
            continue
        fp = fingerprint(path)
        out[label] = fp
        counts = {t: len(v) for t, v in sorted(fp["objects"].items())}
        print("  %-30s %s  %s" % (label, fp["sha256"][:16], counts))
    if missing:
        print("\n  MISSING (not captured):", missing)
    dest = os.path.join(HERE, "schema_baseline.json")
    json.dump(out, open(dest, "w"), indent=2, sort_keys=True)
    print("\nwrote %s (%d databases)" % (dest, len(out)))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
