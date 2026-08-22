import numpy as np
import torch
import torch.nn as nn
import json
from scipy.stats import spearmanr, pearsonr

data = np.load("/home/david/loupe/aesthetic/pairs.npz")
X, y, ids = data["X"], data["y"], data["ids"]

split = np.load("/home/david/loupe/aesthetic/split.npz")
test_idx = split["test_idx"]

Xte, yte = X[test_idx], y[test_idx]
ids_te = ids[test_idx]


class AestheticHead(nn.Module):
    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(1152, 256),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(256, 64),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(64, 1),
            nn.Sigmoid(),
        )

    def forward(self, x):
        return self.net(x)


model = AestheticHead()
model.load_state_dict(torch.load("/home/david/loupe/aesthetic/aesthetic_head.pt"))
model.eval()

with torch.no_grad():
    pred = model(torch.tensor(Xte, dtype=torch.float32)).squeeze(1).numpy()

N = len(yte)

# --- Spearman / Pearson ---
rho = spearmanr(pred, yte).correlation
r, _ = pearsonr(pred, yte)

# --- Top-K recall ---
topk_results = {}
order_apple = np.argsort(-yte)
order_pred = np.argsort(-pred)
for K in (50, 200, 1000):
    k = min(K, N)
    apple_top = set(ids_te[order_apple[:k]].tolist())
    pred_top = set(ids_te[order_pred[:k]].tolist())
    recall = len(apple_top & pred_top) / k
    topk_results[str(K)] = {"k_used": k, "recall": recall}

# --- Badge 3-class agreement ---
def badge(v):
    if v >= 0.6:
        return "good"
    elif v <= 0.25:
        return "bad"
    else:
        return "mid"

apple_badge = np.array([badge(v) for v in yte])
pred_badge = np.array([badge(v) for v in pred])

classes = ["good", "mid", "bad"]
confusion = {a: {p: 0 for p in classes} for a in classes}
for a, p in zip(apple_badge, pred_badge):
    confusion[a][p] += 1

accuracy = float((apple_badge == pred_badge).mean())

tp = confusion["good"]["good"]
fn = confusion["good"]["mid"] + confusion["good"]["bad"]
fp = confusion["mid"]["good"] + confusion["bad"]["good"]
precision_good = tp / (tp + fp) if (tp + fp) > 0 else float("nan")
recall_good = tp / (tp + fn) if (tp + fn) > 0 else float("nan")

# --- Calibration ---
mae = float(np.mean(np.abs(pred - yte)))
rmse = float(np.sqrt(np.mean((pred - yte) ** 2)))

# reliability table: 10 buckets by PREDICTED decile
pred_decile = np.clip(np.floor(pred * 10).astype(int), 0, 9)
reliability = []
for d in range(10):
    mask = pred_decile == d
    n_d = int(mask.sum())
    if n_d == 0:
        reliability.append({"decile": d, "n": 0, "mean_pred": None, "mean_actual": None})
        continue
    reliability.append({
        "decile": d,
        "n": n_d,
        "mean_pred": float(pred[mask].mean()),
        "mean_actual": float(yte[mask].mean()),
    })

report = {
    "N_test": int(N),
    "spearman_rho": float(rho),
    "pearson_r": float(r),
    "topk_recall": topk_results,
    "badge_confusion": confusion,
    "badge_accuracy": accuracy,
    "good_class_precision": precision_good,
    "good_class_recall": recall_good,
    "mae": mae,
    "rmse": rmse,
    "reliability_by_pred_decile": reliability,
}

with open("/home/david/loupe/aesthetic/parity_metrics.json", "w") as f:
    json.dump(report, f, indent=2)

print(json.dumps(report, indent=2))
