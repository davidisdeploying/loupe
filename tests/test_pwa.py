"""PWA manifest and icon invariants (audit 9.10).

The icon set existed; the manifest that gives it meaning did not, so Loupe could not be
installed at all. And the shipped icons are the bare mark on transparency -- correct for
a favicon, wrong for maskable, where a launcher crops to its own shape and a transparent
ground shows through while anything outside the inner ~80% gets cut.

The interesting assertion is the last one: a maskable icon must be genuinely opaque at
the corners. That is the property that fails silently -- the icon looks fine in a file
browser and only goes wrong on someone's home screen.
"""
import json
import os
import re
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MANIFEST = os.path.join(REPO, "static", "site.webmanifest")
ICONS = os.path.join(REPO, "static", "icons")


def load():
    if not os.path.exists(MANIFEST):
        return None
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


M = load()


@unittest.skipUnless(M, "no web app manifest")
class Manifest(unittest.TestCase):
    def test_standard_mark_fill_and_center(self):
        source = (Path(REPO) / "static" / "brand" / "loupe-mark.svg").read_text()
        match = re.search(
            r'transform="translate\(([-\d.]+),([-\d.]+)\) scale\(([\d.]+)\)"',
            source,
        )
        self.assertIsNotNone(match)
        tx, ty, scale = map(float, match.groups())
        self.assertAlmostEqual(scale * (83.5 - 48.5), 96 * 0.76, places=3)
        self.assertAlmostEqual(tx + scale * ((48.5 + 83.5) / 2), 48, places=3)
        self.assertAlmostEqual(ty + scale * ((36.5 + 65.5) / 2), 48, places=3)

    def test_has_the_fields_an_install_needs(self):
        for k in ("name", "short_name", "start_url", "display",
                  "background_color", "theme_color", "icons"):
            self.assertIn(k, M, "manifest is missing %r" % k)

    def test_colours_come_from_the_table(self):
        """8.2/9.10: the ground is the table slate, not a fresh hex."""
        self.assertEqual(M["background_color"].lower(), "#15110d")
        self.assertEqual(M["theme_color"].lower(), "#15110d")

    def test_every_icon_file_exists(self):
        missing = []
        for ic in M["icons"]:
            rel = ic["src"].lstrip("/")
            if not os.path.exists(os.path.join(REPO, rel)):
                missing.append(ic["src"])
        self.assertEqual(missing, [], "manifest lists icons that do not exist: %s" % missing)

    def test_has_both_purposes_at_192_and_512(self):
        """"any" and "maskable" cannot be the same file: one wants the bare mark, the
        other an opaque ground with a safe zone."""
        for size in ("192x192", "512x512"):
            for purpose in ("any", "maskable"):
                with self.subTest(size=size, purpose=purpose):
                    self.assertTrue(
                        any(i.get("sizes") == size and i.get("purpose") == purpose
                            for i in M["icons"]),
                        "no %s icon at %s" % (purpose, size))

    def test_maskable_icons_are_opaque_to_the_corner(self):
        """The one that fails silently: a transparent maskable icon looks fine in a file
        browser and shows the launcher's background through it on a home screen."""
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("PIL unavailable")
        for ic in M["icons"]:
            if ic.get("purpose") != "maskable":
                continue
            with self.subTest(icon=ic["src"]):
                im = Image.open(os.path.join(REPO, ic["src"].lstrip("/"))).convert("RGBA")
                for xy in ((1, 1), (im.width - 2, 1), (1, im.height - 2),
                           (im.width - 2, im.height - 2)):
                    self.assertEqual(im.getpixel(xy)[3], 255,
                                     "%s is transparent at %s" % (ic["src"], xy))

    def test_manifest_is_linked_from_the_app(self):
        with open(os.path.join(REPO, "app.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("site.webmanifest", html, "the manifest is never linked")
        self.assertIn('name="theme-color"', html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
