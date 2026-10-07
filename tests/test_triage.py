"""D7 / audit 9.3 -- cluster triage.

9.3: "Cluster triage mode: one cluster at a time -- 12 exemplar crops in a sheet, big
'same person?' prompt; K confirm-merge into suggested person, X not-them, -> skip. Same
muscle memory as photo culling (the app has ONE gesture language)."

The name box showed the same clusters as a grid of cards, each with four crops and its
own text field: a form to fill in rather than a triage loop. Nothing is ever the current
one, every card asks you to type, and there is no way to say "not now".

K is 9.3's "confirm-merge into suggested person" only where the suggestion earns it.
candidates() does return a per-cluster suggest {name, score, confident}: nearest named
person by centroid. Measured on this library that field runs 0.047 to 0.546, and only
about 3 in 100 clear the provisional 0.45 bar -- most unnamed clusters are simply nobody
in the named list. So a confident suggestion makes the prompt name the person and K
merges into them; without one, K means "yes, one person" and opens the naming field.
The same key, never asserting more than the score supports.

Concrete evidence for the exemplar choice: cluster 50 ranks FIRST by recurrence (81
photos over 46 days, cohesion 0.84) and is not a person at all -- it is a Buddha
wall-hanging photographed repeatedly, with a couple of real faces at the edge (0.52).
Its twelve most central faces are twelve identical statues.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class Exemplars(unittest.TestCase):
    def test_sheet_comes_from_cluster_faces_not_the_four_rep_ids(self):
        # clusters.rep_face_ids holds four. Four is enough to notice a face and not
        # enough to judge whether a cluster is ONE face.
        self.assertIn('if p == "/api/cluster/faces":', read("server.py"),
                      "the twelve-exemplar route is gone; the sheet falls back to four")

    def test_exemplars_are_spread_across_cohesion(self):
        # The twelve most central faces of a contaminated cluster all look alike -- they
        # are the cluster's own idea of itself -- so a top-12 sheet hides the one failure
        # the question "same person?" exists to catch.
        src = read("server.py")
        i = src.index('if p == "/api/cluster/faces":')
        seg = src[i:i + 2600]
        self.assertIn("rows[(i * (n - 1)) // (k - 1)]", seg,
                      "exemplars are no longer sampled across the cohesion range; a "
                      "contaminated cluster will present as twelve identical faces")

    def test_route_is_owner_only(self):
        src = read("server.py")
        i = src.index('if p == "/api/cluster/faces":')
        self.assertIn("_is_lan_peer()", src[i:i + 900],
                      "unnamed strangers' face crops are reachable by a guest")

    def test_cohesion_is_reported(self):
        src = read("server.py")
        i = src.index('if p == "/api/cluster/faces":')
        self.assertIn('"cohesion"', src[i:i + 2600])
        self.assertIn('"loosest"', src[i:i + 2600],
                      "the edge of the cluster is no longer reported, which is where a "
                      "second person shows up")


class Loop(unittest.TestCase):
    def test_one_cluster_at_a_time(self):
        js = read("static/app.js")
        self.assertIn("function renderTriage(", js, "triage mode is gone")
        self.assertIn("TR[TRI]", js, "triage no longer presents a single current cluster")

    def test_the_three_keys(self):
        js = read("static/app.js")
        i = js.index("if(triageOpen()){")
        seg = js[i:i + 700]
        self.assertIn("if(k==='k')", seg, "K is unbound in triage")
        self.assertIn("if(k==='x')", seg, "X is unbound in triage")
        self.assertIn("if(e.key==='ArrowRight')", seg, "skip is unbound in triage")

    def test_typing_is_not_triage_input(self):
        # Without this, typing a name containing x or k dismisses the cluster.
        js = read("static/app.js")
        i = js.index("if(triageOpen()){")
        self.assertIn("if(!typing){", js[i:i + 700],
                      "triage keys fire while the name field has focus; typing 'Max' "
                      "would dismiss the cluster")

    def test_naming_field_is_hidden_until_asked_for(self):
        # The default posture is answering a question, not filling in a form.
        css = read("static/app.css")
        # .trnamebox, NOT .trname: the Cutting Room tray label already owned .trname,
        # its rule is scoped (.crtray .trname) and sets no display, so an unscoped
        # .trname{display:none} won that property and hid all eight tray labels.
        m = re.search(r"\.trnamebox\{([^}]*)\}", css)
        self.assertIsNotNone(m, "the triage naming field rule is gone")
        self.assertIn("display:none", m.group(1))
        self.assertIn(".trnamebox.on{display:flex}", css)
        self.assertNotRegex(css, r"(?m)^\.trname\{",
                            "an unscoped .trname rule is back; it will hide the "
                            "Cutting Room tray labels")

    def test_dismiss_advances_before_the_round_trip(self):
        js = read("static/app.js")
        m = re.search(r"async function triageDismiss\(\)\{(.*?)\n\}", js, re.S)
        self.assertIsNotNone(m, "triageDismiss changed shape")
        body = m.group(1)
        self.assertLess(body.index("triageAdvance()"), body.index("fetch("),
                        "triage waits on the network before advancing; the decision is "
                        "already made")

    def test_triage_is_a_registered_overlay(self):
        # So closeOverlays and the Escape ladder treat it like every other layer.
        self.assertIn("'triageview'", read("static/app.js"),
                      "the triage overlay is not registered in OVL")

    def test_stale_sheet_cannot_land_on_the_next_cluster(self):
        js = read("static/app.js")
        self.assertIn("+card.dataset.cid!==cid", js,
                      "a slow exemplar fetch can paint one cluster's faces under "
                      "another cluster's question")


class Suggestion(unittest.TestCase):
    """The prompt may only claim what the score supports."""

    def test_prompt_names_the_person_only_when_confident(self):
        js = read("static/app.js")
        self.assertIn("c.suggest&&c.suggest.confident?c.suggest:null", js,
                      "triage no longer gates the named prompt on the confident flag; "
                      "it will assert a match for clusters scoring 0.05")

    def test_unconfident_clusters_get_the_neutral_question(self):
        js = read("static/app.js")
        self.assertIn("'Same person?'", js,
                      "the neutral prompt is gone; every cluster will be asked about a "
                      "person it probably is not")

    def test_confirm_merges_into_the_suggested_person(self):
        js = read("static/app.js")
        m = re.search(r"async function triageName\(\)\{(.*?)\n\}", js, re.S)
        self.assertIsNotNone(m, "triageName changed shape")
        body = m.group(1)
        self.assertIn("/api/person/assign_cluster", body,
                      "K no longer performs 9.3's confirm-merge where a suggestion is "
                      "confident")
        self.assertIn("wrap.classList.add('on')", body,
                      "K no longer falls back to naming when there is no confident "
                      "suggestion")

    def test_the_score_is_shown_with_its_caveat(self):
        # The bar is provisional and uncalibrated; presenting the score without saying so
        # would make it look like a settled threshold.
        js = read("static/app.js")
        self.assertIn("provisional bar 0.45", js,
                      "the suggestion score is shown without noting the bar is "
                      "uncalibrated")

    def test_key_hint_says_which_k_this_is(self):
        js = read("static/app.js")
        self.assertIn("'K yes, add to '+c.suggest.name", js,
                      "the key bar claims 'K same person' even when K would merge into "
                      "a named person")


if __name__ == "__main__":
    unittest.main()
