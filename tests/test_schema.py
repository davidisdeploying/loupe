"""Schema drift detection for every Loupe database (P5 schema versioning).

Done read-only and out-of-band. Writing a `schema_version` row into each store would
mean migrating eleven live databases that hold irreproducible human judgment; a
fingerprint recorded beside them gives the same drift detection with no write.

An unexpected failure here means a schema changed without anyone deciding to change it —
a pipeline migration that ran silently, or a restore from an older snapshot. A *deliberate*
change is blessed by re-running `tests/capture_schema.py` and saying why in the commit.

Skips cleanly when the databases are not present, so the rest of the suite still runs on
a machine that is not Charlie.
"""
import json
import os
import sqlite3
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE = os.path.join(HERE, "schema_baseline.json")

import sys
sys.path.insert(0, HERE)
from capture_schema import DATABASES, REPO, fingerprint  # noqa: E402


def _load():
    try:
        return json.load(open(BASELINE))
    except Exception:
        return {}


BASE = _load()
PRESENT = {k: v for k, v in DATABASES.items() if os.path.exists(v)}


@unittest.skipUnless(BASE, "no schema baseline")
@unittest.skipUnless(PRESENT, "no Loupe databases on this host")
class SchemaDrift(unittest.TestCase):
    def test_every_baselined_database_still_exists(self):
        missing = [k for k in BASE if k not in PRESENT]
        # Databases inside the repo are build products, not repository
        # content: a fresh checkout has not built them yet.
        missing = [k for k in missing
                   if not os.path.abspath(DATABASES.get(k, "")).startswith(REPO)]
        self.assertEqual(missing, [], "baselined databases have disappeared: %s" % missing)

    def test_schema_fingerprints_match(self):
        for label in sorted(set(BASE) & set(PRESENT)):
            with self.subTest(database=label):
                got = fingerprint(PRESENT[label])
                if got["sha256"] == BASE[label]["sha256"]:
                    continue
                # Produce a readable diff rather than "two hashes differ".
                exp_objs, got_objs = BASE[label]["objects"], got["objects"]
                lines = []
                for typ in sorted(set(exp_objs) | set(got_objs)):
                    e, g = exp_objs.get(typ, {}), got_objs.get(typ, {})
                    for name in sorted(set(e) | set(g)):
                        if name not in g:
                            lines.append("  - %s %s REMOVED" % (typ, name))
                        elif name not in e:
                            lines.append("  + %s %s ADDED" % (typ, name))
                        elif e[name] != g[name]:
                            lines.append("  ~ %s %s CHANGED\n      was: %s\n      now: %s"
                                         % (typ, name, e[name], g[name]))
                self.fail("%s schema drifted:\n%s\n\nIf deliberate, re-run "
                          "tests/capture_schema.py and explain in the commit."
                          % (label, "\n".join(lines) or "  (sql text differs)"))


@unittest.skipUnless("metadata.db" in PRESENT, "metadata.db not present")
class CoreSchemaGuarantees(unittest.TestCase):
    """Specific structures other work depends on, asserted by name so a failure says
    what broke rather than just 'the hash moved'."""

    @classmethod
    def setUpClass(cls):
        con = sqlite3.connect("file:%s?mode=ro" % PRESENT["metadata.db"], uri=True)
        cls.names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
        cls.cols = {r[1] for r in con.execute("PRAGMA table_info(assets)")}
        con.close()

    def test_idx_year_month_survives(self):
        """P1.5's composite index — dropping it silently reintroduces a TEMP B-TREE on
        the overview GROUP BY that the whole landing page depends on."""
        self.assertIn("idx_year_month", self.names)

    def test_assets_columns_the_app_reads(self):
        for col in ("id", "filepath", "extension", "year", "month"):
            self.assertIn(col, self.cols,
                          "assets.%s is gone — server.py reads it directly" % col)


if __name__ == "__main__":
    unittest.main(verbosity=2)
