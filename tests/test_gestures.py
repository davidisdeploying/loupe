"""D11 / audit 9.8 -- gesture vocabulary, and the undo it depends on.

The behaviour here was verified in a real Chrome with synthetic TouchEvents against
the live library (long-press peek renders and does not navigate; two-finger tap takes
a cut frame back to undecided; the frame was restored afterwards and the decision
counts returned to baseline). What these tests hold in place is the structure that
made those results possible, because each one replaces a specific defect that was
found by running the app rather than by reading it.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, "static", "app.js")
CSS = os.path.join(ROOT, "static", "app.css")


def js():
    with open(JS, encoding="utf-8") as fh:
        return fh.read()


def css():
    with open(CSS, encoding="utf-8") as fh:
        return fh.read()


class Undo(unittest.TestCase):
    """'U undo' was advertised in the help text and did not undo.

    focusDecide passes advance=true, so X cut the current frame and stepped fidx
    forward; the old handler then ran decide([seq[fidx]],'undecided') against the
    frame you had already moved to. Measured in-browser: frame 191 undecided -> X ->
    cut -> U -> still cut. Undo has to remember what it is undoing.
    """

    def test_undo_is_a_real_function(self):
        self.assertIn("function undoLast(", js(),
                      "undoLast is gone; 'u' and the two-finger tap have nothing to call")

    def test_focus_decide_records_the_previous_state(self):
        src = js()
        m = re.search(r"function focusDecide\(state\)\{(.*?)\n\}", src, re.S)
        self.assertIsNotNone(m, "focusDecide changed shape")
        body = m.group(1)
        self.assertIn("lastDec", body,
                      "focusDecide no longer records the frame and its prior state, so "
                      "undo cannot know what to restore")

    def test_u_key_calls_undo_not_a_bare_decide(self):
        src = js()
        m = re.search(r"else if\(k==='u'\)\{([^}]*)\}", src)
        self.assertIsNotNone(m, "the 'u' binding is gone")
        binding = m.group(1)
        self.assertIn("undoLast", binding,
                      "'u' is bound to something other than undoLast; the previous "
                      "binding decided the CURRENT frame, which is the frame after the "
                      "one you meant to undo")


class Gestures(unittest.TestCase):
    def test_focus_swipe_requires_a_single_finger(self):
        # Two-finger gestures put a finger in touches[0]. Without this guard a
        # pinch-zoom -- or the undo tap itself -- lifts into a swipe and decides a frame.
        src = js()
        self.assertIn("single=(e.touches.length===1)", src,
                      "the focus swipe handler no longer checks finger count; a pinch "
                      "or a two-finger tap can register as a keep/cut swipe")

    def test_long_press_peek_exists(self):
        self.assertIn("function peekTile(", js(), "the long-press peek is gone")

    def test_peek_cannot_become_a_decision_surface(self):
        # 9.8: peek is "no navigation". pointer-events:none keeps it strictly a readout.
        block = re.search(r"\.peek\{([^}]*)\}", css())
        self.assertIsNotNone(block, "the .peek rule is gone")
        self.assertIn("pointer-events:none", block.group(1),
                      "peek is interactive; it must be a read-only popover")

    def test_peek_swallows_the_click_it_generates(self):
        # A long press still emits a click, and a tile's click navigates into focus.
        # Without the swallow, peeking a tile would open it -- which is exactly the
        # navigation 9.8 says peek must not do.
        self.assertIn("swallow", js(),
                      "the post-long-press click is no longer suppressed; peeking a "
                      "tile will navigate into focus")

    def test_peek_value_column_can_shrink(self):
        # A grid 1fr track will not go below its content's min-content width, and an
        # originals/YYYY/MM/IMG_....JPG path has no break opportunity, so the value
        # column ran straight past the popover's own background.
        m = re.search(r"\.peek span\{([^}]*)\}", css())
        self.assertIsNotNone(m, "the .peek span rule is gone")
        body = m.group(1)
        self.assertIn("min-width:0", body, "the peek value column can overflow its box again")
        self.assertIn("overflow-wrap", body, "long paths will not break inside the peek")

    def test_two_finger_undo_is_wired_to_the_focus_stage(self):
        src = js()
        self.assertRegex(src, r"e\.touches\.length===2",
                         "the two-finger tap is gone from the focus stage")


class DealMode(unittest.TestCase):
    """9.8's card physics. The decisions already worked; nothing moved.

    Verified in-browser with synthetic touches: mid-drag the stage carried
    translate(60px, 10px) rotate(2.3deg); a 10px swipe decided nothing and sprang
    back; a 180px swipe cut the frame; and the stage transform cleared afterwards.
    """

    def test_card_transform_is_applied_to_the_persistent_stage(self):
        # #fstage survives every frame; its innerHTML does not. Transforming the <img>
        # would be wiped by the next render mid-gesture.
        src = js()
        self.assertIn("const st=$('#fstage')", src, "the card handler lost its stage element")

    def test_reset_runs_for_every_frame(self):
        # The bad failure mode: a transform left applied strands the next card
        # off-screen and the stage looks blank. renderFocus resets unconditionally.
        src = js()
        m = re.search(r"function renderFocus\(\)\{(.{0,400})", src, re.S)
        self.assertIsNotNone(m, "renderFocus changed shape")
        self.assertIn("resetCard", m.group(1),
                      "renderFocus no longer resets the card; a transform left applied "
                      "will strand the next frame off-screen")

    def test_reset_clears_transform_and_opacity(self):
        src = js()
        m = re.search(r"window\.resetCard=function\(\)\{(.*?)\};", src, re.S)
        self.assertIsNotNone(m, "resetCard is gone")
        body = m.group(1)
        self.assertIn("transform=''", body.replace(" ", ""),
                      "resetCard leaves a transform applied")
        self.assertIn("opacity=''", body.replace(" ", ""),
                      "resetCard leaves the stage faded out, so the next frame is invisible")

    def test_threshold_is_shared_by_move_and_decide(self):
        # A card that springs back at one distance but decides at another feels broken.
        src = js()
        self.assertIn("const THRESH=42", src, "the swipe threshold is no longer named once")

    def test_zoomed_stage_pans_rather_than_deals(self):
        # While zoomed the finger is panning the image; dragging the card away would
        # make zoom unusable.
        src = js()
        self.assertIn("focusZoomed()", src,
                      "the card handler no longer exempts the zoomed stage")

    def test_reduced_motion_is_honoured(self):
        src = js()
        self.assertIn("prefers-reduced-motion", src,
                      "deal mode ignores prefers-reduced-motion")


if __name__ == "__main__":
    unittest.main()
