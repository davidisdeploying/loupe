"""Golden embedding vectors -- the safety net that made the W16 rename provable.

Renaming the query-side modules touches the code path that turns a text query into a
vector. Nothing about a rename *should* change the numbers, but "should" is not
evidence, and a silent drift here degrades search quality without raising any error.
These goldens were captured before the rename and re-verified byte-identical after it.

Opt-in, because it loads the SigLIP2 text ONNX model (~3s, CPU only -- it never takes
the shared GPU-0 resident-model slot):

    LOUPE_TEST_SLOW=1 tests/run.sh golden

If a *deliberate* recipe change makes these fail, re-capture with
`tests/capture_golden.py` and say why in the commit -- do not hand-edit the JSON.
"""
import hashlib
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GOLDEN = os.path.join(HERE, "embed_golden.json")


@unittest.skipUnless(os.environ.get("LOUPE_TEST_SLOW"), "set LOUPE_TEST_SLOW=1 to run")
@unittest.skipUnless(os.path.exists(GOLDEN), "no golden file")
class EmbeddingGolden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(REPO, "stage5"))
        cls.cwd = os.getcwd()
        os.chdir(os.path.join(REPO, "stage5"))   # the recipe resolves model paths relatively
        import text_embed_cpu
        cls.TE = text_embed_cpu
        cls.golden = json.load(open(GOLDEN))

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.cwd)

    def test_vectors_are_byte_identical_to_the_goldens(self):
        import numpy as np
        for q, g in sorted(self.golden.items()):
            with self.subTest(query=q):
                a = np.asarray(self.TE.embed_text(q), dtype=np.float32).ravel()
                self.assertEqual(int(a.size), g["dim"], "embedding dimension changed")
                self.assertEqual(
                    hashlib.sha256(a.tobytes()).hexdigest(), g["sha256"],
                    "embedding for %r changed -- search quality drifts silently when this "
                    "happens, so treat it as a real regression unless the recipe was "
                    "changed deliberately" % q)

    def test_vectors_stay_l2_normalised(self):
        """The vec0 index assumes unit vectors; an un-normalised query silently
        reorders every result rather than failing."""
        import numpy as np
        for q in sorted(self.golden):
            with self.subTest(query=q):
                a = np.asarray(self.TE.embed_text(q), dtype=np.float32).ravel()
                self.assertAlmostEqual(float((a * a).sum() ** 0.5), 1.0, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
