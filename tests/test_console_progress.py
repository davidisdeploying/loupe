"""Darkroom console progress invariants (D10 / audit 9.6).

A progress bar that exceeds its own total is not a cosmetic glitch: it means the
numerator and the denominator disagree about what population is being counted, and the
number is therefore meaningless in both directions.

Both stages were wrong on 2026-08-09:

  faces  88,985 / 61,802 = 144%   -- the stage2b import added 26,972 video assets to
                                     faces.db, but the denominator was still images-only.
                                     The comment above it still read "faces runs on
                                     images only", which stopped being true when video
                                     faces landed.
  nsfw   62,013 / 61,802 = 100.3% -- nsfw_pipeline's own WHERE clause is "extension NOT
                                     IN (video)", 211 wider than the image whitelist the
                                     console divided by.

Each denominator now comes from the population its pipeline actually scans.
"""
import json
import os
import unittest
import urllib.error
import urllib.request

PORT = int(os.environ.get("LOUPE_TEST_PORT", "8000"))
BASE = "http://127.0.0.1:%d" % PORT


def status():
    try:
        with urllib.request.urlopen(BASE + "/api/setup/status", timeout=120) as r:
            return json.loads(r.read())
    except Exception:
        return None


DATA = status()


@unittest.skipUnless(DATA, "setup status not reachable")
class ConsoleProgress(unittest.TestCase):
    def test_stages_are_reported(self):
        self.assertTrue(DATA.get("stages"), "the console reports no stages")

    def test_no_stage_exceeds_its_own_total(self):
        """The invariant. Allows a small float margin, not a 44% one."""
        bad = []
        for st in DATA.get("stages", []):
            p = st.get("progress") or {}
            done, total = p.get("done"), p.get("total")
            if not done or not total:
                continue
            if done > total * 1.001:
                bad.append("%s %s/%s = %.1f%%" % (st.get("id"), done, total,
                                                  100.0 * done / total))
        self.assertEqual(bad, [], "stages reporting more than 100%%: %s" % bad)

    def test_running_stages_have_a_denominator(self):
        """An in-progress bar with no total is unreadable -- you cannot tell 3% from 97%.

        Completed DISCOVERY stages are exempt and legitimately have none: load_roll
        reports "102,110 originals on disk" and sort_sheets "678 trips sorted", where the
        count IS the result rather than progress toward a target. An earlier version of
        this test flagged both, which was the test being wrong rather than the console."""
        bad = []
        for st in DATA.get("stages", []):
            p = st.get("progress") or {}
            if p.get("done") and not p.get("total") and st.get("status") != "done":
                bad.append("%s (status=%s)" % (st.get("id"), st.get("status")))
        self.assertEqual(bad, [], "running stages with no denominator: %s" % bad)

    def test_ledger_room_reports_both_destinations(self):
        """P15's ledger room. It reports the newest file actually PRESENT in each
        destination, never a unit's exit code -- exit codes are what reported SUCCESS
        through three days of producing no backup at all."""
        L = DATA.get("ledger")
        self.assertIsNotNone(L, "the console no longer reports backup state")
        self.assertIn("nas", L)
        self.assertIn("offhost", L)
        self.assertIn("restore_runbook", L)

    def test_ledger_is_not_stale(self):
        """The assertion that would have fired on 2026-08-07 and 08."""
        L = DATA.get("ledger") or {}
        self.assertFalse(L.get("stale"),
                         "no backup in the last %sh: nas=%s offhost=%s"
                         % (L.get("max_age_hours"), L.get("nas"), L.get("offhost")))

    def test_ledger_ages_are_plausible(self):
        """A destination reporting an age but no name, or vice versa, means the reader
        half-worked -- which is how a reassuring-but-wrong panel gets built."""
        L = DATA.get("ledger") or {}
        for key in ("nas", "offhost"):
            x = L.get(key)
            if x is None:
                continue
            with self.subTest(dest=key):
                self.assertIn("name", x)
                self.assertIsInstance(x.get("age_hours"), (int, float))
                self.assertGreaterEqual(x["age_hours"], 0)

    def test_faces_denominator_spans_images_and_video(self):
        """Named because it is the one that was 44% wrong: the face pass covers images
        AND non-Live-Photo videos since the stage2b import."""
        for st in DATA.get("stages", []):
            if st.get("id") == "faces":
                p = st.get("progress") or {}
                if p.get("total"):
                    self.assertGreater(
                        p["total"], 70000,
                        "faces denominator looks images-only again (%s)" % p["total"])
                return


if __name__ == "__main__":
    unittest.main(verbosity=2)
