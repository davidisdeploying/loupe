#!/usr/bin/env python3
"""C4 / audit 9.5 -- precompute the zero-shot prompt set.

9.5: "Zero-shot chips (C4): precomputed prompt set surfaces as toggle chips on the
sheet toolbar (screenshots · documents · food · sunsets) -- one tap subtracts the junk
categories from view."

Method. Assignment is ARGMAX over a prompt set that deliberately includes background
categories, not a threshold on each junk category alone. A bare threshold needs a magic
number per category and has no way to express "this is a photo of a person, not a
document"; argmax against a background set is self-calibrating and is how zero-shot
SigLIP is normally used. Only assets whose winner is a subtractable category are
stored, which keeps the table to the frames the chips actually act on.

Each category is an ensemble of phrasings, averaged then renormalised -- one phrasing
is noticeably noisier, and the average costs nothing here since there are a few dozen
prompts in total.

Output is deliberately NOT in the ledger: it is derived entirely from the embeddings
and regenerable by re-running this, which is the opposite of what the ledger is for
(vault marks and edits, which no re-run can reproduce).
"""
import argparse
import os
import sqlite3
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "stage5"))   # text_embed_cpu lives beside the embeddings

EMB_DB = os.path.join(REPO, "stage5", "embeddings_siglip2.db")
OUT_DB = os.path.join(REPO, "stage5", "zeroshot.db")

# The four the audit names, each as an ensemble.
SUBTRACTABLE = {
    "screenshots": [
        "a screenshot of a phone screen",
        "a screenshot of a computer screen",
        "a screen capture of an app interface",
        "a photo of a screen showing a user interface",
    ],
    "documents": [
        "a scanned document",
        "a photo of a printed page of text",
        "a photo of a receipt",
        "a photo of a form or paperwork",
    ],
    "food": [
        "a photo of a plate of food",
        "a photo of a meal on a table",
        "a close-up photo of food",
        "a photo of a drink on a table",
    ],
    "sunsets": [
        "a photo of a sunset",
        "a photo of a sunrise over the horizon",
        "a photograph of the sky at golden hour",
        "a silhouette against an orange sky",
    ],
}

# Background classes exist so argmax has somewhere else to land. They are never stored;
# their only job is to stop ordinary photographs being forced into one of the four.
BACKGROUND = {
    "people": ["a photo of a person", "a portrait of someone", "a photo of a group of people"],
    "outdoors": ["a landscape photograph", "a photo of a street", "a photo of a building"],
    "animals": ["a photo of a dog", "a photo of a cat", "a photo of an animal"],
    "indoors": ["a photo of a room", "a photo of the inside of a house"],
    "objects": ["a photo of an object on a surface", "a close-up photo of a thing"],
    "vehicles": ["a photo of a car", "a photo of a vehicle"],
}


def category_vectors(embed_text):
    names, vecs = [], []
    for group in (SUBTRACTABLE, BACKGROUND):
        for name, prompts in group.items():
            m = np.stack([embed_text(p)[0].astype("float32") for p in prompts])
            v = m.mean(axis=0)
            v /= np.linalg.norm(v)
            names.append(name)
            vecs.append(v)
    return names, np.stack(vecs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emb", default=EMB_DB)
    ap.add_argument("--out", default=OUT_DB)
    ap.add_argument("--batch", type=int, default=8000)
    # A winner with a negative cosine is anti-correlated with the very prompt that won:
    # it matched nothing, rather than matching this. Measured over the full library this
    # is 16 of 12,628 assignments (0.1%), while a floor of 0.05 would discard 8.3% of
    # real ones -- so the floor exists to drop the degenerate case, not to tune recall.
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="report the assignment counts without writing the database")
    args = ap.parse_args()

    from text_embed_cpu import embed_text

    names, C = category_vectors(embed_text)
    keep = {n for n in SUBTRACTABLE}
    print("categories: " + ", ".join(names))

    # vec_images is a vec0 virtual table -- unreadable without the extension loaded,
    # even for a plain SELECT.
    con = sqlite3.connect(args.emb)
    import sqlite_vec
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    total = con.execute("SELECT count(*) FROM vec_images").fetchone()[0]
    print("assets: %d" % total)

    counts = {n: 0 for n in names}
    rows_out = []
    seen = 0
    cur = con.execute("SELECT asset_id, embedding FROM vec_images")
    while True:
        chunk = cur.fetchmany(args.batch)
        if not chunk:
            break
        ids = np.fromiter((r[0] for r in chunk), dtype=np.int64, count=len(chunk))
        M = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in chunk])
        S = M @ C.T                                   # unit-norm both sides -> cosine
        best = S.argmax(axis=1)
        bestsc = S[np.arange(len(chunk)), best]
        for i, b in enumerate(best):
            name = names[b]
            counts[name] += 1
            if name in keep and float(bestsc[i]) > args.min_score:
                rows_out.append((int(ids[i]), name, float(bestsc[i])))
        seen += len(chunk)
        print("  %d/%d" % (seen, total), end="\r", flush=True)
    con.close()
    print()

    for n in names:
        mark = "*" if n in keep else " "
        print(" %s %-12s %7d  (%.1f%%)" % (mark, n, counts[n], 100.0 * counts[n] / max(total, 1)))
    print("stored (subtractable only): %d" % len(rows_out))

    if args.dry_run:
        print("dry run -- nothing written")
        return

    tmp = args.out + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    out = sqlite3.connect(tmp)
    out.execute("CREATE TABLE zs (asset_id INTEGER PRIMARY KEY, cat TEXT NOT NULL, score REAL NOT NULL)")
    out.execute("CREATE INDEX zs_cat ON zs(cat)")
    out.executemany("INSERT INTO zs (asset_id, cat, score) VALUES (?,?,?)", rows_out)
    out.commit()
    out.execute("PRAGMA journal_mode=DELETE")         # single-file artifact, no -wal
    out.close()
    os.replace(tmp, args.out)                          # atomic: readers never see a partial db
    print("wrote %s" % args.out)


if __name__ == "__main__":
    main()
