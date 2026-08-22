"""Faithful-library invariants (DL-L2) against synthetic fixtures — no live data."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import loupe_common as LC          # noqa: E402  (stdlib-only module, safe to import)


def build_fixture(path):
    """A tiny metadata.db standing in for the real 102k-row library.

    Deliberately includes the two path shapes EXCLUDE_SQL used to filter out, so the
    no-silent-loss assertion below is meaningful rather than vacuous."""
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE assets (
        id INTEGER PRIMARY KEY, filepath TEXT, extension TEXT, year INT, month INT)""")
    rows = [
        (1, "/mnt/nas2/photos/2024/a.heic", "HEIC", 2024, 3),
        (2, "/mnt/nas2/photos/production/b.jpg", "JPG", 2024, 3),
        (3, "/mnt/nas2/photos/long-video-elsewhere/c.mov", "MOV", 2023, 11),
        (4, "/mnt/nas2/photos/2023/d.mp4", "MP4", 2023, 11),
        (5, "/mnt/nas2/photos/2022/e.cr3", "CR3", 2022, 1),
    ]
    con.executemany("INSERT INTO assets VALUES (?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return len(rows)


class FaithfulLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="loupe-test-")
        self.db = os.path.join(self.tmp, "metadata.db")
        self.total = build_fixture(self.db)

    def tearDown(self):
        for f in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    def test_exclude_sql_drops_no_asset(self):
        """DL-L2: never silently lose an asset.

        EXCLUDE_SQL is currently the tautology '1=1' by David's 2026-07-04 directive
        ("everything included in the loupe app library"). If anyone reinstates the old
        production/ and long-video-elsewhere/ clauses, this fails loudly and forces the
        decision to be made deliberately instead of drifting back in."""
        con = LC.ro(self.db)
        got = con.execute(
            "SELECT COUNT(*) FROM assets WHERE %s" % LC.EXCLUDE_SQL).fetchone()[0]
        con.close()
        self.assertEqual(
            got, self.total,
            "EXCLUDE_SQL now hides %d of %d assets — a silent-loss regression"
            % (self.total - got, self.total))

    def test_exclude_sql_is_composable(self):
        """~20 call sites append it with AND; it must never need parenthesising."""
        con = LC.ro(self.db)
        con.execute("SELECT id FROM assets WHERE year=2024 AND %s" % LC.EXCLUDE_SQL).fetchall()
        con.close()

    def test_ro_connection_really_is_read_only(self):
        con = LC.ro(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("INSERT INTO assets VALUES (99,'/x','JPG',2020,1)")
        con.close()

    def test_ro_returns_mapping_rows(self):
        con = LC.ro(self.db)
        row = con.execute("SELECT id, filepath FROM assets WHERE id=1").fetchone()
        con.close()
        self.assertEqual(row["filepath"], "/mnt/nas2/photos/2024/a.heic",
                         "row_factory regression: call sites index rows by column name")


class SharedConstants(unittest.TestCase):
    def test_video_ext_is_uppercase_frozenset(self):
        """Membership is tested as `ext in VIDEO_EXT` after upper-casing; a lowercase or
        mutable container silently reclassifies every video as a still."""
        self.assertIsInstance(LC.VIDEO_EXT, frozenset)
        for e in LC.VIDEO_EXT:
            self.assertEqual(e, e.upper(), "VIDEO_EXT entry %r is not upper-case" % e)

    def test_mp4_and_mov_classify_as_video(self):
        for ext in ("MP4", "MOV"):
            self.assertIn(ext, LC.VIDEO_EXT)
        self.assertNotIn("HEIC", LC.VIDEO_EXT)

    def test_roots_are_absolute(self):
        for name in ("HERE", "V2", "APP_DATA", "METADATA_DB", "PIPELINE_DIR"):
            self.assertTrue(os.path.isabs(getattr(LC, name)),
                            "%s must be absolute; relative roots break service startup" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
