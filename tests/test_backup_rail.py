"""Regression test for the 2026-08-06 -> 2026-08-09 ledger outage.

The unit reported SUCCESS for three days while producing no NAS snapshot, because the
most recent invocation was a manual verify run that wrote to /tmp. Exit status was
therefore worse than useless -- it was actively reassuring. These tests assert the only
thing that actually matters: a recent file exists in the destination.
"""
import os
import subprocess
import time
import unittest

LEDGER_DIR = os.environ.get("LOUPE_LEDGER_DIR", "/home/david/loupe-archive/loupe-ledger")
MAX_AGE_H = float(os.environ.get("LOUPE_LEDGER_MAX_AGE_H", "36"))
EXPECTED_DBS = 11


def snapshots():
    try:
        names = [n for n in os.listdir(LEDGER_DIR)
                 if n.startswith("ledger-") and n.endswith(".tar.zst")]
    except OSError:
        return []
    return sorted((os.path.getmtime(os.path.join(LEDGER_DIR, n)),
                   os.path.join(LEDGER_DIR, n)) for n in names)


@unittest.skipUnless(os.path.isdir(LEDGER_DIR), "%s not mounted" % LEDGER_DIR)
class BackupRail(unittest.TestCase):
    def test_a_snapshot_exists_at_all(self):
        self.assertTrue(snapshots(), "no ledger snapshot in %s" % LEDGER_DIR)

    def test_newest_snapshot_is_recent(self):
        """THE test. A daily timer that has not produced a file in %.0f hours has
        failed, whatever systemctl says.""" % MAX_AGE_H
        snaps = snapshots()
        if not snaps:
            self.fail("no ledger snapshot in %s" % LEDGER_DIR)
        mtime, path = snaps[-1]
        age_h = (time.time() - mtime) / 3600.0
        self.assertLess(
            age_h, MAX_AGE_H,
            "newest snapshot %s is %.1f h old (limit %.0f h) — the rail is broken even "
            "if loupe-ledger.service exited 0" % (os.path.basename(path), age_h, MAX_AGE_H))

    def test_newest_snapshot_is_not_suspiciously_small(self):
        """A truncated or partial archive is worse than an absent one, because it looks
        like a backup."""
        snaps = snapshots()
        if len(snaps) < 2:
            self.skipTest("need two snapshots to compare sizes")
        newest = os.path.getsize(snaps[-1][1])
        prev = os.path.getsize(snaps[-2][1])
        self.assertGreater(
            newest, prev * 0.5,
            "newest snapshot is %d bytes vs %d for the previous one" % (newest, prev))

    def test_destination_is_not_a_scratch_path(self):
        """The Aug 8 'success' wrote to /tmp. A snapshot outside the durable
        destination is not a backup."""
        self.assertFalse(LEDGER_DIR.startswith("/tmp"),
                         "ledger destination is a scratch path: %s" % LEDGER_DIR)


@unittest.skipUnless(os.path.isdir(LEDGER_DIR), "%s not mounted" % LEDGER_DIR)
@unittest.skipUnless(os.environ.get("LOUPE_TEST_SLOW"), "set LOUPE_TEST_SLOW=1 to run")
class BackupContents(unittest.TestCase):
    """Slow: reads the whole archive off the NAS. Opt-in."""

    def test_verify_passes_over_every_database(self):
        script = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools", "ledger_snapshot.sh")
        env = dict(os.environ, LOUPE_STATE_DIR=os.environ.get(
            "LOUPE_STATE_DIR", "/data/loupe/state"))
        p = subprocess.run([script, "--verify"], capture_output=True, text=True,
                           env=env, timeout=1800)
        self.assertEqual(p.returncode, 0, "ledger --verify failed:\n%s" % p.stderr[-2000:])
        oks = [l for l in p.stdout.splitlines() if l.startswith("OK ")]
        self.assertEqual(len(oks), EXPECTED_DBS,
                         "expected %d databases verified, got %d:\n%s"
                         % (EXPECTED_DBS, len(oks), p.stdout))


if __name__ == "__main__":
    unittest.main(verbosity=2)
