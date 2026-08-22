"""pipeline/video/ is vendored from the live working directory — prove it stayed so.

The three load-bearing video scripts exist twice: the versioned copies here, and
the originals in LOUPE_VIDEO_BASE (default ~/loupe-ml/video) that sit beside the
data they wrote. pipeline/video/README.md states the two sets are sha256-identical
and that every edit must be applied to BOTH; that identity is the provenance claim
for treating the repo copies as canonical.

Until now nothing checked it. The invariant was enforced by discipline alone, so a
one-sided edit would fork the pair silently and leave the README asserting
something false — the exact failure mode this repo's other guards (test_backup_rail,
test_portability) exist to prevent.

Skips when the originals are absent, so a checkout on another host still passes.
"""
import hashlib
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VENDORED_DIR = os.path.join(REPO, "pipeline", "video")
ORIGINALS_DIR = os.environ.get(
    "LOUPE_VIDEO_BASE", os.path.join(os.path.expanduser("~"), "loupe-ml", "video"))

# The vendored set, per pipeline/video/README.md. Everything else in the working
# directory is one-off exploration and deliberately not carried here.
VENDORED = ("video_face_pass.py", "videoscan.py", "run_full.py")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@unittest.skipUnless(os.path.isdir(ORIGINALS_DIR),
                     "%s not present (originals live only on charlie)" % ORIGINALS_DIR)
class VendoredCopiesMatchOriginals(unittest.TestCase):
    def test_every_vendored_file_has_an_original(self):
        missing = [n for n in VENDORED
                   if not os.path.isfile(os.path.join(ORIGINALS_DIR, n))]
        self.assertEqual(missing, [], "vendored files with no original in %s: %s"
                                      % (ORIGINALS_DIR, missing))

    def test_sha256_identical(self):
        drifted = []
        for name in VENDORED:
            repo_path = os.path.join(VENDORED_DIR, name)
            orig_path = os.path.join(ORIGINALS_DIR, name)
            if not (os.path.isfile(repo_path) and os.path.isfile(orig_path)):
                continue                      # covered by the test above
            a, b = sha256(repo_path), sha256(orig_path)
            if a != b:
                drifted.append("%s: repo=%s original=%s" % (name, a[:16], b[:16]))
        self.assertEqual(
            drifted, [],
            "pipeline/video/ has forked from the originals in %s.\n  %s\n"
            "Apply the edit to BOTH copies — see pipeline/video/README.md."
            % (ORIGINALS_DIR, "\n  ".join(drifted)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
