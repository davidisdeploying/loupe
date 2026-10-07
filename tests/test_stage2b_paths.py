"""Path resolution for the Stage-2b video-face importer.

`stage2b_import.py` appends to the live `faces.db`. Until 2026-08-09 it resolved that
database as `~/loupe/faces.db` — the pre-restructure location. `sqlite3.connect()` creates
a missing file, so a run would have opened a brand-new **empty** database rather than the
live one. It failed safe only by luck: the first statement is a `SELECT` on `processed`,
which does not exist in a fresh file. A different statement order would have appended
26,972 assets' worth of faces into a stray file and reported success.

Same class as the `vec0` and `ftfy` breakages: a path the host move invalidated, invisible
until something actually exercised it.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC_PATH = os.path.join(REPO, "stage2b_import.py")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@unittest.skipUnless(os.path.exists(SRC_PATH), "stage2b_import.py not present")
class ImporterPaths(unittest.TestCase):
    def setUp(self):
        self.src = read(SRC_PATH)

    def test_faces_db_is_not_hardcoded_to_the_old_repo_path(self):
        self.assertNotIn('FACES_DB = os.path.expanduser("~/loupe/faces.db")', self.src,
                         "faces.db resolved to the pre-restructure path; a run would "
                         "create and append to an empty database")

    def test_faces_db_resolves_through_the_shared_root(self):
        self.assertRegex(self.src, r"FACES_DB\s*=.*APP_DATA",
                         "faces.db should resolve through loupe_common like every other store")

    def test_export_dir_is_overridable(self):
        """The producer and importer disagree on where the bundle lives; the path has to
        be a parameter so the bundle can be validated where it actually is."""
        self.assertIn("LOUPE_VIDEO_EXPORT_DIR", self.src)

    def test_resolved_faces_db_is_the_live_one_when_data_root_is_set(self):
        if not os.path.isdir("/data/loupe/state"):
            self.skipTest("state root not present on this host")
        sys.path.insert(0, REPO)
        os.environ.setdefault("DATA_ROOT", "/data/loupe/state")
        import importlib
        import loupe_common
        importlib.reload(loupe_common)
        resolved = os.path.join(loupe_common.APP_DATA, "faces.db")
        self.assertTrue(os.path.exists(resolved),
                        "resolved faces.db does not exist: %s" % resolved)
        self.assertGreater(os.path.getsize(resolved), 1_000_000,
                           "resolved faces.db is suspiciously small -- a fresh empty file?")

    def test_dry_run_is_still_advertised(self):
        """The only reason this importer is safe to point at anything is --dry-run."""
        self.assertIn("--dry-run", self.src)
        self.assertIn("dry_run", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
