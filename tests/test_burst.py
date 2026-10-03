"""B3 burst extras -- the rule the room states must be the rule that ran.

9.4 asks for "ceremony proportional to consequence" in the one room that prepares
deletion. Three things there did not match what the code does.

THE RULE TEXT. The room said "every frame except the sharpest in a run of three or
more". culling.py keeps BURST_KEEP=3, so the 4th-sharpest onward is flagged and a burst
of exactly 3 flags nothing at all. The data agrees: 5,956 rows across 2,104 clusters,
every sharp_rank >= 4 and every cluster_size >= 4.

THE CAPTION. 9.4 asks for "BURST EXTRA · 5 OF 7". I left the second half out earlier on
the grounds that it needed C2 -- that was wrong. cluster_id, sharp_rank and cluster_size
have been computed per frame all along; they just were not reaching the client.

THE SILENT CAP. reviewIds truncated to 4,000 ids without saying so, so "Review all
5,956" opened the first 4,000 and reported nothing.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class RuleText(unittest.TestCase):
    def test_text_matches_burst_keep(self):
        server = read("server.py")
        i = server.index('"B3":  {"title": "Burst extras"')
        seg = server[i:i + 700]
        self.assertNotIn("every frame except the sharpest", seg,
                         "the room claims a far more aggressive rule than the one that "
                         "ran; culling.py keeps the three sharpest")
        self.assertIn("3 sharpest are kept", seg,
                      "the rule slip no longer states how many frames survive a burst")

    def test_generator_still_keeps_three(self):
        # If this changes, the text above has to change with it.
        pipeline = read("pipeline/culling.py")
        self.assertIn("BURST_KEEP = 3", pipeline,
                      "BURST_KEEP moved; the rule text in server.py now describes "
                      "something else")
        self.assertIn("BURST_MIN = 3", pipeline)

    def test_text_states_the_no_op_case(self):
        # A burst of exactly 3 yields nothing, which is not obvious from "3 or more".
        server = read("server.py")
        i = server.index('"B3":  {"title": "Burst extras"')
        self.assertIn("burst of 3 flags nothing", server[i:i + 700])


class Caption(unittest.TestCase):
    def test_server_sends_the_burst_position(self):
        server = read("server.py")
        i = server.index('if p == "/api/cutting-room/ids":')
        seg = server[i:i + 1400]
        self.assertIn('out["pos"] = pos', seg,
                      "rank/size no longer reach the client, so 9.4's caption loses its "
                      "'5 OF 7'")

    def test_caption_uses_the_burst_not_the_tray_index(self):
        js = read("static/app.js")
        self.assertIn("return p?cap+' · '+p[0]+' OF '+p[1]:cap;", js,
                      "the caption no longer reports the frame's rank within its own "
                      "burst")
        self.assertNotRegex(js, r"OF '\+view\.length",
                            "the caption is reporting a position in the tray, which "
                            "reads identically and means something else")

    def test_caption_degrades_rather_than_lying(self):
        # 675 of B3's ids are Live Photo motion components, pruned from CAND but present
        # in the rule's raw id list, so they have no burst position.
        js = read("static/app.js")
        self.assertIn("const p=pos&&pos[id];", js,
                      "the caption assumes every frame has a position")


class ReviewCap(unittest.TestCase):
    def test_cap_is_named(self):
        self.assertIn("const REVIEW_CAP=4000", read("static/app.js"),
                      "the review cap is an unexplained literal again")

    def test_cap_is_announced(self):
        js = read("static/app.js")
        m = re.search(r"async function reviewIds\(ids,label,opts\)\{(.*?)\n\}", js, re.S)
        self.assertIsNotNone(m, "reviewIds changed shape")
        body = m.group(1)
        self.assertIn("opening the first ", body,
                      "reviewIds truncates silently again; 'Review all 5,956' will open "
                      "4,000 and say nothing")

    def test_unreviewable_shortfall_is_reported(self):
        js = read("static/app.js")
        m = re.search(r"async function reviewIds\(ids,label,opts\)\{(.*?)\n\}", js, re.S)
        self.assertIn("reviewable of", m.group(1),
                      "a set that opens fewer frames than it promised says nothing about "
                      "the difference")


if __name__ == "__main__":
    unittest.main()
