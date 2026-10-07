"""CSS design-token invariants (P8 token foundation).

The visual-system dossier recorded three drift hazards in the token layer: variables used
only as `var()` fallbacks so the fallback always won, fallbacks that disagreed with their
definitions, and one token name defined twice with different values. The first two closed
when the UI moved out of `server.py` into `static/app.css`; the third survived as `--ink`
and `--line` and was closed on 2026-08-09.

These tests keep all three closed. Pure text analysis -- no browser, no service.

Note on the parser: a selector may legitimately appear in several blocks --
`html.loupe-glass` carries the palette in one and the glass-chrome layer in another --
so declarations are MERGED per selector. An earlier version of this file read only the
first block per selector and consequently reported the entire `--g-*` set as undefined.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CSS_PATH = os.path.join(os.path.dirname(HERE), "static", "app.css")
def _read(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


CSS = _read(CSS_PATH)

# var() usages are found with a paren-balanced scan rather than a regex: fallbacks may
# contain nested parens (rgba(...), linear-gradient(...)), and a lazy regex happily runs
# past the closing paren into the next declaration. That is not hypothetical -- it
# produced a spurious failure the first time this file was written.
def var_uses(css):
    """Yield (name, fallback_or_None) for every var() in the stylesheet."""
    out, i = [], 0
    while True:
        i = css.find("var(", i)
        if i < 0:
            return out
        j, depth = i + 4, 1
        while j < len(css) and depth:
            if css[j] == "(":
                depth += 1
            elif css[j] == ")":
                depth -= 1
            j += 1
        inner = css[i + 4:j - 1]
        comma = inner.find(",")
        if comma < 0:
            name, fb = inner.strip(), None
        else:
            name, fb = inner[:comma].strip(), inner[comma + 1:].strip()
        if name.startswith("--"):
            out.append((name, fb))
        i = j


DECL = re.compile(r"(--[\w-]+)\s*:\s*([^;}]+)")
BLOCK = re.compile(r"([^{}/]+?)\{([^{}]*)\}", re.S)


def blocks_by_selector():
    """Merged declarations per selector, across every block that selector opens."""
    out = {}
    for sel, body in BLOCK.findall(CSS):
        sel = " ".join(sel.split())
        if "--" not in body:
            continue
        out.setdefault(sel, {}).update(
            {k: v.strip() for k, v in DECL.findall(body)})
    return out


SELECTORS = blocks_by_selector()
ALL_DECLARED = {k for decls in SELECTORS.values() for k in decls}


@unittest.skipUnless(CSS, "static/app.css not found")
class DesignTokens(unittest.TestCase):
    def setUp(self):
        self.root = SELECTORS.get(":root", {})
        self.glass = SELECTORS.get("html.loupe-glass", {})
        # app.html always carries .loupe-glass, so it wins wherever both define a name.
        self.winning = dict(self.root)
        self.winning.update(self.glass)

    def test_root_block_exists_and_is_populated(self):
        self.assertGreaterEqual(len(self.root), 10,
                                ":root token block looks truncated or moved")

    def test_glass_layer_defines_its_own_tokens(self):
        self.assertIn("--g-bg", self.glass,
                      "the glass chrome layer lost its token block")

    def test_no_token_is_used_only_via_a_fallback(self):
        """`var(--x, #hex)` where --x is defined nowhere means the fallback always wins:
        a real colour with no central definition. This was the original drift hazard."""
        # Per-element DATA variables are set inline from server values rather than
        # declared in a rule -- they are not design tokens and have no stylesheet
        # definition by construction. --ar is written onto every tile by tile() from
        # lean()'s orientation-corrected aspect ratio.
        RUNTIME_SET = {"--ar"}
        phantom = sorted({n for n, _ in var_uses(CSS)
                          if n not in ALL_DECLARED and n not in RUNTIME_SET})
        self.assertEqual(phantom, [],
                         "tokens used but never defined (fallback silently wins): %s" % phantom)

    def test_fallbacks_agree_with_definitions(self):
        """A fallback disagreeing with its definition is a second, invisible value for
        the same token, waiting for the definition to be removed."""
        bad = set()
        for name, fb in var_uses(CSS):
            if not fb:
                continue
            defined = self.winning.get(name)
            if defined and defined.lower() != fb.strip().lower():
                bad.add("%s fallback=%s defined=%s" % (name, fb.strip(), defined))
        self.assertEqual(sorted(bad), [],
                         "var() fallbacks disagree with their definitions: %s" % sorted(bad))

    def test_no_token_is_shadowed_with_a_different_value(self):
        """--ink and --line each meant two different colours until 2026-08-09, depending
        on whether you read :root or html.loupe-glass.

        Deliberately compares only those two selectors. The html[data-glass] presets
        override the same names on purpose -- that is the feature, not drift."""
        clash = {k: (self.root[k], self.glass[k])
                 for k in set(self.root) & set(self.glass)
                 if self.root[k].lower() != self.glass[k].lower()}
        self.assertEqual(clash, {},
                         "tokens defined twice with different values: %s" % clash)

    def test_rail_block_comes_after_the_rules_it_overrides(self):
        """Source order is the whole correctness story for this block.

        The rail rules sit at the same specificity as the base main/header#top rules, so
        they only win by coming later. The first attempt put the block near the top of
        the file: main{margin:0 auto} beat main{margin-left:var(--rail-w)} and the
        content slid under the rail, while the non-colliding properties still applied --
        so it looked half-right rather than obviously broken."""
        rail = CSS.find("audit 8.5: the persistent rail")
        if rail == -1:
            self.skipTest("rail block not present")
        base_main = CSS.find("main{padding:")
        base_header = CSS.find("header#top{position:fixed")
        self.assertGreater(rail, base_main,
                           "the rail block precedes the base main rule and will lose")
        self.assertGreater(rail, base_header,
                           "the rail block precedes the base header rule and will lose")

    def test_rail_offsets_every_registered_overlay(self):
        """A fixed full-viewport overlay that does not clear the rail renders underneath
        it. The registry is the source of truth for which those are."""
        if "audit 8.5: the persistent rail" not in CSS:
            self.skipTest("rail block not present")
        js_path = os.path.join(os.path.dirname(CSS_PATH), "app.js")
        with open(js_path, encoding="utf-8") as fh:
            js = fh.read()
        m = re.search(r"const OVL\s*=\s*\[(.*?)\]", js, re.S)
        self.assertIsNotNone(m, "overlay registry not found")
        registered = set(re.findall(r"['\"]([a-zA-Z0-9_-]+)['\"]", m.group(1)))
        rail_block = CSS[CSS.find("audit 8.5: the persistent rail"):]
        offset = set(re.findall(r"#([a-zA-Z0-9_-]+)", rail_block))
        missing = sorted(registered - offset)
        self.assertEqual(
            missing, [],
            "overlays that do not clear the rail and would render under it: %s" % missing)

    def test_rail_is_opt_in_via_one_variable(self):
        """--rail-w defaults to 0px, so nothing moves below the breakpoint. That is what
        makes the whole block additive and revertible."""
        self.assertRegex(CSS, r"--rail-w:\s*0px",
                         "--rail-w no longer defaults to 0 -- the rail would leak into "
                         "the narrow layout")

    def test_the_ramp_is_load_bearing(self):
        """D1's whole point: changing one ramp stop must re-skin the app.

        A ramp that is declared but unused is decoration. The semantic tokens the
        stylesheet actually references have to resolve through it."""
        if "--n-0" not in CSS:
            self.skipTest("ramp not present")
        for token in ("--bg", "--panel", "--line", "--ink", "--mut"):
            with self.subTest(token=token):
                self.assertRegex(
                    CSS, re.escape(token) + r":\s*var\(--n-\d",
                    "%s does not resolve through the ramp -- the ramp is inert" % token)

    def test_glass_layer_is_remapped_too(self):
        """html.loupe-glass re-declares several tokens at higher specificity and
        app.html always carries that class, so remapping :root alone would leave the
        ramp overridden on every served page."""
        if "--n-0" not in CSS:
            self.skipTest("ramp not present")
        blocks = [b for sel, b in re.findall(r"(html\.loupe-glass)\s*\{([^}]*)\}", CSS)]
        self.assertTrue(any("var(--n-" in b for b in blocks),
                        "html.loupe-glass does not resolve through the ramp")

    def test_no_stale_hex_fallbacks_beside_ramp_definitions(self):
        """A hex fallback next to an OKLCH definition is a value that cannot render but
        can go stale. Every token is defined, so the fallbacks were removed."""
        stale = re.findall(r"var\(\s*--(?:dim|faint|lit|keep|cut|amber|ink|line|mut)\s*,\s*#", CSS)
        self.assertEqual(stale, [], "%d stale hex fallbacks remain" % len(stale))

    def test_decision_marks_exist_for_both_states(self):
        """8.1: the mark sits ON the photograph. keep = loose ellipse, cut = one strike."""
        if "--mark-keep" not in CSS:
            self.skipTest("marks not present")
        self.assertRegex(CSS, r"\.tile\.keep::after")
        self.assertRegex(CSS, r"\.tile\.cut::after")

    def test_both_keep_colours_are_prototyped(self):
        """8.15 question 1 asks to prototype BOTH china-marker red-orange and sage rather
        than pick one, so the switch has to stay available for David to look at."""
        if "--mark-keep" not in CSS:
            self.skipTest("marks not present")
        self.assertIn("data-keepmark=rust", CSS,
                      "the rust variant is gone -- 8.15 Q1 can no longer be answered by "
                      "looking at both")
        m = re.search(r"--mark-keep:\s*([^;]+);", CSS)
        self.assertIsNotNone(m)

    def test_marks_replace_the_text_badge(self):
        """8.1: at grid density the marks are the ENTIRE decision UI -- no badges."""
        if "--mark-keep" not in CSS:
            self.skipTest("marks not present")
        self.assertRegex(CSS, r"\.tile\.(keep|cut)[^{]*\.b-st[^{]*\{[^}]*display:none",
                         "the text state badge still shows alongside the mark")

    def test_cut_frames_recede(self):
        if "--mark-keep" not in CSS:
            self.skipTest("marks not present")
        m = re.search(r"\.tile\.cut>img\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "cut frames no longer recede")
        self.assertIn("opacity:.55", m.group(1))

    def test_brand_and_font_tokens_have_a_definition_point(self):
        for token in ("--amber", "--keep", "--cut", "--ink", "--bg", "--hd", "--bd", "--mo"):
            self.assertIn(token, self.winning, "token %s is gone" % token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
