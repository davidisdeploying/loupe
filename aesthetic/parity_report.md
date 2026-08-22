# Aesthetic Head — Parity Report

model_version: `siglip2-so400m-mlp-v1-20260714`
Generated: 2026-07-14 (delta, CPU-only, no NAS I/O)

## Data

- Source A: `~/loupe/stage5/embeddings_siglip2.db` — 61,970 SigLIP2 embeddings (1152-d, L2-normed, mean norm 1.0)
- Source B: `~/loupe/apple-enrichment.db` — 77,684 non-null `apple_score.overall` rows
- Training corpus (intersection): **N = 45,737** — matches recon estimate exactly
- y (Apple `overall`) distribution: min 0.0, max 1.0, mean 0.373, std 0.194
  - y ≥ 0.6 ("good"): 4,849 (10.6%)
  - y ≤ 0.25 ("bad"): 11,499 (25.1%)

## Split (stratified by decile bucket, seed 1729)

| split | N |
|---|---|
| train | 36,590 |
| val | 4,573 |
| test | 4,574 |

Sparse high buckets (8: y∈[0.8,0.9), 9: y∈[0.9,1.0]) confirmed present in both val (28, 4) and test (28, 5).

## Training

- MLP: 1152→256(GELU,Dropout .1)→64(GELU,Dropout .1)→1(Sigmoid)
- Weighted Huber (SmoothL1, β=0.1), inverse-sqrt-frequency per-bucket sample weights clipped [0.3, 6.0]
- Adam lr=1e-3, batch 512, early-stopped on val Spearman (patience 20)
- **Best epoch: 81** (of 101 run), **val Spearman ρ = 0.9015**
- Wall-clock: 123.1s under `CPUQuota=400%` (4-core CPU-only box)

## Parity validation — held-out TEST (never used in train/val/early-stop)

N_test = 4,574

### Rank correlation
- **Spearman ρ = 0.9032**
- Pearson r = 0.9160

### Top-K recall (MLP top-K ∩ Apple top-K, over test set)
| K | recall |
|---|---|
| 50 | 0.440 |
| 200 | 0.580 |
| 1000 | 0.752 |

### Badge 3-class agreement (good ≥0.6, bad ≤0.25, else mid)

Confusion matrix (rows = Apple, cols = predicted):

| Apple \ Pred | good | mid | bad |
|---|---|---|---|
| **good** | 345 | 139 | 1 |
| **mid** | 196 | 2566 | 166 |
| **bad** | 0 | 188 | 973 |

- Overall accuracy: **84.9%**
- "good" class: precision = **0.638**, recall = **0.711**

### Calibration
- MAE = 0.0598
- RMSE = 0.0797

Reliability table (mean predicted vs mean actual, grouped by predicted decile):

| pred decile | n | mean pred | mean actual |
|---|---|---|---|
| 0 | 542 | 0.035 | 0.046 |
| 1 | 376 | 0.149 | 0.163 |
| 2 | 547 | 0.256 | 0.271 |
| 3 | 824 | 0.354 | 0.362 |
| 4 | 1037 | 0.449 | 0.442 |
| 5 | 707 | 0.545 | 0.526 |
| 6 | 359 | 0.639 | 0.607 |
| 7 | 132 | 0.743 | 0.681 |
| 8 | 49 | 0.838 | 0.756 |
| 9 | 1 | 0.922 | 0.920 |

Calibration is tight through decile 5, then predictions run slightly hot relative to actual (over-confident) in the sparse top deciles (6-8, n=49-359) — consistent with inverse-frequency weighting pushing the model to spread out the thin high-score tail. No systematic bias in the bulk of the distribution.

## Export + faithfulness

- `aesthetic_head.onnx` (opset 17 target, dynamic batch, input `embedding` f32 [batch,1152] → output `score` f32 [batch,1])
- Verified dynamic batch (bs=1,8,100) produces correctly-shaped output
- torch vs onnxruntime on full test set (N=4574): **max abs diff = 1.34e-7**, mean abs diff = 2.45e-8 (well under the 1e-5 gate)

## Verdict

**PARITY: ρ=0.903 / top-1000 recall=0.752 / good-class P=0.638/R=0.711 — strong**

Rank correlation and calibration both hold up well against Apple's score across the full range, with sparse-high-score recall softer (P=0.64 on "good") than the bulk-range behavior — expected given only 4,849/45,737 (10.6%) training examples are in that band, even with inverse-frequency upweighting. Good enough to proceed to Step B/C (library-wide scoring) gated on this report; the top tail is the one area worth watching once scored against the full library.

## Artifacts (all under `~/aesthetic/`)

- `pairs.npz` — extracted (X, y, ids)
- `split.npz`, `split_report.json` — stratified split + per-bucket counts
- `aesthetic_head.pt` — best-val torch checkpoint
- `aesthetic_head.onnx` (+ `.onnx.data`) — exported model
- `train_history.json` — per-epoch train loss / val Spearman
- `parity_metrics.json` — full validation metrics (this report's source data)
- `onnx_faithfulness.json` — torch vs onnxruntime diff
- `sanity.json` — extraction sanity stats
- `metrics.json` — everything above, consolidated
- `extract.py`, `split.py`, `train.py`, `validate.py`, `export.py` — pipeline scripts (reproducible)

No `aesthetic.db` written. No app files, `loupe.service`, or source DBs touched (both opened read-only). Full rollback: `rm -rf ~/aesthetic`.
