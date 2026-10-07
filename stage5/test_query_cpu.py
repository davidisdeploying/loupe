"""Step 4a unit test: verify delta can embed text queries with the pinned
SigLIP2 textual tower, CPU-only, matching charlie's recipe."""

import sys

import numpy as np

sys.path.insert(0, "/home/david/loupe/stage5")

import recipe_siglip2 as recipe
from text_embed_cpu import embed_text, get_session, tokenize_text_full

QUERIES = [
    "a photo of a person",
    "a screenshot of a phone or computer screen",
    "a photo taken outdoors",
    "a dog",
]


def main():
    sess = get_session()
    print("providers actually used:", sess.get_providers())

    vecs = []
    for q in QUERIES:
        vec = embed_text(q)
        assert vec.shape == (1, 1152), f"bad shape {vec.shape} for {q!r}"
        norm = float(np.linalg.norm(vec))
        assert np.isfinite(vec).all(), f"non-finite values for {q!r}"
        assert abs(norm - 1.0) < 1e-4, f"norm {norm} not ~1.0 for {q!r}"
        print(f"  {q!r:50s} shape={vec.shape} norm={norm:.6f} finite=True")
        vecs.append(vec[0])

    print("\npairwise cosines (queries differ => should be clearly < 1.0):")
    for i in range(len(QUERIES)):
        for j in range(i + 1, len(QUERIES)):
            cos = float(np.dot(vecs[i], vecs[j]))
            print(f"  cos({QUERIES[i]!r}, {QUERIES[j]!r}) = {cos:.4f}")

    print("\n200-token truncation probe:")
    long_text = " ".join(["word"] * 200)
    ids = tokenize_text_full(long_text)
    print(f"  ids shape: {ids.shape}, dtype: {ids.dtype}")
    print(f"  id at position 63 (0-indexed): {ids[63]}")
    tok = recipe.get_tokenizer()
    eos_id = tok.token_to_id("<eos>")
    print(f"  <eos> token id: {eos_id}")
    assert ids[63] == eos_id, f"expected <eos> ({eos_id}) at position 63, got {ids[63]}"
    print("  PASS: <eos> at position 63 (truncation reserved the EOS slot)")

    print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
