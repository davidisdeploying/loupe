"""Query-side text embedding: wraps recipe_siglip2.py unmodified, adding the
ftfy.fix_text step its canonicalize_text() skipped on the ingest side and
swapping the session provider to CPU.

Named *_delta until 2026-08-09 (W16); the query side no longer runs on
delta. ftfy is a hard runtime dependency of this module -- it went missing
from /data/loupe-venv during the 2026-08-07 host move and silently took
/api/search down until 2026-08-09.

recipe_siglip2.py's own canonicalize_text() already does the html.unescape/
strip half of open_clip's basic_clean() plus the full canonicalize_text()
proper (underscore->space, strip punctuation, lowercase, collapse whitespace).
The only piece missing is basic_clean's leading ftfy.fix_text() call, which we
apply here before delegating to the untouched recipe function -- this keeps
recipe_siglip2.py byte-identical to charlie's copy (provable via sha256) while
still matching Gallery's full text-cleaning pipeline.
"""

import numpy as np
import ftfy

import recipe_siglip2 as recipe
from ort_env_cpu import make_session

_session = None


def get_session():
    global _session
    if _session is None:
        _session = make_session(recipe.TEXTUAL_ONNX)
    return _session


def canonicalize_text_full(text):
    text = ftfy.fix_text(text)
    return recipe.canonicalize_text(text)


def tokenize_text_full(text):
    tok = recipe.get_tokenizer()
    cleaned = canonicalize_text_full(text)
    ids = tok.encode(cleaned).ids
    return np.array(ids, dtype=np.int32)


def embed_text(text):
    """Returns L2-normalized [1152] float32 embedding for a single text query."""
    sess = get_session()
    input_name = sess.get_inputs()[0].name
    ids = tokenize_text_full(text)
    out = sess.run(None, {input_name: ids[np.newaxis, ...]})[0]
    return recipe.l2_normalize(out)
