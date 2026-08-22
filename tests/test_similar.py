"""D9 / audit 9.5 + C4 -- more like this.

9.5: "'More like this' (C4) from any frame's context menu / S in focus -- the asset's
own embedding as query; results carry a similarity ring (conic, accent) so the falloff
is visible."

S was advertised in the help text as "skip" and bound to nothing: there is no k==='s'
branch in the focus handler, so pressing it did nothing at all. Skip already has two
real bindings (arrow-right, and swipe up).

Measured: S on frame #9089 returns 60 neighbours with rings spanning 1.00 down to 0.18
and absolute cosine 0.739 -> 0.6808 on their titles.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class SimilarQuery(unittest.TestCase):
    def test_similar_exists(self):
        self.assertIn("def similar(asset_id", read("local_search.py"),
                      "the embedding-as-query path is gone")

    def test_similar_excludes_the_query_asset(self):
        # Without this the first result is always the frame you are already looking at.
        src = read("local_search.py")
        m = re.search(r"def similar\(asset_id.*?\n\n\n", src, re.S)
        self.assertIsNotNone(m, "similar() changed shape")
        self.assertIn("if aid == asset_id:", m.group(0),
                      "similar() no longer drops the query asset from its own results")

    def test_similar_never_loads_the_text_model(self):
        # The query vector comes from the index. Loading the ~2.7GB onnxruntime session
        # to answer "more like this" would make it far more expensive than it needs to be.
        src = read("local_search.py")
        m = re.search(r"def similar\(asset_id.*?\n\n\n", src, re.S)
        self.assertNotIn("_get_embed_text", m.group(0),
                         "similar() loads the text model; the asset's own embedding is "
                         "already in the index")

    def test_route_is_gated_like_search(self):
        # It reads the same index, so it answers to the same setting.
        src = read("server.py")
        i = src.index('if p == "/api/similar":')
        self.assertIn('_search_settings() != "local"', src[i:i + 400],
                      "/api/similar is not gated by the search setting")

    def test_route_returns_similarity_per_item(self):
        src = read("server.py")
        i = src.index('if p == "/api/similar":')
        self.assertIn('it["sim"]', src[i:i + 1200],
                      "the route no longer returns a similarity per item, so the ring "
                      "has nothing to encode")


class Ring(unittest.TestCase):
    def test_ring_is_conic(self):
        m = re.search(r"\.simring\{([^}]*)\}", read("static/app.css"))
        self.assertIsNotNone(m, "the similarity ring is gone")
        self.assertIn("conic-gradient", m.group(1), "9.5 asks for a conic ring")

    def test_ring_is_rank_relative_with_the_absolute_on_the_title(self):
        # Measured: the 59 neighbours of #18085 span cosine 0.8916-0.9238, a spread of
        # 0.032. An absolute conic is a nearly-full circle on every result and shows no
        # falloff -- the one thing the ring exists to show.
        js = read("static/app.js")
        self.assertIn("const ringFrac=v=>{", js, "the ring normalisation is gone")
        self.assertIn("0.18+0.82*((v-smin)/(smax-smin))", js,
                      "the ring is no longer rank-relative; with a 0.03 absolute spread "
                      "every ring will look identical")
        self.assertIn('title="cosine ${it.sim}"', js,
                      "the absolute cosine is no longer available on the ring")


class SKey(unittest.TestCase):
    def test_s_is_bound(self):
        self.assertIn("else if(k==='s'){moreLikeThis();}", read("static/app.js"),
                      "S is unbound again, and the keymap advertises it")

    def test_help_no_longer_claims_s_is_skip(self):
        js = read("static/app.js")
        self.assertNotIn("S skip", js,
                         "the help text advertises S as skip again; it is more-like-this")


if __name__ == "__main__":
    unittest.main()
