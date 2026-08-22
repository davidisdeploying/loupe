import sqlite3
import json
import base64
import io
import os

import numpy as np
import sqlite_vec
import onnxruntime as ort
from scipy.stats import spearmanr
from PIL import Image

EMB_DB = "/home/david/loupe/stage5/embeddings_siglip2.db"
SCORE_DB = "/home/david/loupe/apple-enrichment.db"
META_DB = "/home/david/loupe-pipeline/metadata.db"
THUMBS = "/home/david/loupe-pipeline/culling/contactsheets/thumbs"
ONNX_PATH = "/home/david/loupe/aesthetic/aesthetic_head.onnx"
MODEL_VERSION = "siglip2-so400m-mlp-v1-20260714"
THUMB_MAX = 200

OUT_DIRS = [
    "/home/david/loupe/aesthetic/eyeball",
    "/home/david/Vaults/loupe-vault/experiments/aesthetic-head",
]


def open_ro(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


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


def run_head(emb_mat):
    sess = ort.InferenceSession(ONNX_PATH, providers=["CPUExecutionProvider"])
    BATCH = 4096
    preds = np.zeros((emb_mat.shape[0],), dtype=np.float32)
    for i in range(0, emb_mat.shape[0], BATCH):
        batch = emb_mat[i:i + BATCH]
        out = sess.run(["score"], {"embedding": batch})[0]
        preds[i:i + batch.shape[0]] = out.squeeze(1)
    return preds


def histogram_10bucket(values):
    edges = np.linspace(0.0, 1.0, 11)
    counts, _ = np.histogram(values, bins=edges)
    buckets = []
    for i in range(10):
        buckets.append({
            "range": f"{edges[i]:.1f}-{edges[i+1]:.1f}",
            "count": int(counts[i]),
        })
    return buckets


def percentile_ranks(values):
    # rank within array, 0..1, higher value -> higher percentile
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


def cell_html(asset_id, filenames, head_score, apple_score, pred_pct=None, apple_pct=None, thumb_cache=None):
    fn = filenames.get(asset_id, "?")
    short_fn = fn if len(fn) <= 28 else fn[:25] + "..."
    b64 = thumb_cache.get(asset_id)
    if b64:
        img_html = f'<img src="data:image/jpeg;base64,{b64}" loading="eager" />'
    else:
        img_html = '<div class="placeholder">no cached thumb</div>'
    apple_str = f"{apple_score:.3f}" if apple_score is not None else "&mdash;"
    extra = ""
    if pred_pct is not None and apple_pct is not None:
        extra = f'<div class="pct">pred pct {pred_pct*100:.0f}% / apple pct {apple_pct*100:.0f}%</div>'
    return f"""
    <div class="cell">
      {img_html}
      <div class="meta">
        <div class="fn" title="{esc(fn)}">{esc(short_fn)}</div>
        <div class="id">id {asset_id}</div>
        <div class="scores">head {head_score:.3f} &nbsp;|&nbsp; apple {apple_str}</div>
        {extra}
      </div>
    </div>"""


def section_html(title, caption, asset_ids, filenames, head_map, apple_map, pct_map=None, thumb_cache=None):
    cells = []
    missing = 0
    for aid in asset_ids:
        if thumb_cache.get(aid) is None:
            missing += 1
        pred_pct = apple_pct = None
        if pct_map is not None and aid in pct_map:
            pred_pct, apple_pct = pct_map[aid]
        cells.append(cell_html(
            aid, filenames, head_map[aid], apple_map.get(aid),
            pred_pct, apple_pct, thumb_cache,
        ))
    found = len(asset_ids) - missing
    return f"""
  <section>
    <h2>{esc(title)}</h2>
    <p class="caption">{esc(caption)} &mdash; {len(asset_ids)} assets, {found} thumbs found / {missing} missing</p>
    <div class="grid">
      {''.join(cells)}
    </div>
  </section>""", missing


def main():
    print("loading embeddings...", flush=True)
    emb_ids, emb_mat = load_embeddings()
    print(f"  {emb_mat.shape[0]} embeddings", flush=True)

    print("running onnx head over all embeddings...", flush=True)
    preds = run_head(emb_mat)
    head_map = {int(aid): float(p) for aid, p in zip(emb_ids, preds)}

    print("loading apple scores...", flush=True)
    apple_map_all = load_apple_scores()

    overlap_ids = sorted(set(head_map.keys()) & set(apple_map_all.keys()))
    unscored_ids = sorted(set(head_map.keys()) - set(apple_map_all.keys()))
    print(f"  overlap N = {len(overlap_ids)}, unscored N = {len(unscored_ids)}", flush=True)

    all_pred_values = np.array([head_map[aid] for aid in emb_ids])
    hist = histogram_10bucket(all_pred_values)

    overlap_pred = np.array([head_map[aid] for aid in overlap_ids])
    overlap_apple = np.array([apple_map_all[aid] for aid in overlap_ids])
    rho, pval = spearmanr(overlap_pred, overlap_apple)
    print(f"  in-sample spearman = {rho:.4f} (p={pval:.2e})", flush=True)

    pred_pct_overlap = percentile_ranks(overlap_pred)
    apple_pct_overlap = percentile_ranks(overlap_apple)
    pct_lookup = {aid: (pp, ap) for aid, pp, ap in zip(overlap_ids, pred_pct_overlap, apple_pct_overlap)}

    # Set a: HEAD-TOP-40 across all
    set_a = [int(aid) for aid in emb_ids[np.argsort(-all_pred_values)][:40]]

    # Set b: APPLE-TOP-40 among overlap (apple's own scored top, restricted to embedded assets)
    set_b = [aid for aid, _ in sorted(apple_map_all.items(), key=lambda kv: -kv[1]) if aid in head_map][:40]

    # Set c: HEAD-LOVES / APPLE-MEH — top by (pred_pct - apple_pct) desc
    diff_head_loves = sorted(overlap_ids, key=lambda aid: -(pct_lookup[aid][0] - pct_lookup[aid][1]))[:40]

    # Set d: APPLE-LOVES / HEAD-MEH — top by (apple_pct - pred_pct) desc
    diff_apple_loves = sorted(overlap_ids, key=lambda aid: -(pct_lookup[aid][1] - pct_lookup[aid][0]))[:40]

    # Set e: HEAD-TOP-40-UNSCORED
    unscored_pred = [(aid, head_map[aid]) for aid in unscored_ids]
    set_e = [aid for aid, _ in sorted(unscored_pred, key=lambda kv: -kv[1])[:40]]

    all_probe_ids = sorted(set(set_a) | set(set_b) | set(diff_head_loves) | set(diff_apple_loves) | set(set_e))
    print(f"total probe assets across all sets: {len(all_probe_ids)}", flush=True)

    print("loading filenames...", flush=True)
    filenames = load_filenames(all_probe_ids)

    print("loading + downscaling thumbnails...", flush=True)
    thumb_cache = {}
    for aid in all_probe_ids:
        thumb_cache[aid] = thumb_b64(aid)
    missing_total = sum(1 for v in thumb_cache.values() if v is None)
    print(f"  {len(all_probe_ids) - missing_total} found / {missing_total} missing", flush=True)

    apple_map_display = apple_map_all  # for cells needing apple score display

    sections = []
    missing_report = {}

    html, miss = section_html(
        "a. HEAD-TOP-40",
        "Top 40 assets by the trained head's predicted score, across all 61,970 embedded assets.",
        set_a, filenames, head_map, apple_map_display, None, thumb_cache,
    )
    sections.append(html); missing_report["a_head_top_40"] = miss

    html, miss = section_html(
        "b. APPLE-TOP-40",
        "Top 40 assets by Apple's own overall aesthetic score, among the 45,737 embedded+scored assets.",
        set_b, filenames, head_map, apple_map_display, None, thumb_cache,
    )
    sections.append(html); missing_report["b_apple_top_40"] = miss

    html, miss = section_html(
        "c. HEAD-LOVES / APPLE-MEH",
        "Among the overlap: head ranks these high, Apple ranks them low (pred_pct - apple_pct, desc). Biggest positive divergence.",
        diff_head_loves, filenames, head_map, apple_map_display, pct_lookup, thumb_cache,
    )
    sections.append(html); missing_report["c_head_loves_apple_meh"] = miss

    html, miss = section_html(
        "d. APPLE-LOVES / HEAD-MEH",
        "Among the overlap: Apple ranks these high, head ranks them low (apple_pct - pred_pct, desc). Biggest negative divergence.",
        diff_apple_loves, filenames, head_map, apple_map_display, pct_lookup, thumb_cache,
    )
    sections.append(html); missing_report["d_apple_loves_head_meh"] = miss

    html, miss = section_html(
        "e. HEAD-TOP-40-UNSCORED (beyond Apple)",
        "Top 40 by head prediction among the ~16,233 embedded assets Apple never scored at all.",
        set_e, filenames, head_map, apple_map_display, None, thumb_cache,
    )
    sections.append(html); missing_report["e_head_top_40_unscored"] = miss

    hist_rows = "\n".join(
        f'<tr><td>{b["range"]}</td><td>{b["count"]}</td>'
        f'<td><div class="bar" style="width:{min(100, b["count"] / max(1, max(h["count"] for h in hist)) * 100):.1f}%"></div></td></tr>'
        for b in hist
    )

    generated_utc = os.popen("date -u +'%Y-%m-%d %H:%M UTC'").read().strip()

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>Aesthetic Head Eyeball Probe — 2026-07-14</title>
<style>
  body {{
    background: #14161a;
    color: #e8e8e8;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    margin: 0;
    padding: 24px 32px 64px;
  }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .sub {{ color: #9aa0a8; font-size: 13px; margin-bottom: 24px; }}
  h2 {{ font-size: 17px; margin: 40px 0 4px; border-bottom: 1px solid #333; padding-bottom: 6px; }}
  .caption {{ color: #9aa0a8; font-size: 12.5px; margin: 4px 0 14px; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 10px;
  }}
  .cell {{
    background: #1d2025;
    border: 1px solid #2c2f36;
    border-radius: 6px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }}
  .cell img {{
    width: 100%;
    height: 140px;
    object-fit: cover;
    display: block;
    background: #0c0d0f;
  }}
  .placeholder {{
    width: 100%;
    height: 140px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #6a6f78;
    font-size: 11px;
    background: #0c0d0f;
    text-align: center;
    padding: 4px;
    box-sizing: border-box;
  }}
  .meta {{ padding: 6px 8px 8px; font-size: 11px; line-height: 1.5; }}
  .fn {{ color: #cfd3d9; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .id {{ color: #6a6f78; }}
  .scores {{ color: #a8c0e0; }}
  .pct {{ color: #d7a8e0; }}
  table.hist {{ border-collapse: collapse; font-size: 12.5px; margin-top: 8px; }}
  table.hist td {{ padding: 3px 10px 3px 0; vertical-align: middle; }}
  .bar {{ height: 10px; background: #5b8bd0; border-radius: 2px; }}
  .stats {{ font-size: 13px; color: #cfd3d9; margin: 10px 0 6px; }}
  .note {{ color: #e0b060; font-size: 12.5px; margin-top: 4px; }}
</style>
</head>
<body>
  <h1>Aesthetic Head Eyeball Probe</h1>
  <div class="sub">model {esc(MODEL_VERSION)} &middot; generated {esc(generated_utc)} &middot; read-only probe, no scores written to the app</div>

  <h2>Head score distribution (all {emb_mat.shape[0]:,} embedded assets)</h2>
  <table class="hist">
    {hist_rows}
  </table>

  <div class="stats">
    In-sample head-vs-Apple Spearman &rho; = {rho:.4f} on the {len(overlap_ids):,}-asset overlap.
    <div class="note">Note: this overlap includes assets used in training/val/eval of the head, so this is NOT a clean out-of-sample metric &mdash; treat as a rough in-sample sanity check only.</div>
  </div>

  {''.join(sections)}

</body>
</html>"""

    for d in OUT_DIRS:
        os.makedirs(d, exist_ok=True)
        out_path = os.path.join(d, "eyeball-2026-07-14.html")
        with open(out_path, "w") as f:
            f.write(html_doc)
        print(f"wrote {out_path} ({os.path.getsize(out_path)/1024:.0f} KB)", flush=True)

    result = {
        "generated_utc": generated_utc,
        "model_version": MODEL_VERSION,
        "total_embeddings": int(emb_mat.shape[0]),
        "overlap_n": len(overlap_ids),
        "unscored_n": len(unscored_ids),
        "histogram": hist,
        "in_sample_spearman": rho,
        "in_sample_spearman_p": pval,
        "missing_thumb_report": missing_report,
        "sets": {
            "a_head_top_40": [{"id": aid, "filename": filenames.get(aid), "head": head_map[aid], "apple": apple_map_all.get(aid)} for aid in set_a],
            "b_apple_top_40": [{"id": aid, "filename": filenames.get(aid), "head": head_map[aid], "apple": apple_map_all.get(aid)} for aid in set_b],
            "c_head_loves_apple_meh": [{"id": aid, "filename": filenames.get(aid), "head": head_map[aid], "apple": apple_map_all.get(aid), "pred_pct": pct_lookup[aid][0], "apple_pct": pct_lookup[aid][1]} for aid in diff_head_loves],
            "d_apple_loves_head_meh": [{"id": aid, "filename": filenames.get(aid), "head": head_map[aid], "apple": apple_map_all.get(aid), "pred_pct": pct_lookup[aid][0], "apple_pct": pct_lookup[aid][1]} for aid in diff_apple_loves],
            "e_head_top_40_unscored": [{"id": aid, "filename": filenames.get(aid), "head": head_map[aid], "apple": None} for aid in set_e],
        },
    }
    with open("/home/david/loupe/aesthetic/eyeball/eyeball_results.json", "w") as f:
        json.dump(result, f, indent=2)
    print("wrote eyeball_results.json", flush=True)


if __name__ == "__main__":
    main()
