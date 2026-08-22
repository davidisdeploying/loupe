"""D11 / audit 9.8 -- the touch branch.

9.8 asks for two things this file can check without a browser: targets >=44px, and
"@media (hover: none) drives the swap, not UA sniffing".

The 44px floor itself is measured in a real Chrome at a 390x844 phone viewport (that
is what found the original 62 undersized controls, and what proved the desk density
was unchanged). What is guarded here is that the machinery those measurements depend
on stays in place: the branch exists, it is gated on a coarse pointer rather than a
user-agent string, it declares the floor, and it stays at the end of the file.

The source-order assertion is not pedantry. An earlier rail block placed near the top
of app.css lost to later base rules at equal specificity and slid content underneath
itself while looking half-right in a screenshot. Anything appended to this stylesheet
that must win has to stay last.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "app.css")
JS = os.path.join(ROOT, "static", "app.js")

TOUCH_RE = re.compile(r"@media\s*\(hover:\s*none\)\s*and\s*\(pointer:\s*coarse\)")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TouchBranch(unittest.TestCase):
    def test_touch_branch_exists(self):
        self.assertRegex(read(CSS), TOUCH_RE,
                         "9.8's touch branch is gone; every control reverts to desk density")

    def test_gated_on_pointer_not_user_agent(self):
        # "@media (hover: none) drives the swap, not UA sniffing."
        js = read(JS)
        for probe in ("navigator.userAgent", "navigator.platform", "maxTouchPoints"):
            self.assertNotIn(probe, js,
                             "touch affordances must be driven by media queries, not by " + probe)

    def test_declares_the_44px_floor(self):
        css = read(CSS)
        block = css[css.index(TOUCH_RE.search(css).group(0)):]
        self.assertIn("min-height:44px", block,
                      "the touch branch no longer states the 44px target floor")

    def test_branch_stays_last_in_the_file(self):
        # Source order is load-bearing here; see the module docstring.
        css = read(CSS)
        start = TOUCH_RE.search(css).start()
        tail = css[start:]
        self.assertNotIn("@media", tail[len(TOUCH_RE.search(css).group(0)):],
                         "another @media block was appended after the touch branch; "
                         "the touch branch must remain last so it is not overridden")

    def test_covers_the_surfaces_that_were_measured_short(self):
        css = read(CSS)
        block = css[TOUCH_RE.search(css).start():]
        # nav + stats strip (30px before), year arrows (40px), focus deck (32px)
        for sel in (".hbtns button", ".yarrow", "#focus button", "#focus select",
                    ".toolbar select"):
            self.assertIn(sel, block,
                          sel + " dropped out of the touch branch; it measured under "
                                "44px on a phone before this block existed")

    def test_declink_keeps_its_gap(self):
        # inline-flex turns "cut" and its count into two flex items and flex strips the
        # whitespace between them -- without an explicit gap the stats read "cut226".
        css = read(CSS)
        block = css[TOUCH_RE.search(css).start():]
        m = re.search(r"\.strip \.declink\{([^}]*)\}", block)
        self.assertIsNotNone(m, "the declink touch rule is gone")
        body = m.group(1)
        if "inline-flex" in body:
            self.assertIn("gap:", body,
                          "declink is inline-flex without a gap; the stats will render "
                          "as 'cut226' / 'kept21' on touch devices")


if __name__ == "__main__":
    unittest.main()
