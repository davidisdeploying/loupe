"""Zero-shot CLIP-IQA-style aesthetic probe: does SigLIP2 carry aesthetic/quality
signal independent of the Apple teacher, via antonym text-prompt cosine scoring
-- ZERO training. Reuses the pinned text-embedding recipe from local_search.py /
stage5/recipe_siglip2.py (Gemma tokenizer, int32 tokens, EOS append, ftfy,
L2-norm) unmodified via sys.path import -- see text_embed_cpu.embed_text.

Read-only w.r.t. the app/service/DBs. Writes only under ~/aesthetic/zeroshot/
and loupe-vault/experiments/aesthetic-head/.
"""

import sys
import os
import json
import base64
import io
import sqlite3

import numpy as np
import sqlite_vec
import onnxruntime as ort
from scipy.stats import spearmanr
from PIL import Image

sys.path.insert(0, "/home/david/loupe/stage5")
from text_embed_cpu import embed_text  # noqa: E402  (pinned recipe, see module docstring)

EMB_DB = "/home/david/loupe/stage5/embeddings_siglip2.db"
SCORE_DB = "/home/david/loupe/apple-enrichment.db"
META_DB = "/home/david/loupe-pipeline/metadata.db"
THUMBS = "/home/david/loupe-pipeline/culling/contactsheets/thumbs"
V1_HEAD_ONNX = "/home/david/loupe/aesthetic/aesthetic_head.onnx"
THUMB_MAX = 200
TEMPERATURE = 0.05

OUT_DIRS = [
    "/home/david/loupe/aesthetic/zeroshot",
    "/home/david/Vaults/loupe-vault/experiments/aesthetic-head",
]

PROMPT_AXES = {
    "AESTHETIC_A": (
        "a beautiful, well-composed photograph",
        "a poorly composed, unappealing photograph",
    ),
    "AESTHETIC_B": (
        "a great photo worth keeping",
        "a bad photo you would delete",
    ),
    "TECH_SHARP": (
        "a sharp, well-exposed, in-focus photograph",
        "a blurry, poorly-exposed, out-of-focus photograph",
    ),
}


def open_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def embed_prompt(text):
    v = embed_text(text)[0].astype(np.float32)
    norm = float(np.linalg.norm(v))
    assert v.shape == (1152,), f"unexpected shape {v.shape}"
    assert abs(norm - 1.0) < 1e-4, f"prompt embedding not unit-norm: {norm}"
    return v


def load_embeddings():
    conn = open_ro(EMB_DB)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    cur = conn.execute("SELECT asset_id, embedding FROM vec_images")
    ids, embs = [], []
    for asset_id, emb_blob in cur:
        ids.append(asset_id)
        embs.append(np.frombuffer(emb_blob, dtype=np.float32))
    conn.close()
    return np.array(ids, dtype=np.int64), np.vstack(embs).astype(np.float32)


def load_apple_scores():
    conn = open_ro(SCORE_DB)
    rows = conn.execute("SELECT asset_id, overall FROM apple_score WHERE overall IS NOT NULL").fetchall()
    conn.close()
    return {aid: overall for aid, overall in rows}


def load_filenames(asset_ids):
    conn = open_ro(META_DB)
    out = {}
    ids = list(asset_ids)
    CHUNK = 500
    for i in range(0, len(ids), CHUNK):
        chunk = ids[i:i + CHUNK]
        qmarks = ",".join("?" for _ in chunk)
        cur = conn.execute(f"SELECT id, filename FROM assets WHERE id IN ({qmarks})", chunk)
        for aid, fn in cur:
            out[aid] = fn
    conn.close()
    return out


def run_v1_head(emb_mat):
    sess = ort.InferenceSession(V1_HEAD_ONNX, providers=["CPUExecutionProvider"])
    BATCH = 4096
    preds = np.zeros((emb_mat.shape[0],), dtype=np.float32)
    for i in range(0, emb_mat.shape[0], BATCH):
        batch = emb_mat[i:i + BATCH]
        out = sess.run(["score"], {"embedding": batch})[0]
        preds[i:i + batch.shape[0]] = out.squeeze(1)
    return preds


def softmax_score(cos_pos, cos_neg, T):
    m = np.maximum(cos_pos, cos_neg)
    exp_pos = np.exp((cos_pos - m) / T)
    exp_neg = np.exp((cos_neg - m) / T)
    return exp_pos / (exp_pos + exp_neg)


def percentile_ranks(values):
    order = np.argsort(values)
    ranks = np.empty_like(order, dtype=np.float64)
    n = len(values)
    ranks[order] = np.arange(n)
    return ranks / max(n - 1, 1)


def thumb_b64(asset_id):
    path = os.path.join(THUMBS, f"{asset_id}.jpg")
    if not os.path.exists(path):
        return None
    try:
        img = Image.open(path)
        img = img.convert("RGB")
        w, h = img.size
        longest = max(w, h)
        if longest > THUMB_MAX:
            scale = THUMB_MAX / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=78)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def esc(s):
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def cell_html(asset_id, filenames, zshot, apple, v1head, extra_line=""):
    fn = filenames.get(asset_id, "?")
    short_fn = fn if len(fn) <= 28 else fn[:25] + "..."
    b64 = thumb_b64(asset_id)
    if b64:
        img_html = f'<img src="data:image/jpeg;base64,{b64}" loading="eager" />'
    else:
        img_html = '<div class="placeholder">no cached thumb</div>'
    apple_str = f"{apple:.3f}" if apple is not None else "&mdash;"
    v1_str = f"{v1head:.3f}" if v1head is not None else "&mdash;"
    return f"""
    <div class="cell">
      {img_html}
      <div class="meta">
        <div class="fn" title="{esc(fn)}">{esc(short_fn)}</div>
        <div class="id">id {asset_id}</div>
        <div class="scores">zshot {zshot:.3f} &nbsp;|&nbsp; apple {apple_str} &nbsp;|&nbsp; v1 {v1_str}</div>
        {extra_line}
      </div>
    </div>"""


def section_html(title, caption, rows):
    cells = [cell_html(*r) for r in rows]
    return f"""
  <section>
    <h2>{esc(title)}</h2>
    <p class="caption">{esc(caption)} &mdash; {len(rows)} assets</p>
    <div class="grid">
      {''.join(cells)}
    </div>
  </section>"""


def main():
    print("embedding prompts via pinned recipe...", flush=True)
    prompt_vecs = {}
    for axis, (pos, neg) in PROMPT_AXES.items():
        prompt_vecs[axis] = (embed_prompt(pos), embed_prompt(neg))
        print(f"  {axis}: pos={pos!r} neg={neg!r}  OK (unit-norm 1152d)", flush=True)

    print("loading image embeddings...", flush=True)
    emb_ids, emb_mat = load_embeddings()
    print(f"  {emb_mat.shape[0]} embeddings", flush=True)
    assert emb_mat.shape[0] == 61970, f"expected 61970 embeddings, got {emb_mat.shape[0]}"

    print("computing per-axis zero-shot scores...", flush=True)
    axis_softmax = {}
    axis_margin = {}
    for axis, (pos_vec, neg_vec) in prompt_vecs.items():
        cos_pos = emb_mat @ pos_vec
        cos_neg = emb_mat @ neg_vec
        axis_softmax[axis] = softmax_score(cos_pos, cos_neg, TEMPERATURE)
        axis_margin[axis] = cos_pos - cos_neg
        print(f"  {axis}: softmax mean={axis_softmax[axis].mean():.4f} std={axis_softmax[axis].std():.4f} "
              f"margin mean={axis_margin[axis].mean():.4f} std={axis_margin[axis].std():.4f}", flush=True)

    print("loading apple scores...", flush=True)
    apple_map = load_apple_scores()

    print("running v1 aesthetic head over all embeddings...", flush=True)
    v1_preds = run_v1_head(emb_mat)
    v1_map = {int(aid): float(p) for aid, p in zip(emb_ids, v1_preds)}

    id_to_idx = {int(aid): i for i, aid in enumerate(emb_ids)}
    overlap_ids = sorted(set(id_to_idx.keys()) & set(apple_map.keys()))
    print(f"overlap N = {len(overlap_ids)}", flush=True)
    overlap_idx = np.array([id_to_idx[aid] for aid in overlap_ids])
    apple_overlap = np.array([apple_map[aid] for aid in overlap_ids])
    v1_overlap = np.array([v1_map[aid] for aid in overlap_ids])

    def axis_at(axis_dict, axis, idx):
        return axis_dict[axis][idx]

    results = {}

    # ranking softmax vs margin order check per axis (Spearman between the two within-axis)
    order_checks = {}
    for axis in PROMPT_AXES:
        rho_order, _ = spearmanr(axis_softmax[axis], axis_margin[axis])
        order_checks[axis] = float(rho_order)

    # Spearman(AESTHETIC-A, apple) and Spearman(AESTHETIC-B, apple) on overlap
    rho_a_apple, p_a_apple = spearmanr(axis_at(axis_softmax, "AESTHETIC_A", overlap_idx), apple_overlap)
    rho_b_apple, p_b_apple = spearmanr(axis_at(axis_softmax, "AESTHETIC_B", overlap_idx), apple_overlap)
    rho_a_apple_margin, _ = spearmanr(axis_at(axis_margin, "AESTHETIC_A", overlap_idx), apple_overlap)

    # Spearman(AESTHETIC-A, v1_head) -- full embedded set (v1_head scored all 61,970)
    rho_a_v1, p_a_v1 = spearmanr(axis_softmax["AESTHETIC_A"], v1_preds)

    # Prompt sensitivity: Spearman(AESTHETIC-A, AESTHETIC-B) -- full set
    rho_a_b, p_a_b = spearmanr(axis_softmax["AESTHETIC_A"], axis_softmax["AESTHETIC_B"])

    # Spearman(TECH, apple) on overlap
    rho_tech_apple, p_tech_apple = spearmanr(axis_at(axis_softmax, "TECH_SHARP", overlap_idx), apple_overlap)
    # Spearman(TECH, AESTHETIC_A) full set, for context
    rho_tech_a, _ = spearmanr(axis_softmax["TECH_SHARP"], axis_softmax["AESTHETIC_A"])

    # top-1000 overlap between AESTHETIC-A and Apple (within the 45,737 overlap set)
    K = 1000
    a_scores_overlap = axis_at(axis_softmax, "AESTHETIC_A", overlap_idx)
    order_a = np.argsort(-a_scores_overlap)
    order_apple = np.argsort(-apple_overlap)
    top_a_ids = set(np.array(overlap_ids)[order_a[:K]].tolist())
    top_apple_ids = set(np.array(overlap_ids)[order_apple[:K]].tolist())
    top1000_overlap_n = len(top_a_ids & top_apple_ids)
    top1000_overlap_frac = top1000_overlap_n / K

    results.update({
        "temperature": TEMPERATURE,
        "n_total_embeddings": int(emb_mat.shape[0]),
        "n_overlap_with_apple": len(overlap_ids),
        "softmax_vs_margin_rank_agreement": order_checks,
        "spearman_AESTHETIC_A_vs_apple": {"rho": float(rho_a_apple), "p": float(p_a_apple)},
        "spearman_AESTHETIC_A_margin_vs_apple": float(rho_a_apple_margin),
        "spearman_AESTHETIC_B_vs_apple": {"rho": float(rho_b_apple), "p": float(p_b_apple)},
        "spearman_AESTHETIC_A_vs_v1head": {"rho": float(rho_a_v1), "p": float(p_a_v1)},
        "spearman_AESTHETIC_A_vs_AESTHETIC_B": {"rho": float(rho_a_b), "p": float(p_a_b)},
        "spearman_TECH_vs_apple": {"rho": float(rho_tech_apple), "p": float(p_tech_apple)},
        "spearman_TECH_vs_AESTHETIC_A": float(rho_tech_a),
        "top1000_overlap_AESTHETIC_A_vs_apple": {
            "n_overlap": top1000_overlap_n, "frac": top1000_overlap_frac, "k": K,
        },
    })
    print(json.dumps(results, indent=2), flush=True)

    # --- eyeball sets ---
    all_a_scores = axis_softmax["AESTHETIC_A"]
    set_a_top40 = [int(aid) for aid in emb_ids[np.argsort(-all_a_scores)][:40]]

    a_pct_overlap = percentile_ranks(a_scores_overlap)
    apple_pct_overlap = percentile_ranks(apple_overlap)
    pct_lookup = {aid: (ap, apl) for aid, ap, apl in zip(overlap_ids, a_pct_overlap, apple_pct_overlap)}
    zshot_loves_apple_meh = sorted(overlap_ids, key=lambda aid: -(pct_lookup[aid][0] - pct_lookup[aid][1]))[:40]

    probe_ids = sorted(set(set_a_top40) | set(zshot_loves_apple_meh))
    print("loading filenames for eyeball sets...", flush=True)
    filenames = load_filenames(probe_ids)

    rows_a = []
    for aid in set_a_top40:
        idx = id_to_idx[aid]
        rows_a.append((aid, filenames, float(all_a_scores[idx]), apple_map.get(aid), v1_map.get(aid), ""))

    rows_c = []
    for aid in zshot_loves_apple_meh:
        idx = id_to_idx[aid]
        pp, ap = pct_lookup[aid]
        extra = f'<div class="pct">zshot pct {pp*100:.0f}% / apple pct {ap*100:.0f}%</div>'
        rows_c.append((aid, filenames, float(all_a_scores[idx]), apple_map.get(aid), v1_map.get(aid), extra))

    html_a = section_html(
        "a. AESTHETIC-A top-40 (zero-shot)",
        "Top 40 of all 61,970 embedded assets by AESTHETIC-A softmax score "
        "('a beautiful, well-composed photograph' vs its antonym), T=0.05.",
        rows_a,
    )
    html_c = section_html(
        "b. ZERO-SHOT LOVES / APPLE MEH",
        "Among the 45,737 overlap: AESTHETIC-A ranks these high, Apple ranks them low "
        "(zshot_pct - apple_pct, desc). Candidate 'good photos Apple under-rated'.",
        rows_c,
    )

    generated_utc = os.popen("date -u +'%Y-%m-%d %H:%M UTC'").read().strip()
    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>Zero-shot Aesthetic Probe — 2026-07-14</title>
<style>
  body {{
    background: #14161a; color: #e8e8e8;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    margin: 0; padding: 24px 32px 64px;
  }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .sub {{ color: #9aa0a8; font-size: 13px; margin-bottom: 24px; }}
  h2 {{ font-size: 17px; margin: 40px 0 4px; border-bottom: 1px solid #333; padding-bottom: 6px; }}
  .caption {{ color: #9aa0a8; font-size: 12.5px; margin: 4px 0 14px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }}
  .cell {{ background: #1d2025; border: 1px solid #2c2f36; border-radius: 6px; overflow: hidden; display: flex; flex-direction: column; }}
  .cell img {{ width: 100%; height: 140px; object-fit: cover; display: block; background: #0c0d0f; }}
  .placeholder {{ width: 100%; height: 140px; display: flex; align-items: center; justify-content: center; color: #6a6f78; font-size: 11px; background: #0c0d0f; text-align: center; padding: 4px; box-sizing: border-box; }}
  .meta {{ padding: 6px 8px 8px; font-size: 11px; line-height: 1.5; }}
  .fn {{ color: #cfd3d9; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .id {{ color: #6a6f78; }}
  .scores {{ color: #a8c0e0; }}
  .pct {{ color: #d7a8e0; }}
  .stats {{ font-size: 13px; color: #cfd3d9; margin: 10px 0 6px; white-space: pre-wrap; }}
</style>
</head>
<body>
  <h1>Zero-shot Aesthetic Probe (CLIP-IQA-style antonym prompts)</h1>
  <div class="sub">SigLIP2 so400m, pinned text recipe &middot; generated {esc(generated_utc)} &middot; read-only probe, no scores written to the app</div>

  <div class="stats">{esc(json.dumps(results, indent=2))}</div>

  {html_a}
  {html_c}

</body>
</html>"""

    for d in OUT_DIRS:
        os.makedirs(d, exist_ok=True)
        out_path = os.path.join(d, "zeroshot-2026-07-14.html")
        with open(out_path, "w") as f:
            f.write(html_doc)
        print(f"wrote {out_path} ({os.path.getsize(out_path)/1024:.0f} KB)", flush=True)

    out_json = {
        "generated_utc": generated_utc,
        "results": results,
        "sets": {
            "a_aesthetic_A_top40": [
                {"id": aid, "filename": filenames.get(aid), "zshot": float(all_a_scores[id_to_idx[aid]]),
                 "apple": apple_map.get(aid), "v1_head": v1_map.get(aid)}
                for aid in set_a_top40
            ],
            "b_zshot_loves_apple_meh": [
                {"id": aid, "filename": filenames.get(aid), "zshot": float(all_a_scores[id_to_idx[aid]]),
                 "apple": apple_map.get(aid), "v1_head": v1_map.get(aid),
                 "zshot_pct": pct_lookup[aid][0], "apple_pct": pct_lookup[aid][1]}
                for aid in zshot_loves_apple_meh
            ],
        },
    }
    with open("/home/david/loupe/aesthetic/zeroshot/zeroshot_results.json", "w") as f:
        json.dump(out_json, f, indent=2)
    print("wrote zeroshot_results.json", flush=True)


if __name__ == "__main__":
    main()
