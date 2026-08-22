"""The published keyboard map must be true (audit §8.5).

The `?` overlay documents the shortcuts. A documented shortcut that does nothing is worse
than an undocumented one: the user learns it, it fails silently, and they stop trusting
the rest of the list. So the test that matters here is not "does the overlay exist" but
"is everything it claims actually bound".

The audit's canonical map also specifies `G`-chord go-to, which is unbuilt and
deliberately unlisted. `[` `]` density, `Space` zoom-to-pixel, `I` and the `/` palette
were all unbuilt until 2026-08-09 and are now real.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def read(rel):
    p = os.path.join(REPO, rel)
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as fh:
        return fh.read()


HTML = read("app.html")
JS = read("static/app.js")

# What each advertised key must map to in the source.
BINDINGS = {
    "?": r"e\.key==='\?'",
    "/": r"e\.key==='/'",
    "Esc": r"e\.key==='Escape'",
    "K": r"k==='k'",
    "X": r"k==='x'",
    "U": r"k==='u'",
    "V": r"k==='v'",
    # S was advertised in the help text as "skip" with no k==='s' branch anywhere: a
    # documented key that did nothing. It is bound to 9.5's more-like-this now, and
    # listing it here is what keeps that true.
    "S": r"k==='s'",
    "←": r"k==='arrowleft'",
    "→": r"k==='arrowright'",
    "Space": r"k===' '",
    "I": r"k==='i'",
    "[": r"e\.key==='\['",
    "]": r"e\.key===']'",
}


def advertised_keys():
    m = re.search(r"<div id=keysview>(.*?)\n</div>", HTML, re.S)
    if not m:
        return []
    raw = re.findall(r"<kbd>([^<]+)</kbd>", m.group(1))
    return [k.replace("&larr;", "←").replace("&rarr;", "→").strip() for k in raw]


@unittest.skipUnless(HTML and JS, "static assets not found")
class PublishedKeymap(unittest.TestCase):
    def test_the_overlay_exists(self):
        self.assertIn("id=keysview", HTML)

    def test_question_mark_toggles_it(self):
        self.assertRegex(JS, r"e\.key==='\?'", "'?' no longer opens the keyboard map")
        self.assertIn("showKeys()", JS)
        self.assertIn("closeKeys()", JS)

    def test_question_mark_does_not_fire_while_typing(self):
        """A question mark typed into the search box must reach the box."""
        m = re.search(r"if\(e\.key==='\?'\)\{.{0,400}", JS, re.S)
        self.assertIsNotNone(m)
        for guard in ("INPUT", "TEXTAREA", "isContentEditable"):
            self.assertIn(guard, m.group(0),
                          "'?' handler does not exempt %s" % guard)

    def test_every_advertised_key_is_actually_bound(self):
        """The whole point of the file."""
        keys = advertised_keys()
        self.assertTrue(keys, "no <kbd> entries found in the overlay")
        unbound = []
        for k in keys:
            pattern = BINDINGS.get(k)
            if pattern is None:
                unbound.append("%s (no known binding pattern)" % k)
            elif not re.search(pattern, JS):
                unbound.append(k)
        self.assertEqual(
            unbound, [],
            "the keyboard map advertises shortcuts that are not bound: %s" % unbound)

    def test_it_does_not_advertise_unbuilt_shortcuts(self):
        """Palette, density, zoom and go-to chords are specified but unbuilt. Listing
        them would be documentation of an intention, not of the software."""
        keys = set(advertised_keys())
        # [ and ] moved from unbuilt to built on 2026-08-09 (D3 density), so they are
        # advertised now; the binding check above is what keeps that honest.
        # Space (zoom to pixel) became real on 2026-08-09 with D4.
        # / (command palette) became real on 2026-08-09 with D5.
        for unbuilt in ("G",):
            self.assertNotIn(unbuilt, keys,
                             "%r is advertised but not implemented" % unbuilt)

    def test_it_is_registered_as_an_overlay(self):
        """So Escape and navigation dismiss it through the single chokepoint rather than
        a bespoke lifecycle."""
        m = re.search(r"const OVL\s*=\s*\[(.*?)\]", JS, re.S)
        self.assertIsNotNone(m)
        self.assertIn("keysview", m.group(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
