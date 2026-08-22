"""The proof sheet must survive leaving the LAN (audit 9.9, fleet rule).

Two invariants, both about the artifact being read somewhere else:

  JS-FREE. The fleet requires shareable HTML to remain readable with JavaScript
  disabled. A sheet that needs a script to show its own contents is not a document.

  SELF-CONTAINED. Every thumbnail is a data URI. An artifact referencing /thumb/123.jpg
  is a link to a server the recipient cannot reach, so it would render as a page of
  broken images exactly when it matters -- after it has been sent to someone.

Generated into a temp directory from a synthetic manifest, so the test never depends on
the live export or writes near it.
"""
import csv
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.path.join(REPO, "tools", "proof_sheet.py")
PY = os.environ.get("LOUPE_PYTHON", "/data/loupe-venv/bin/python")
if not os.path.exists(PY):
    PY = sys.executable


def build(rows, state=None):
    tmp = tempfile.mkdtemp(prefix="loupe-proof-")
    man = os.path.join(tmp, "manifest.csv")
    with open(man, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "filepath", "rule", "size_bytes", "pair_role"])
        w.writerows(rows)
    out = os.path.join(tmp, "sheet.html")
    env = dict(os.environ, DATA_ROOT=state or tmp)
    p = subprocess.run([PY, TOOL, man, "-o", out], capture_output=True, text=True, env=env)
    if p.returncode != 0:
        raise AssertionError("generator failed: %s" % (p.stderr or p.stdout))
    with open(out, encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(os.path.exists(TOOL), "proof_sheet.py not present")
class ProofSheet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = build([(101, "/x/a.jpg", "B4", 1234, ""),
                          (102, "/x/b.jpg", "SD", 5678, "live_motion")])

    def test_contains_no_javascript(self):
        for pattern in (r"<script", r"\son\w+\s*=", r"javascript:"):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, self.html, re.I),
                                  "proof sheet contains JavaScript (%s)" % pattern)

    def test_references_nothing_external(self):
        """No http(s), protocol-relative, or absolute-path sources."""
        for pattern in (r'src="https?:', r'src="//', r'src="/(?!/)',
                        r'href="https?:', r'<link\b'):
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, self.html, re.I),
                                  "proof sheet references something external (%s)" % pattern)

    def test_is_a_complete_document(self):
        self.assertIn("<!doctype html", self.html.lower())
        self.assertIn("</html>", self.html.lower())

    def test_carries_a_dual_stamp_and_stats(self):
        self.assertRegex(self.html, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
        self.assertIn("CDT", self.html)
        self.assertIn("frames", self.html)

    def test_every_frame_is_struck(self):
        """8.1's mark: a proof sheet of cuts shows them as cuts."""
        self.assertEqual(self.html.count("<figure>"), 2)
        self.assertGreaterEqual(self.html.count("<svg"), 2)

    def test_has_print_styles(self):
        """9.9: 'proof sheets become literal printable contact sheets for free'."""
        self.assertIn("@media print", self.html)

    def test_says_it_deletes_nothing(self):
        """The artifact travels without its author; it has to carry that itself."""
        self.assertIn("Nothing is deleted", self.html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
