"""Overlay registry invariant (P8 focus/overlay behaviour).

`closeOverlays()` is the single place overlays are dropped, so that no `show*()` can
leak a stale sibling. That guarantee only holds for ids listed in `OVL`: an overlay that
is never registered simply cannot be closed by navigation.

On 2026-08-09 `#resmodal` was exactly that -- `position:fixed; inset:0; z-index:55`,
higher than every registered overlay, and absent from `OVL`. Opening the residence form
and navigating away left a full-viewport scrim on top of the new view.

The rule is one-directional: anything fixed and full-viewport that receives `.on` must be
registered. Extra registry members are fine -- `focus` and `persondetail` are registered
without matching that CSS shape, which is deliberate.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
JS_PATH = os.path.join(REPO, "static", "app.js")
CSS_PATH = os.path.join(REPO, "static", "app.css")


def read(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


JS = read(JS_PATH)
CSS = read(CSS_PATH)


def registry():
    m = re.search(r"const OVL\s*=\s*\[(.*?)\]", JS, re.S)
    if not m:
        return set()
    return set(re.findall(r"['\"]([a-zA-Z0-9_-]+)['\"]", m.group(1)))


def ids_turned_on():
    return set(re.findall(
        r"\$\(\s*['\"]#([a-zA-Z0-9_-]+)['\"]\s*\)\.classList\.add\(\s*['\"]on['\"]", JS))


def css_body(el_id):
    m = re.search(r"#%s\s*\{([^}]*)\}" % re.escape(el_id), CSS)
    return (m.group(1) if m else "").replace(" ", "")


def is_full_viewport_overlay(el_id):
    b = css_body(el_id)
    if "position:fixed" not in b:
        return False
    return "inset:0" in b or all(k in b for k in ("top:", "left:", "right:", "bottom:"))


@unittest.skipUnless(JS and CSS, "static assets not found")
class OverlayRegistry(unittest.TestCase):
    def test_registry_is_parseable_and_populated(self):
        self.assertGreaterEqual(len(registry()), 11, "OVL registry looks truncated or moved")

    def test_close_overlays_is_the_only_bulk_dropper(self):
        self.assertIn("function closeOverlays(", JS)
        self.assertRegex(JS, r"OVL\.forEach",
                         "closeOverlays no longer iterates the registry")

    def test_every_fullscreen_overlay_is_registered(self):
        reg = registry()
        unregistered = sorted(
            i for i in ids_turned_on()
            if is_full_viewport_overlay(i) and i not in reg)
        self.assertEqual(
            unregistered, [],
            "fixed full-viewport overlays missing from OVL: %s\n"
            "closeOverlays() cannot drop these, so navigating away leaves them on screen."
            % unregistered)

    def test_resmodal_specifically_stays_registered(self):
        """Named because it is the one that was actually broken, and because its
        z-index is the highest in the app -- when it leaks, it leaks over everything."""
        self.assertIn("resmodal", registry())

    def test_escape_dismisses_the_topmost_overlay(self):
        """#resmodal had no keyboard exit at all -- a keyboard trap. Focus view already
        bound Escape to exitFocus(), so this makes the app's own convention consistent."""
        self.assertRegex(JS, r"e\.key===['\"]Escape['\"]",
                         "Escape no longer dismisses overlays")
        self.assertIn("classList.remove('on')", JS)

    def test_escape_leaves_typing_contexts_alone(self):
        """The name autocomplete binds Escape to close its dropdown and does not stop
        propagation, so without this guard a single Escape would dismiss both it and the
        overlay behind it."""
        m = re.search(r"if\(e\.key===['\"]Escape['\"]&&view!==['\"]focus['\"].{0,700}", JS, re.S)
        self.assertIsNotNone(m, "Escape handler not found")
        seg = m.group(0)
        for guard in ("INPUT", "TEXTAREA", "isContentEditable"):
            self.assertIn(guard, seg,
                          "Escape handler does not exempt %s -- typing would close the "
                          "overlay" % guard)

    def test_escape_picks_the_topmost_layer_not_all_of_them(self):
        """Escape should step back one level, not collapse every overlay at once."""
        m = re.search(r"if\(e\.key===['\"]Escape['\"]&&view!==['\"]focus['\"].{0,700}", JS, re.S)
        self.assertIsNotNone(m)
        self.assertIn("zIndex", m.group(0),
                      "Escape no longer resolves the topmost overlay by z-index")

    def test_focus_view_keeps_its_own_escape(self):
        self.assertIn("exitFocus()", JS,
                      "focus view lost its Escape binding")

    def test_no_overlay_outranks_the_registry_silently(self):
        """A new overlay with a z-index above the registered ones is the shape of the
        next version of this bug; make sure it at least has to be registered."""
        reg = registry()
        for el_id in ids_turned_on():
            if not is_full_viewport_overlay(el_id):
                continue
            z = re.search(r"z-index:(\d+)", css_body(el_id))
            if z and int(z.group(1)) >= 45:
                self.assertIn(el_id, reg,
                              "%s sits at z-index %s and is unregistered" % (el_id, z.group(1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
