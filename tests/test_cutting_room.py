"""D8 / audit 9.4 -- the Cutting Room floor.

9.4: "The floor: rule bins (B2-B5/A2a/A2b + future embedding-dupes C10) as labeled
trays down the left; the selected tray's frames spill onto the sheet with their
triggering rule as small-caps caption ('BURST EXTRA · 5 OF 7')."

The room was a grid of equally-prominent cards, each showing five sample thumbs, and
the only way to see a bin's contents was to leave the room for the review surface --
a directory of piles rather than a floor you work on.

Measured after: 8 trays down the left, exactly one selected, trays entirely left of
the sheet and stacked vertically, 90 frames captioned "SOFT & BLURRY" in all-small-caps,
and selecting "Burst extras" swapped the sheet's frames and its caption.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "app.css")
JS = os.path.join(ROOT, "static", "app.js")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class Floor(unittest.TestCase):
    def test_trays_run_down_the_left_beside_a_sheet(self):
        m = re.search(r"\.crfloor\{([^}]*)\}", read(CSS))
        self.assertIsNotNone(m, "the floor layout is gone")
        body = m.group(1)
        self.assertIn("grid-template-columns:minmax(210px,268px) 1fr", body,
                      "the floor is no longer trays-then-sheet")

    def test_the_card_grid_does_not_squeeze_the_floor(self):
        # .crgrid is still repeat(auto-fill,minmax(330px,1fr)); the floor is a single
        # child, so without this it was placed into ONE 330px column and the sheet
        # became a strip where the explain text wrapped one word per line.
        self.assertIn(".crgrid:has(.crfloor){display:block}", read(CSS),
                      "the floor will be placed inside the old card grid and squeezed "
                      "into a single column")

    def test_trays_are_rendered_per_rule_bin(self):
        js = read(JS)
        self.assertIn("class=\"crtray", js, "the trays are gone")
        self.assertIn("CR.cats.map", js, "the trays are no longer built from the rule bins")

    def test_exactly_one_tray_is_selected(self):
        # CRSEL falls back to the first bin, and to the first bin again if the selected
        # key disappears from the payload.
        js = read(JS)
        self.assertIn("if(!CRSEL||!CR.cats.some(c=>c.key===CRSEL))", js,
                      "the floor can render with no tray selected, or with a stale "
                      "selection that no longer exists")

    def test_caption_is_the_triggering_rule_in_small_caps(self):
        m = re.search(r"\.crcap\{([^}]*)\}", read(CSS))
        self.assertIsNotNone(m, "the frame caption rule is gone")
        self.assertIn("font-variant-caps:all-small-caps", m.group(1),
                      "9.4 asks for the triggering rule as a small-caps caption")

    def test_sheet_is_bounded(self):
        # A 24,000-frame bin must not build 24,000 <img>; the sheet is a look inside.
        js = read(JS)
        self.assertIn("const SHOWN=90", js, "the tray sheet is unbounded")
        self.assertIn("crmore", js,
                      "the sheet no longer says how much of the tray it is not showing")

    def test_a_slower_response_cannot_overwrite_a_newer_tray(self):
        # Clicking two trays quickly: the first fetch can resolve last and paint the
        # wrong bin's frames under the right bin's caption.
        js = read(JS)
        self.assertIn("if(CRSEL!==key)return;", js,
                      "the tray fetch no longer guards against an out-of-order response")

    def test_burst_position_is_not_faked(self):
        # 9.4's caption is "BURST EXTRA · 5 OF 7" -- rule, then position within the
        # burst group. That needs per-frame group membership (C2, unbuilt). A position
        # within the tray would read as the same thing and mean something else.
        js = read(JS)
        self.assertNotRegex(js, r"crcap[^`]*\$\{i\+1\} OF ",
                            "the caption fakes a burst position from the tray index")


if __name__ == "__main__":
    unittest.main()
