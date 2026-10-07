"""D9 / audit 9.5 -- one input, three shelves.

9.5: "it fans results into three labeled shelves: Places/Trips (name match), People
(person match), Frames (SigLIP2 semantic). Shelves render as horizontal sheet strips;
Enter on a shelf expands it to a full sheet with the query pinned as a filter chip."

Search returned one flat grid of semantic frame hits, so a query that was plainly a
place or a person answered only with frames the model thought looked like the words.
Searching "baird" is the clearest case: the trip shelf answers it exactly, and the
semantic shelf returns book covers.

And the palette's '#' grammar could not work at all: it filters the registry to kinds
'trip' and 'place', and neither was ever pushed. Measured before: 310 entries -- 11
view, 20 person, 25 year, 254 month, 0 trip -- and "#baird" returned zero rows against
a real 258-frame trip. After: 376 entries including 66 trips, and 16 rows.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSS = os.path.join(ROOT, "static", "app.css")
JS = os.path.join(ROOT, "static", "app.js")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class PaletteGrammar(unittest.TestCase):
    def test_trips_are_loaded_into_the_registry(self):
        js = read(JS)
        self.assertIn("async function palLoadTrips(", js,
                      "trips are no longer loaded for the palette, so '#' has nothing "
                      "to match")

    def test_trip_entries_are_pushed(self):
        js = read(JS)
        self.assertIn("kind:'trip'", js,
                      "no trip entries reach the palette registry; the '#' grammar the "
                      "palette advertises cannot match anything")

    def test_palette_open_loads_both_sources(self):
        js = read(JS)
        self.assertIn("Promise.all([palLoadPeople(),palLoadTrips()])", js,
                      "the palette no longer loads trips on open")

    def test_hash_branch_still_targets_those_kinds(self):
        # If this filter changes, the entries above have to change with it.
        js = read(JS)
        self.assertIn("r.kind==='trip'||r.kind==='place'", js,
                      "the '#' branch no longer selects trip/place kinds")


class Shelves(unittest.TestCase):
    def test_three_shelves_are_rendered(self):
        js = read(JS)
        for label in ("'Places & Trips'", "'People'", "'Frames'"):
            self.assertIn(label, js, "the " + label + " shelf is gone")

    def test_name_shelves_match_against_the_registry(self):
        js = read(JS)
        self.assertIn("function shelfMatches(kinds,q)", js,
                      "the name shelves no longer match against the palette registry")
        self.assertIn("shelfMatches(['trip','place']", js)
        self.assertIn("shelfMatches(['person']", js)

    def test_strips_are_horizontal(self):
        m = re.search(r"\.shelfstrip\{([^}]*)\}", read(CSS))
        self.assertIsNotNone(m, "the shelf strip rule is gone")
        body = m.group(1)
        self.assertIn("display:flex", body, "9.5 asks for horizontal sheet strips")
        self.assertIn("overflow-x:auto", body)

    def test_enter_expands_a_shelf(self):
        js = read(JS)
        self.assertIn("h.onkeydown=e=>{if(e.key==='Enter'", js,
                      "Enter no longer expands a shelf")

    def test_expanded_shelf_pins_the_query_as_a_chip(self):
        js = read(JS)
        self.assertIn("class=shelfchip", js,
                      "the query is no longer pinned as a filter chip on the expanded "
                      "shelf")
        self.assertIn("${open?chip:''}", js,
                      "the chip is shown on collapsed shelves too, or not at all")

    def test_flat_grid_does_not_lay_out_the_shelves(self):
        # #searchgrid carries class=grid and keeps its own columns for the old layout.
        self.assertIn("#searchgrid:has(.shelf){display:block}", read(CSS),
                      "the shelves will be placed into the old flat grid's columns")

    def test_shelf_text_is_legible_on_the_paper_slab(self):
        # html.loupe-glass .grid paints #searchgrid as a cream slab via a background
        # IMAGE, which reports no backgroundColor -- so the contrast looked correct by
        # computed value (L 0.919 on L 0.181) while being unreadable on screen.
        css = read(CSS)
        self.assertIn("html.loupe-glass .shelfhd h3{color:#241d15}", css,
                      "shelf headings use the dark theme's light ink on the paper slab "
                      "and will be nearly invisible")
        self.assertIn("html.loupe-glass .shelfcard .scname{color:#241d15}", css,
                      "shelf card names are unreadable on the paper slab")


if __name__ == "__main__":
    unittest.main()
