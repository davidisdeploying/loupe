"""C4 / audit 9.5 -- zero-shot subtract chips.

9.5: "Zero-shot chips (C4): precomputed prompt set surfaces as toggle chips on the
sheet toolbar (screenshots · documents · food · sunsets) -- one tap subtracts the junk
categories from view. The chip IS the filter state (no separate filter panel)."

Assignment is argmax over a prompt set that includes background categories, not a
threshold per junk category: a bare threshold needs a magic number each and cannot
express "this is a person, not a document".

Validated against the rule-based B2 screenshot bin, which uses filename/dimensions and
is therefore an independent signal: 5,158 of B2's 5,590 frames (92.3%) are also called
screenshot or document by the model, and it finds 5,039 more that the rule does not.
Library-wide: screenshots 10,152 · documents 901 · food 1,104 · sunsets 455.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


class Precompute(unittest.TestCase):
    def test_tool_exists(self):
        self.assertTrue(os.path.exists(os.path.join(ROOT, "tools", "zero_shot.py")),
                        "the zero-shot precompute is gone")

    def test_background_categories_exist(self):
        # Without somewhere else to land, argmax forces every ordinary photograph into
        # one of the four junk categories.
        src = read("tools/zero_shot.py")
        self.assertIn("BACKGROUND = {", src,
                      "the background prompt set is gone; argmax has nowhere else to go")
        for name in ("people", "outdoors", "animals"):
            self.assertIn('"%s"' % name, src)

    def test_only_subtractable_categories_are_stored(self):
        src = read("tools/zero_shot.py")
        self.assertIn("if name in keep", src,
                      "background assignments are being stored; the table should only "
                      "hold the frames the chips act on")

    def test_negative_scores_are_dropped(self):
        # A winner with a negative cosine is anti-correlated with the prompt that won:
        # it matched nothing. Measured at 16 of 12,628 (0.1%).
        src = read("tools/zero_shot.py")
        self.assertIn("float(bestsc[i]) > args.min_score", src,
                      "degenerate argmax winners are stored again")

    def test_write_is_atomic(self):
        # Readers must never see a half-written classification.
        src = read("tools/zero_shot.py")
        self.assertIn("os.replace(tmp, args.out)", src,
                      "the database is no longer swapped into place atomically")

    def test_not_in_the_ledger(self):
        # The ledger is for decisions no re-run can reproduce. This is derived entirely
        # from the embeddings, so it belongs outside it -- and test_backup_coverage
        # would otherwise require an off-host home for it.
        self.assertNotIn("zeroshot", read("tools/ledger_snapshot.sh"),
                         "a regenerable derived database was added to the ledger")


class Route(unittest.TestCase):
    def test_absent_database_disables_rather_than_errors(self):
        src = read("server.py")
        i = src.index('if p == "/api/zeroshot":')
        seg = src[i:i + 900]
        self.assertIn("if not os.path.exists(zpath):", seg,
                      "a fresh checkout without the precompute will error instead of "
                      "simply not showing the chips")
        self.assertIn('"disabled": True', seg)

    def test_opened_read_only(self):
        src = read("server.py")
        i = src.index('if p == "/api/zeroshot":')
        self.assertIn("mode=ro", src[i:i + 900],
                      "the classification database is opened writable by the server")


class Chips(unittest.TestCase):
    def test_membership_is_held_as_sets(self):
        # The subtract runs on every re-render; an array scan per frame per category
        # would be the slowest thing on the sheet.
        js = read("static/app.js")
        self.assertIn("ZSSET[k]=new Set(ZS.cats[k])", js,
                      "membership is no longer a Set; the subtract is O(n*m) per render")

    def test_chip_bar_survives_its_own_filter(self):
        # A query whose every result is screenshots subtracts to zero. If the chip row
        # renders inside the shelf body, it disappears with the results and there is no
        # way left to press the chip again -- measured: 60 results, 60 subtracted, 0 left.
        js = read("static/app.js")
        self.assertIn("${toolbar||''}", js,
                      "the chip bar is rendered inside the results body again; "
                      "subtracting everything will strand the user with no way back")
        self.assertNotIn("zschips()+fr", js,
                         "the chip bar is concatenated into the body, so the empty "
                         "branch drops it")

    def test_empty_state_names_the_cause(self):
        js = read("static/app.js")
        self.assertIn("every result here is subtracted by the chips above", js,
                      "an empty sheet caused by the chips reads as 'no results'")

    def test_chip_count_is_what_it_removes_here(self):
        # A chip offering to remove 10,152 screenshots from a 60-frame sheet would be a
        # lie about what pressing it does.
        js = read("static/app.js")
        self.assertIn("zsHits[c]=frames.reduce(", js,
                      "the chip shows a library-wide count rather than what it removes "
                      "from these results")


if __name__ == "__main__":
    unittest.main()
