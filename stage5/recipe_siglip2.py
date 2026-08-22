"""
Stage 5 shared recipe for the PRODUCTION model — SigLIP2 so400m onnx
(Gallery's ViT-SO400M-16-SigLIP2-384__webli, from gallery-app HF org),
extracted 2026-07-13 into ~/loupe/stage5/models/siglip2-so400m/.

  visual/model.onnx  sha256 c801892ee1737a913b729ac19f4b1ed69bbcc80755b568e7cd8266e9c8f32b6e
                     input  "image" [1,3,384,384] float32
                     output "embedding" [1,1152] float32
  textual/model.onnx sha256 52dee7c8fed53910bbe38cf4cb390a1344521b27b38f2ddf1a9b006d695c93d4
                     input  "text" [1,64] int32   (NOTE: int32, not int64 -- same gotcha as ViT-B-32)
                     output "embedding" [1,1152] float32
  embed_dim = 1152 (both towers), context_length = 64, vocab_size = 256000
  textual/model.onnx external_data spans TWO disjoint file sets that must BOTH be
  present alongside it: the raw open_clip state_dict tensors (text.token_embedding.weight,
  text.transformer.resblocks.*, text.ln_final.*, text.text_projection.*) AND separately
  exported onnx__{Add,MatMul}_NNNN constant tensors. Missing either set fails onnxruntime
  session init with "External data path does not exist". The repo ALSO ships
  textual|visual/rknpu/rk35xx/model.rknn (RK NPU exports, ~1-1.4GB each x8) which are
  irrelevant to onnxruntime CUDA inference and were not downloaded.

Preprocessing (from the model's own visual/preprocess_cfg.json -- differs from CLIP):
  size=384x384, mode=RGB, resize_mode="squash" (direct resize to 384x384, NO center-crop,
  aspect ratio NOT preserved -- unlike CLIP's shortest-side-crop), interpolation=bicubic,
  fill_color=0, mean=[0.5,0.5,0.5], std=[0.5,0.5,0.5] (SigLIP constants, not CLIP's).

Tokenization: SigLIP2 uses open_clip's HFTokenizer wrapping the repo's own
textual/tokenizer.json (GemmaTokenizerFast: sentencepiece-derived, vocab_size=256000,
NOT CLIP BPE). Per the model's config.json text_cfg.tokenizer_kwargs={"clean":
"canonicalize"}, texts are cleaned via open_clip.tokenizer.canonicalize_text(
basic_clean(text)) before tokenizing:
  basic_clean:     ftfy.fix_text -> html.unescape (x2) -> strip
                   (ftfy step SKIPPED here -- not installed in /data/loupe-venv and
                   out of this step's scope-guarded install allowance; a no-op on
                   well-formed unicode/ASCII text, but flagged as a full-pass risk
                   for inputs with mojibake/HTML entities -- see deliverable notes)
  canonicalize_text: "_" -> " ", strip all string.punctuation, lowercase,
                   collapse whitespace, strip
The tokenizer.json's own TemplateProcessing post_processor appends <eos> (id=1) to
every sequence automatically (add_bos_token=False / add_eos_token=True per
tokenizer_config.json -- no <bos> is prepended). We reproduce transformers'
max_length=64, truncation=True, padding='max_length' behavior directly via the
`tokenizers` lib's enable_truncation()/enable_padding() (verified empirically:
truncation reserves room for the trailing <eos> exactly like HF fast tokenizers do
-- a 200+ token input truncates to 63 real tokens + <eos> at position 63, not 64
real tokens with eos lost). Pad id = 0 (<pad>), pad side = right.
"""

import io
import os
import html
import string

import numpy as np
from PIL import Image, UnidentifiedImageError
from tokenizers import Tokenizer

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

try:
    import rawpy

    RAW_SUPPORTED = True
except ImportError:
    RAW_SUPPORTED = False

FALLBACK_EXTS = (".jpg", ".jpeg", ".JPG", ".JPEG", ".heic", ".HEIC", ".png", ".PNG")

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "siglip2-so400m")
VISUAL_ONNX = os.path.join(MODEL_DIR, "visual", "model.onnx")
TEXTUAL_ONNX = os.path.join(MODEL_DIR, "textual", "model.onnx")
TOKENIZER_JSON = os.path.join(MODEL_DIR, "textual", "tokenizer.json")

EMBED_DIM = 1152
CONTEXT_LENGTH = 64
PAD_TOKEN_ID = 0  # <pad>

IMAGE_SIZE = 384
MEAN = np.array([0.5, 0.5, 0.5], dtype=np.float32)
STD = np.array([0.5, 0.5, 0.5], dtype=np.float32)

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)

# Stored-vs-local mount translation. As of the 2026-08-14 share split the library lives
# at /mnt/nas2/photos on charlie and stored filepaths were rewritten to match, so both
# prefixes name the same tree and the rewrite below is inert. Kept configurable so a host
# that mounts the share elsewhere can still translate without touching metadata.db.
NAS_PATH_PREFIX_STORED = os.environ.get("LOUPE_STORED_PATH_PREFIX", "/mnt/nas2/photos/")
NAS_PATH_PREFIX_LOCAL = os.environ.get("LOUPE_LOCAL_PATH_PREFIX", "/mnt/nas2/photos/")

_tokenizer = None


def resolve_path(filepath):
    """Translate a stored metadata.db filepath to a path on this host.

    Stored paths and this host's mount currently resolve to the same tree, so the
    rewrite is inert here; it remains for a host that mounts the share elsewhere.
    If the resolved path doesn't exist (NAS basename drift), try the same
    basename with common image extensions in the same directory."""
    if filepath.startswith(NAS_PATH_PREFIX_STORED):
        filepath = NAS_PATH_PREFIX_LOCAL + filepath[len(NAS_PATH_PREFIX_STORED):]
    if os.path.exists(filepath):
        return filepath
    base, _ = os.path.splitext(filepath)
    for ext in FALLBACK_EXTS:
        candidate = base + ext
        if os.path.exists(candidate):
            return candidate
    return filepath


def _load_raw_image(filepath):
    """Decode a camera raw file (CR2/CR3/DNG/...) via rawpy. Prefers the
    embedded JPEG preview for speed; falls back to a full raw postprocess."""
    if not RAW_SUPPORTED:
        raise RuntimeError("rawpy not installed -- cannot decode raw file")
    with rawpy.imread(filepath) as raw:
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                return Image.open(io.BytesIO(thumb.data))
        except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
            pass
        rgb = raw.postprocess()
    return Image.fromarray(rgb)


def load_image(filepath):
    """Open filepath (post resolve_path) as RGB, HEIC-aware via pillow-heif.
    Falls back to rawpy for camera raw formats PIL can't identify."""
    try:
        img = Image.open(filepath)
    except UnidentifiedImageError:
        img = _load_raw_image(filepath)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img


def preprocess_image(img):
    """resize_mode='squash' -> direct resize to 384x384 (bicubic, aspect ratio
    NOT preserved, no crop), scale to [0,1], normalize with 0.5/0.5 constants, CHW."""
    img = img.resize((IMAGE_SIZE, IMAGE_SIZE), Image.BICUBIC)

    arr = np.asarray(img, dtype=np.float32) / 255.0  # HWC, [0,1]
    arr = (arr - MEAN) / STD
    arr = arr.transpose(2, 0, 1)  # CHW
    return arr.astype(np.float32)


def load_and_preprocess_image(filepath):
    resolved = resolve_path(filepath)
    img = load_image(resolved)
    return preprocess_image(img)


def get_tokenizer():
    global _tokenizer
    if _tokenizer is None:
        tok = Tokenizer.from_file(TOKENIZER_JSON)
        tok.enable_truncation(max_length=CONTEXT_LENGTH)
        tok.enable_padding(length=CONTEXT_LENGTH, pad_id=PAD_TOKEN_ID, pad_token="<pad>", direction="right")
        _tokenizer = tok
    return _tokenizer


def canonicalize_text(text):
    """open_clip's SigLIP clean_fn: basic_clean (ftfy-fix skipped, see module
    docstring) + canonicalize_text (underscore->space, strip punctuation,
    lowercase, collapse whitespace)."""
    text = html.unescape(html.unescape(text)).strip()
    text = text.replace("_", " ")
    text = text.translate(_PUNCT_TABLE)
    text = text.lower()
    text = " ".join(text.split())
    return text.strip()


def tokenize_text(text):
    """Returns int32 array shape (CONTEXT_LENGTH,): canonicalized, tokenized via
    the model's own GemmaTokenizerFast (tokenizer.json), truncated/padded to
    CONTEXT_LENGTH with trailing <eos> always preserved (post_processor-added,
    reserved-for by enable_truncation), padded with PAD_TOKEN_ID."""
    tok = get_tokenizer()
    cleaned = canonicalize_text(text)
    ids = tok.encode(cleaned).ids
    return np.array(ids, dtype=np.int32)


def l2_normalize(vec):
    vec = np.asarray(vec, dtype=np.float32)
    norm = np.linalg.norm(vec, axis=-1, keepdims=True)
    norm = np.where(norm == 0, 1.0, norm)
    return vec / norm
