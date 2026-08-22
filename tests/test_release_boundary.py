"""Release-boundary invariants (P7).

Loupe is meant to be installable by someone who is not the owner. That needs three
things kept apart: the product, the owner's data, and derived state a fresh install
rebuilds. See RELEASE-BOUNDARY.md.

The tests that matter here are the ones about *not shipping* the owner's data, and the
one about `faces.db` being mixed — it holds ML output and human decisions in the same
file, so "rebuild the face pipeline" must never become "delete faces.db and re-run".
"""
import os
import subprocess
import sqlite3
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Files that must never be tracked in git, with what they would leak.
OWNER_DATA = {
    "secrets.env": "credentials",
    "loupe-settings.json": "residences, protected-people names, and the W23 write token",
}


def git(*args):
    return subprocess.run(["git"] + list(args), cwd=REPO,
                          capture_output=True, text=True)


def is_git_repo():
    return git("rev-parse", "--git-dir").returncode == 0


@unittest.skipUnless(is_git_repo(), "not a git repo")
class WhatShips(unittest.TestCase):
    def test_owner_data_is_not_tracked(self):
        for name, what in sorted(OWNER_DATA.items()):
            with self.subTest(file=name):
                r = git("ls-files", "--error-unmatch", name)
                self.assertNotEqual(r.returncode, 0,
                                    "%s is tracked in git — it contains %s" % (name, what))

    def test_owner_data_is_ignored_not_merely_absent(self):
        """Untracked is not enough: an unignored file gets added by the next `git add`."""
        for name in sorted(OWNER_DATA):
            with self.subTest(file=name):
                r = git("check-ignore", name)
                self.assertEqual(r.returncode, 0, "%s is not gitignored" % name)

    def test_no_databases_or_media_are_tracked(self):
        r = git("ls-files")
        self.assertEqual(r.returncode, 0)
        bad = [f for f in r.stdout.split()
               if f.endswith((".db", ".sqlite", ".sqlite3", ".heic", ".mov", ".mp4",
                              ".jpg", ".jpeg"))]
        self.assertEqual(bad, [], "databases or media are tracked: %s" % bad[:10])

    def test_protected_names_ships_empty(self):
        """Real names belong only in the gitignored settings file."""
        with open(os.path.join(REPO, "server.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("PROTECTED_NAMES = []", src,
                      "PROTECTED_NAMES no longer ships empty — a real name may be baked in")

    def test_release_boundary_is_documented(self):
        self.assertTrue(os.path.exists(os.path.join(REPO, "RELEASE-BOUNDARY.md")))


FACES_DB = None
for _c in ("/data/loupe/state/faces.db", os.path.join(REPO, "faces.db")):
    if os.path.exists(_c) and os.path.getsize(_c) > 1_000_000:
        FACES_DB = _c
        break


@unittest.skipUnless(FACES_DB, "faces.db not present")
class FacesDbIsMixed(unittest.TestCase):
    """The single most dangerous thing in the boundary: one file, two categories."""

    @classmethod
    def setUpClass(cls):
        con = sqlite3.connect("file:%s?mode=ro" % FACES_DB, uri=True)
        cls.tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cls.counts = {}
        for t in ("persons", "assignments", "rejections", "faces", "processed"):
            if t in cls.tables:
                cls.counts[t] = con.execute('SELECT COUNT(*) FROM "%s"' % t).fetchone()[0]
        con.close()

    def test_human_decision_tables_still_exist(self):
        for t in ("persons", "assignments", "rejections"):
            self.assertIn(t, self.tables,
                          "faces.db lost %s — that is a human-decision table, not ML output" % t)

    def test_the_person_graph_is_not_empty(self):
        """A rebuild that dropped faces.db would leave these at zero while everything
        else looked healthy."""
        self.assertGreater(self.counts.get("persons", 0), 0,
                           "no named persons — was faces.db rebuilt from scratch?")
        self.assertGreater(self.counts.get("assignments", 0), 0,
                           "no face->person assignments — the person graph is gone")

    def test_derived_and_human_really_do_coexist(self):
        """If this ever fails because the file was split, update RELEASE-BOUNDARY.md --
        the hazard it documents would be gone, which is good news worth recording."""
        self.assertGreater(self.counts.get("faces", 0), 0)
        self.assertGreater(self.counts.get("assignments", 0), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
