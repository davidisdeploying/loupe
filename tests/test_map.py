"""D13 / audit 9.2 -- the map is an atlas of photographs, not a heat map.

9.2: "at country/state zoom, clusters render as frame-stacks (2-3 offset rectangles +
tabular count), never dots -- photographs remain the unit even at 30,000 ft", and
"Heat ring: cluster border thickness encodes count decile (1-4px) in neutral; no
heat-map color washes (chroma rule)."

One marker broke all three: it was a circle, it carried an amber radial-gradient with
an 18px glow -- which is the colour wash the chroma rule forbids -- and it encoded
count only through diameter, so there was no ring to read.

Measured after: 23 clusters on screen, all four ring widths (1/2/3/4px) present, 0
markers with a gradient or glow, outer border-radius 0px, counts tabular, 15 of 23
deep enough for a third frame.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "app.css")
JS = os.path.join(ROOT, "static", "app.js")
HTML = os.path.join(ROOT, "app.html")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def mcl_rule():
    m = re.search(r"\n\.mcl\{([^}]*)\}", read(CSS))
    assert m, "the .mcl rule is gone"
    return m.group(1)


class FrameStacks(unittest.TestCase):
    def test_clusters_are_not_dots(self):
        self.assertNotIn("border-radius:50%", mcl_rule(),
                         "map clusters are circles again; 9.2 says never dots")

    def test_no_heat_map_colour_wash(self):
        # The chroma rule. The old marker was an amber radial-gradient with a glow.
        body = mcl_rule()
        self.assertNotIn("radial-gradient", body,
                         "the cluster marker carries a colour wash again")
        self.assertNotIn("box-shadow:0 0 18px", body,
                         "the cluster glow is back; that is a heat map by another name")

    def test_stack_has_offset_frames(self):
        css = read(CSS)
        self.assertIn(".mcl .f2", css, "the second frame of the stack is gone")
        self.assertIn(".mcl .f3", css, "the third frame of the stack is gone")

    def test_third_frame_only_for_deep_clusters(self):
        # "2-3 offset rectangles" -- a third frame on a 4-frame cluster would lie about
        # its depth.
        js = read(JS)
        self.assertIn("const deep=n>=25", js,
                      "every cluster gets the same number of frames; the stack no "
                      "longer reads as depth")

    def test_ring_encodes_a_decile_in_one_to_four_px(self):
        js = read(JS)
        self.assertIn("const ring=1+Math.round(decile(n)/9*3)", js,
                      "the heat ring no longer encodes the count decile in 1-4px")

    def test_decile_is_computed_across_visible_clusters(self):
        # Recomputed per draw, so the ring compares what is on screen rather than
        # against a library-wide constant that would flatten every ring when zoomed
        # into a single city.
        js = read(JS)
        self.assertRegex(js, r"const decile=n=>\{",
                         "the decile helper is gone; ring thickness has nothing to encode")

    def test_count_is_tabular(self):
        css = read(CSS)
        m = re.search(r"(?m)^\.mcl b\{([^}]*)\}", css)   # anchored: .mcl i,.mcl b{...} also contains ".mcl b{"
        self.assertIsNotNone(m, "the front-frame count rule is gone")
        self.assertIn("tabular-nums", m.group(1))


class Legend(unittest.TestCase):
    """A legend that does not match its map is worse than no legend."""

    def test_legend_shows_a_stack_not_a_dot(self):
        html = read(HTML)
        self.assertIn("lgstack", html,
                      "the map legend shows a disc again while the markers are stacks")

    def test_legend_explains_the_ring(self):
        # The ring is the one encoding a reader cannot guess from the map itself.
        html = read(HTML)
        self.assertIn("lgring", html, "the legend no longer explains the heat ring")
        self.assertIn("decile", html,
                      "the legend does not say what ring thickness means")


class BottomDeck(unittest.TestCase):
    """9.2: "Tap cluster -> the cluster BECOMES a contact-sheet strip pinned to the
    bottom deck, map dims; Esc returns. Map never navigates away."

    What existed was a 286px card at bottom-left with six sampled thumbs in a 3x2
    grid: a popup about the cluster rather than the cluster laid out to be looked at.
    The map behind it stayed at full brightness, so nothing said a lens was open, and
    Escape did not reach it -- it closed the whole map instead, one level too far.

    Measured: deck pinned to the bottom edge and spanning the map (186->1400, the rail
    is nav and not map), 60 frames in a scrollable flex row, map at brightness(0.62);
    after Esc the deck is closed, the map is still open, and the filter is back to none.
    """

    def test_deck_is_pinned_across_the_bottom(self):
        m = re.search(r"(?m)^\.mapcard\{left:0;right:0;bottom:0", read(CSS))
        self.assertIsNotNone(m,
            "the map card floats again instead of being pinned across the bottom deck")

    def test_strip_is_a_row_not_a_grid(self):
        # Two .mcthumbs rules exist: the original card grid and the deck override.
        # The LAST one is what the browser applies, so that is the one to assert on.
        rules = re.findall(r"(?m)^\.mcthumbs\{([^}]*)\}", read(CSS))
        self.assertTrue(rules, "the deck strip rule is gone")
        body = rules[-1]
        self.assertIn("display:flex", body,
                      "the deck shows a grid again; 9.2 asks for a contact-sheet strip")
        self.assertIn("overflow-x:auto", body, "the strip no longer scrolls")

    def test_the_map_dims_and_the_deck_does_not(self):
        # The filter is on the map element, not a shared ancestor, so the deck above it
        # keeps full contrast.
        css = read(CSS)
        self.assertIn("body:has(#mapcard.on) #lmap{filter:brightness(", css,
                      "the map no longer dims when the lens is open")

    def test_escape_returns_from_the_lens_before_closing_the_map(self):
        # The deck is not an OVL entry, so without an explicit branch the topmost-overlay
        # scan finds #mapview and Escape closes the whole map.
        js = read(JS)
        i_deck = js.find("if(deck&&deck.classList.contains('on')){e.preventDefault();closeMapCard();return;}")
        i_scan = js.find("let top=null,topZ=-1;")
        self.assertNotEqual(i_deck, -1,
                            "Escape no longer returns from the map lens; it will close "
                            "the entire map instead")
        self.assertLess(i_deck, i_scan,
                        "the deck branch must run before the topmost-overlay scan, or "
                        "the scan closes the map first")

    def test_meta_has_a_wrapper(self):
        # As five loose children the eyebrow/title/sub wrapped onto a second grid row,
        # squeezing the strip into a 240px column and stretching the button.
        self.assertIn("mcmeta", read(HTML),
                      "the deck meta wrapper is gone; the grid will wrap and squeeze "
                      "the strip")

    def test_strip_is_capped(self):
        # A 20,000-frame cluster must not build 20,000 <img>.
        self.assertIn("sampleEven(ids,60)", read(JS),
                      "the deck strip is uncapped; a large cluster will build one image "
                      "per frame")


if __name__ == "__main__":
    unittest.main()
