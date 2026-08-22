"""The three-space information architecture (audit §8.5, P9).

The audit specifies restructuring 8 flat views into three spaces that match the task
rather than the data:

  Light table (browse)  — "what do I have?"   Overview · Trips · Map · People · Search
  Loupe (decide)        — "what stays?"       Focus · Cutting Room · Bursts · Dupes
  Darkroom (owner)      — "run the lab"       Vault · NSFW · Ingest/Setup · Settings

These tests hold the shipped nav to that specification, and check the one thing the
grouping fixed on the way past: `/setup`, the darkroom console, had no link anywhere in
the UI at all — it was reachable only by typing the URL.

Bursts and Dupes (C2, C6) are unbuilt, so the Loupe space is currently Cutting Room
alone. Focus is not a nav destination; it is entered from a photograph.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HTML_PATH = os.path.join(REPO, "app.html")


def read(path):
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


HTML = read(HTML_PATH)

SPEC = {
    "table": ["overviewtog", "placestog", "maptog", "peopletog", "searchtog"],
    "loupe": ["modetog"],
    "darkroom": ["vaulttog", "nsfwtog", "settingstog", "setuptog"],
}


def space_blocks():
    """id -> space, by scanning each navspace group's contents."""
    out = {}
    for m in re.finditer(r"<div class=navspace data-space=([a-z]+)(.*?)</div>", HTML, re.S):
        space, body = m.group(1), m.group(2)
        for bid in re.findall(r"<button id=([a-zA-Z0-9_]+)", body):
            out[bid] = space
    return out


@unittest.skipUnless(HTML, "app.html not found")
class ThreeSpaces(unittest.TestCase):
    def test_exactly_three_spaces_exist(self):
        spaces = re.findall(r"data-space=([a-z]+)", HTML)
        self.assertEqual(sorted(spaces), ["darkroom", "loupe", "table"],
                         "the three-space IA changed: %s" % spaces)

    def test_every_nav_button_belongs_to_exactly_one_space(self):
        """Scoped to the .hbtns header container. An earlier version matched any id
        ending in "tog" anywhere in the document and so flagged #sigtog, which is a
        control inside a view, not navigation."""
        placed = space_blocks()
        m = re.search(r"<div class=hbtns>(.*?)\n\s*</div><div class=strip>", HTML, re.S)
        if not m:
            m = re.search(r"<div class=hbtns>(.*)", HTML, re.S)
        nav_region = m.group(1)
        # The codebase names destinations *tog; #expcand and friends are contextual
        # ACTIONS that also live in the header and navigate nowhere.
        nav_buttons = set(re.findall(r"<button id=([a-zA-Z0-9_]+tog)\b", nav_region))
        orphan = sorted(b for b in nav_buttons if b not in placed)
        self.assertEqual(orphan, [],
                         "header nav buttons outside every space: %s" % orphan)

    def test_membership_matches_the_specification(self):
        placed = space_blocks()
        for space, members in sorted(SPEC.items()):
            for bid in members:
                with self.subTest(button=bid):
                    self.assertEqual(
                        placed.get(bid), space,
                        "%s should be in the %s space (audit 8.5), found in %r"
                        % (bid, space, placed.get(bid)))

    def test_darkroom_console_is_reachable_from_the_ui(self):
        """/setup had zero links anywhere before 2026-08-09 — the ingest and status
        console existed but you had to know the URL."""
        self.assertIn("setuptog", HTML)
        self.assertRegex(HTML, r"setuptog[^>]*onclick=\"location\.href='/setup'\"")

    def test_space_labels_carry_no_chroma(self):
        """Audit 8.2: chrome carries no chroma; the only hues are decisions and one
        accent. A group label is chrome."""
        css = read(os.path.join(REPO, "static", "app.css"))
        m = re.search(r"\.navspace-l\{([^}]*)\}", css)
        self.assertIsNotNone(m, ".navspace-l styling is gone")
        body = m.group(1)
        self.assertNotRegex(body, r"color:\s*#(?!.*var)",
                            "space label uses a literal colour instead of a neutral token")
        self.assertIn("var(--", body)

    def test_owner_only_spaces_keep_their_guest_gating(self):
        """Vault and NSFW ship display:none and are revealed only for LOCAL_FULLRES.
        Regrouping them must not have unhidden them for guests."""
        for bid in ("vaulttog", "nsfwtog"):
            with self.subTest(button=bid):
                m = re.search(r"<button id=%s\b[^>]*>" % bid, HTML)
                self.assertIsNotNone(m)
                self.assertIn("display:none", m.group(0),
                              "%s is no longer hidden by default — guests would see it" % bid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
