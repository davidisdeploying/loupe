"""Mobile navigation -- the bottom bar.

The left rail is right on a wide screen and wrong on a phone. At 390px the same ten
buttons stacked into three labelled groups and filled the entire first screen: the
first photograph sat below the fold. A photo app whose first screen contains no
photographs has the wrong navigation.

Measured after: header 172px (was the whole viewport), first photograph at y=192
device px, bar pinned to the bottom edge with five 58px targets, and tapping Trips
moves the view and the active state with it.

The bar is CLONED from the header buttons at runtime, so the icons, labels, handlers
and owner/guest gating stay in one place. Everything here guards that arrangement.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def nav_block(css):
    """The mobile-nav media query, found by its own marker.

    More than one @media (max-width:700px) block exists -- the tripsheet full-width
    fallback is another -- so indexing the first match reads the wrong rules.
    """
    i = css.index("mobile navigation: a bottom bar")
    return css[i:]


CSS = read("static/app.css")
JS = read("static/app.js")
HTML = read("app.html")


class Placement(unittest.TestCase):
    def test_desktop_never_shows_the_bar(self):
        # The rail is the desktop answer; two navigations at once would be worse than
        # either alone.
        self.assertIn("#tabbar{display:none}", CSS,
                      "the tab bar is no longer hidden by default; it will appear "
                      "alongside the rail")

    def test_phone_swaps_the_header_nav_for_the_bar(self):
        seg = nav_block(CSS)
        self.assertIn("header#top .hbtns{display:none}", seg,
                      "the phone shows both the stacked header nav and the bar")
        self.assertIn("#tabbar{\n    display:flex", seg,
                      "the bar is not shown on phones")

    def test_bar_clears_the_home_indicator(self):
        seg = nav_block(CSS)
        self.assertIn("padding-bottom:env(safe-area-inset-bottom)", seg,
                      "the bar sits under the iPhone home indicator")

    def test_targets_meet_the_touch_floor(self):
        # Same 44px floor as D11 / 9.8.
        m = re.search(r"\.tabitem\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "the tab item rule is gone")
        self.assertIn("min-height:var(--tabbar-h)", m.group(1))
        self.assertRegex(CSS, r"--tabbar-h:(4[4-9]|[5-9]\d)px",
                         "the bar is shorter than the 44px touch floor")

    def test_fixed_surfaces_stop_above_the_bar(self):
        # Otherwise the last row of every sheet sits behind the bar and cannot be tapped.
        seg = nav_block(CSS)
        self.assertIn("#cuttingview,#settingsview,#searchview,#triageview,#keysview{bottom:var(--tabbar-h)}",
                      seg.replace("\n  ", ""),
                      "overlays run underneath the bar")

    def test_focus_is_left_immersive(self):
        # Focus is the photograph, full bleed, and a mis-tap there is a decision.
        self.assertIn("body:has(#focus.on) #tabbar{display:none}", nav_block(CSS),
                      "the bar covers the focus view, where a mis-tap cuts a frame")


class Cloning(unittest.TestCase):
    def test_bar_is_cloned_from_the_header_buttons(self):
        self.assertIn("document.querySelectorAll('.hbtns button[id]')", JS,
                      "the bar no longer derives from the rail's buttons; the two can "
                      "now disagree about what exists")

    def test_hidden_buttons_are_skipped(self):
        # Vault, Closed Set, Setup and Search carry display:none until their gate passes.
        self.assertIn("function navVisible(b){ return b && b.style.display!=='none'; }", JS,
                      "the bar will offer a guest the owner-only sections")

    def test_built_after_the_gating_runs(self):
        i_build = JS.index("buildTabbar();")
        i_gate = JS.index("if(window.SEARCH_ENABLED){const st=$('#searchtog')")
        self.assertGreater(i_build, i_gate,
                           "the bar is built before the owner/guest gating, so it will "
                           "be missing Search, Vault, Closed Set and Setup")

    def test_clone_forwards_to_the_original(self):
        self.assertIn("t.onclick=()=>{closeMore();b.click();};", JS,
                      "the clone no longer forwards its click; the bar and the rail can "
                      "now behave differently")

    def test_active_state_mirrors_the_rail(self):
        m = re.search(r"function setNav\((.*?)\n\}", JS, re.S)
        self.assertIsNotNone(m, "setNav changed shape")
        self.assertIn("syncTabbar()", m.group(1),
                      "the bar no longer follows the rail's active state")

    def test_more_holds_everything_not_in_the_bar(self):
        self.assertIn("if(TAB_PRIMARY.includes(b.id)||!navVisible(b))continue;", JS,
                      "the More sheet no longer lists exactly the sections the bar omits")

    def test_more_icon_attributes_are_quoted(self):
        # An unquoted attribute immediately before /> takes the slash into its value:
        # stroke=none/> parsed as stroke="none/", and the icon rendered as one dot.
        i = JS.index("more.innerHTML=")
        seg = JS[i:i + 500]
        self.assertNotIn("stroke=none/>", seg,
                         "the More icon has unquoted attributes again; it will render "
                         "as a single dot")
        self.assertEqual(seg.count("<circle"), 3, "the More icon is not three dots")


if __name__ == "__main__":
    unittest.main()
