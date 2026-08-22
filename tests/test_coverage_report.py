"""Tests for tools/coverage_report.py.

The tool exists because coverage drift is invisible: nothing errors when a newly ingested
video misses the one-off video-face pass, so the gap grows silently. What matters is that
it FAILS when a gap is real — a coverage monitor that always reports OK is worse than
none, which is the lesson this session kept relearning.
"""
import os
import subprocess
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.path.join(REPO, "tools", "coverage_report.py")
PY = os.environ.get("LOUPE_PYTHON", "/data/loupe-venv/bin/python")
if not os.path.exists(PY):
    PY = sys.executable

VIDEO_EXT = ("MP4", "MOV")


def build_fixture(root, n_stills=10, n_videos=6, faces_missing=0, nsfw_missing=0):
    """A miniature library with controllable gaps."""
    m = sqlite3.connect(os.path.join(root, "metadata.db"))
    m.execute("CREATE TABLE assets (id INTEGER PRIMARY KEY, extension TEXT, "
              "file_sha256 TEXT, is_live_photo_video INT)")
    rows = []
    for i in range(1, n_stills + 1):
        rows.append((i, "HEIC", "sha%d" % i, 0))
    for j in range(n_stills + 1, n_stills + n_videos + 1):
        rows.append((j, "MOV", "sha%d" % j, 0))
    m.executemany("INSERT INTO assets VALUES (?,?,?,?)", rows)
    m.commit(); m.close()

    still_ids = list(range(1, n_stills + 1))
    video_ids = list(range(n_stills + 1, n_stills + n_videos + 1))

    f = sqlite3.connect(os.path.join(root, "faces.db"))
    f.execute("CREATE TABLE processed (asset_id INTEGER PRIMARY KEY)")
    covered = still_ids[:len(still_ids) - faces_missing] + video_ids
    f.executemany("INSERT INTO processed VALUES (?)", [(i,) for i in covered])
    f.commit(); f.close()

    n = sqlite3.connect(os.path.join(root, "nsfw.db"))
    n.execute("CREATE TABLE processed (asset_id INTEGER PRIMARY KEY)")
    n.executemany("INSERT INTO processed VALUES (?)",
                  [(i,) for i in still_ids[:len(still_ids) - nsfw_missing]])
    n.commit(); n.close()
    return root


def run(root, *extra):
    env = dict(os.environ, DATA_ROOT=root,
               LOUPE_VIDEO_SIGNALS_DB=os.path.join(root, "nonexistent-signals.db"))
    return subprocess.run([PY, TOOL] + list(extra), capture_output=True, text=True, env=env)


@unittest.skipUnless(os.path.exists(TOOL), "coverage_report.py not present")
class CoverageReport(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="loupe-cov-")

    def test_full_coverage_passes(self):
        build_fixture(self.root)
        r = run(self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("COVERAGE: OK", r.stdout)

    def test_a_real_gap_fails(self):
        """The whole point. 3 of 10 stills unprocessed is 30%, far over the 1% default."""
        build_fixture(self.root, faces_missing=3)
        r = run(self.root)
        self.assertEqual(r.returncode, 1, "a 30% gap must not report OK:\n" + r.stdout)
        self.assertIn("COVERAGE: FAIL", r.stdout)

    def test_threshold_is_honoured(self):
        build_fixture(self.root, nsfw_missing=1)      # 10% gap
        self.assertEqual(run(self.root, "--max-gap-pct", "50").returncode, 0)
        self.assertEqual(run(self.root, "--max-gap-pct", "5").returncode, 1)

    def test_missing_metadata_exits_2_not_0(self):
        """Same rule as the stall check: a monitor that cannot see its subject must not
        pass."""
        empty = tempfile.mkdtemp(prefix="loupe-cov-empty-")
        r = run(empty)
        self.assertEqual(r.returncode, 2)
        self.assertNotIn("COVERAGE: OK", r.stdout)

    def test_reports_every_signal(self):
        build_fixture(self.root)
        out = run(self.root).stdout
        for label in ("faces / stills", "nsfw / stills", "faces / non-live video"):
            self.assertIn(label, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
