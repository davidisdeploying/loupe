#!/usr/bin/env python3
"""Re-capture tests/embed_golden.json.

Only run this for a DELIBERATE recipe change, and say why in the commit message --
otherwise a real regression gets blessed into the goldens. See
tests/test_embedding_golden.py.
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "stage5"))
os.chdir(os.path.join(REPO, "stage5"))

import numpy as np                     # noqa: E402
import text_embed_cpu as TE            # noqa: E402

QUERIES = ["a dog on a beach at sunset", "screenshot of a spreadsheet",
           "birthday cake with candles", ""]

out = {}
for q in QUERIES:
    a = np.asarray(TE.embed_text(q), dtype=np.float32).ravel()
    out[q] = {"dim": int(a.size),
              "sha256": hashlib.sha256(a.tobytes()).hexdigest(),
              "l2": float((a * a).sum() ** 0.5),
              "head": [round(float(x), 6) for x in a[:4]]}
    print("  %-34r dim=%d sha=%s" % (q, a.size, out[q]["sha256"][:16]))

dest = os.path.join(HERE, "embed_golden.json")
json.dump(out, open(dest, "w"), indent=2, sort_keys=True)
print("wrote", dest)
