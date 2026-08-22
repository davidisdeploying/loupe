"""D12 / audit 9.1 -- the Trips mosaic.

9.1 argues that trips are event memory and "the design leans mosaic ... masonry ...
where mixed heights are the point". 8.4 states the app-wide rule: "contact sheets
never crop; neither should Loupe."

Both were contradicted by one declaration. Every hero was pinned to aspect-ratio:4/3
with object-fit:cover, so each postcard was exactly as tall as every other and each
hero was cropped into that box -- a grid of identical rectangles, which is the
calendar treatment this section exists to argue against.

Measured in-browser after the change: 66 cards, heights ranging 385-591px, 18 heroes
decoded and 0 cropped (worst aspect error 0.0%), 66 peek strips, strip 0px at rest
and 54px hovered, sitting directly under the hero and clear of the .rev pill.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "app.css")
JS = os.path.join(ROOT, "static", "app.js")


def css():
    with open(CSS, encoding="utf-8") as fh:
        return fh.read()


def js():
    with open(JS, encoding="utf-8") as fh:
        return fh.read()


class Mosaic(unittest.TestCase):
    def test_hero_is_not_pinned_to_a_fixed_aspect(self):
        m = re.search(r"\.pc \.hero\{([^}]*)\}", css())
        self.assertIsNotNone(m, "the .pc .hero rule is gone")
        body = m.group(1)
        self.assertIn("var(--ar", body,
                      "the trip hero is back to a hardcoded aspect ratio; it will crop "
                      "every hero and flatten the mosaic into a uniform grid")

    def test_cards_may_take_their_natural_height(self):
        # Without align-items:start every card in a row stretches to the tallest one and
        # the ragged edge -- the entire point of a mosaic -- disappears again.
        m = re.search(r"#plgallery\{align-items:start\}", css())
        self.assertIsNotNone(m, "#plgallery no longer lets cards size to their content")

    def test_masonry_is_behind_supports_both_spellings(self):
        # The property was renamed mid-flight; a browser may ship either.
        src = css()
        self.assertIn("@supports (grid-template-rows:masonry)", src)
        self.assertIn("@supports (item-flow:row masonry)", src)

    def test_fallback_grid_survives(self):
        # 9.1 asks for a "justified-row fallback" where masonry is unsupported.
        self.assertIn("grid-template-columns:repeat(auto-fill,minmax(300px,1fr))", css(),
                      "the fallback grid is gone; unsupported browsers get no layout")

    def test_counts_are_tabular(self):
        m = re.search(r"\.pc \.foot,\.pc \.rev\{([^}]*)\}", css())
        self.assertIsNotNone(m, "the tabular-nums rule for trip counts is gone")
        self.assertIn("tabular-nums", m.group(1))


class PeekStrip(unittest.TestCase):
    def test_peek_strip_is_built(self):
        self.assertIn("function peekStrip(", js(), "9.1's 3-frame peek strip is gone")

    def test_peek_strip_excludes_the_hero(self):
        m = re.search(r"function peekStrip\(t\)\{(.*?)\n\}", js(), re.S)
        self.assertIsNotNone(m, "peekStrip changed shape")
        self.assertIn("id!==t.hero", m.group(1),
                      "the peek strip can repeat the hero, so the card shows the same "
                      "frame twice")

    def test_peek_strip_sits_under_the_hero(self):
        # 9.1: "a 3-frame peek strip under the hero". It was first placed after the
        # caption, where it collided with the absolutely-positioned .rev pill.
        src = js()
        self.assertIn("${pm}${hero}${peekStrip(t)}", src,
                      "the peek strip is no longer directly under the hero")

    def test_peek_strip_is_hover_revealed_on_the_table(self):
        self.assertIn(".pc:hover .peekstrip", css(),
                      "the peek strip no longer reveals on hover")

    def test_peek_strip_is_decorative_to_assistive_tech(self):
        # The frames carry no information the card does not already state, and the card
        # is itself one button.
        m = re.search(r"<div class=peekstrip ([^>]*)>", js())
        self.assertIsNotNone(m, "the peek strip markup changed")
        self.assertIn("aria-hidden", m.group(1))


class SideSheet(unittest.TestCase):
    """9.1: "Tripsheet overlay becomes an anchor-positioned side sheet rather than a
    modal -- the table stays visible behind it at 40% dim; you never lose the room."

    It was a modal in every respect that matters: fixed to all four edges with an
    opaque background. Measured after: sheet at x=640 w=760 in a 1400px window, 640px
    of table still on screen, filter brightness(0.6), 50 cells with 0px overflow; and
    opened at 390px it fills the width (15 cells).
    """

    def test_sheet_does_not_span_the_window(self):
        m = re.search(r"#tripsheet\{([^}]*)\}", css())
        self.assertIsNotNone(m, "the #tripsheet rule is gone")
        body = m.group(1)
        self.assertIn("width:min(", body,
                      "the tripsheet spans the window again; it is a modal, and opening "
                      "a trip loses the room")
        self.assertIn("left:auto", body)

    def test_sheet_keeps_its_own_edge_in_rail_mode(self):
        # The rail block offsets every fixed full-width surface by --rail-w and beat
        # left:auto by source order, pinning the sheet against the rail so the dimmed
        # table ended up to its right, half-cropped at the window edge.
        src = css()
        self.assertIn("@media (min-width:1180px){ #tripsheet{left:auto} }", src,
                      "the rail exception is gone; the side sheet will be pinned to the "
                      "rail instead of the window edge")
        rail = src.index("@media (min-width:1180px){\n  :root{--rail-w:186px}")
        exception = src.index("@media (min-width:1180px){ #tripsheet{left:auto} }")
        self.assertGreater(exception, rail,
                           "the rail exception must come after the rail block to win")

    def test_narrow_screens_get_the_full_width_back(self):
        # A 360px phone cannot show a side sheet and a table at once; pretending
        # otherwise leaves both unusable.
        self.assertIn("@media (max-width:700px){#tripsheet{left:0;width:auto", css(),
                      "the sheet no longer falls back to full width on a phone")

    def test_dim_uses_brightness_not_opacity(self):
        # #placesview has an opaque background. opacity makes the whole layer
        # translucent -- background included -- so the overview underneath bled through
        # and the table rendered as a double exposure with the year carousel.
        m = re.search(r"body:has\(#tripsheet\.on\) #placesview\{([^}]*)\}", css())
        self.assertIsNotNone(m, "the dim rule is gone")
        body = m.group(1)
        self.assertIn("brightness", body,
                      "the dim is back to opacity; the overview will bleed through the "
                      "trips table")
        self.assertNotIn("opacity:", body)

    def test_the_room_is_kept_mounted(self):
        # closeOverlays hides every sibling, #placesview included, so the CSS dim alone
        # was decorative -- it was dimming a display:none element.
        src = js()
        m = re.search(r"closeOverlays\('tripsheet'\);(.{0,600})", src, re.S)
        self.assertIsNotNone(m, "openTripSheet changed shape")
        self.assertIn("$('#placesview').classList.add('on')", m.group(1),
                      "the trips table is no longer kept behind the sheet; the room is "
                      "lost again when a trip opens")


if __name__ == "__main__":
    unittest.main()
