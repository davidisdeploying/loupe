"""EXIF orientation must be honoured on every decode path.

Phones record the sensor's frame plus a rotation tag rather than rotating pixels. In a
400-asset sample of this library, **43.9% carried a non-normal orientation** -- 33.7% at
tag 6 alone. Neither `server.build_preview()` nor `pipeline/gen_thumbs.make_image_thumb()`
called `ImageOps.exif_transpose`, so nearly half the library was served sideways or upside
down.

In a culling application that is not cosmetic. The entire task is judging photographs, and
you cannot judge a frame you are looking at edge-on. It went unnoticed because reading the
code tells you nothing -- `Image.open` then `draft` then `thumbnail` looks completely
correct. It was only visible in a screenshot.

`draft()` scales the DCT and does not rotate, so the transpose has to be explicit.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# Every path that decodes an original into something the user looks at.
DECODERS = {
    "server.py": ("build_preview", "PREVIEW_EDGE"),
    "pipeline/gen_thumbs.py": ("make_image_thumb", "LONG_EDGE"),
}


def read(rel):
    p = os.path.join(REPO, rel)
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class ExifOrientation(unittest.TestCase):
    def test_every_decode_path_transposes(self):
        for rel, (func, _edge) in sorted(DECODERS.items()):
            with self.subTest(path=rel):
                src = read(rel)
                if not src:
                    self.skipTest("%s not present" % rel)
                self.assertIn("exif_transpose", src,
                              "%s decodes originals without honouring EXIF orientation; "
                              "~44%% of this library would render rotated" % rel)

    def test_transpose_happens_before_the_resize(self):
        """thumbnail() fits to a box. Transposing afterwards would fit the wrong edge and
        produce a subtly wrong size as well as a wrong rotation."""
        for rel in sorted(DECODERS):
            with self.subTest(path=rel):
                src = read(rel)
                if not src or "exif_transpose" not in src:
                    self.skipTest("%s not present or unpatched" % rel)
                t = src.index("exif_transpose")
                after = src[t:]
                m = re.search(r"\.thumbnail\(", after)
                self.assertIsNotNone(
                    m, "%s resizes before transposing, or not at all" % rel)

    def test_imageops_is_imported(self):
        for rel in sorted(DECODERS):
            with self.subTest(path=rel):
                src = read(rel)
                if not src:
                    self.skipTest("%s not present" % rel)
                self.assertRegex(src, r"from PIL import [^\n]*ImageOps",
                                 "%s uses ImageOps without importing it" % rel)

    def test_transpose_result_is_guarded(self):
        """exif_transpose returns None on some inputs in older Pillow; `or im` keeps a
        malformed tag from blanking the image entirely."""
        for rel in sorted(DECODERS):
            with self.subTest(path=rel):
                src = read(rel)
                if not src or "exif_transpose" not in src:
                    self.skipTest("%s not present or unpatched" % rel)
                self.assertRegex(src, r"ImageOps\.exif_transpose\([^)]*\)\s*or\s+im",
                                 "%s does not fall back when transpose returns None" % rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
