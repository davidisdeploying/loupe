import numpy as np
import torch
import torch.nn as nn
import json
import time
import subprocess
from scipy.stats import spearmanr

SEED = 1729
torch.manual_seed(SEED)
np.random.seed(SEED)
torch.use_deterministic_algorithms(True, warn_only=True)

data = np.load("/home/david/loupe/aesthetic/pairs.npz")
X, y, ids = data["X"], data["y"], data["ids"]

split = np.load("/home/david/loupe/aesthetic/split.npz")
train_idx, val_idx, test_idx = split["train_idx"], split["val_idx"], split["test_idx"]

Xtr, ytr = X[train_idx], y[train_idx]
Xva, yva = X[val_idx], y[val_idx]
Xte, yte = X[test_idx], y[test_idx]

# per-sample weights on train: inverse-freq by bucket
bucket_tr = np.clip(np.floor(ytr * 10).astype(int), 0, 9)
counts = np.array([max((bucket_tr == b).sum(), 1) for b in range(10)], dtype=np.float64)
w_per_bucket = 1.0 / np.sqrt(counts)
w = w_per_bucket[bucket_tr]
w = w / w.mean()
w = np.clip(w, 0.3, 6.0)

Xtr_t = torch.tensor(Xtr, dtype=torch.float32)
ytr_t = torch.tensor(ytr, dtype=torch.float32).unsqueeze(1)
wtr_t = torch.tensor(w, dtype=torch.float32).unsqueeze(1)
Xva_t = torch.tensor(Xva, dtype=torch.float32)
yva_t = torch.tensor(yva, dtype=torch.float32).unsqueeze(1)


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
opt = torch.optim.Adam(model.parameters(), lr=1e-3)
loss_fn = nn.SmoothL1Loss(beta=0.1, reduction="none")

BATCH = 512
N_train = Xtr_t.shape[0]
MAX_EPOCHS = 200
PATIENCE = 20

best_val_spearman = -2.0
best_state = None
best_epoch = -1
epochs_no_improve = 0

gen = torch.Generator().manual_seed(SEED)

history = []
start = time.time()
last_beat = start

for epoch in range(1, MAX_EPOCHS + 1):
    model.train()
    perm = torch.randperm(N_train, generator=gen)
    epoch_loss = 0.0
    for i in range(0, N_train, BATCH):
        idx = perm[i:i + BATCH]
        xb = Xtr_t[idx]
        yb = ytr_t[idx]
        wb = wtr_t[idx]
        opt.zero_grad()
        pred = model(xb)
        loss = (loss_fn(pred, yb) * wb).mean()
        loss.backward()
        opt.step()
        epoch_loss += loss.item() * len(idx)
    epoch_loss /= N_train

    model.eval()
    with torch.no_grad():
        val_pred = model(Xva_t).squeeze(1).numpy()
    val_spearman = spearmanr(val_pred, yva).correlation

    history.append({"epoch": epoch, "train_loss": epoch_loss, "val_spearman": float(val_spearman)})

    if val_spearman > best_val_spearman:
        best_val_spearman = val_spearman
        best_state = {k: v.clone() for k, v in model.state_dict().items()}
        best_epoch = epoch
        epochs_no_improve = 0
    else:
        epochs_no_improve += 1

    now = time.time()
    if now - last_beat >= 25:
        subprocess.run([
            "/home/david/bin/fleet_beat_inline",
            "--done", str(epoch), "--total", str(MAX_EPOCHS),
            "--label", f"train epoch {epoch} val_rho={val_spearman:.4f}",
            "--token", "FLEET-WORKER1-BUILD-20260714-aesthetic-head-train",
        ], check=False)
        last_beat = now

    if epoch % 5 == 0 or epoch == 1:
        print(f"epoch {epoch:3d}  train_loss={epoch_loss:.5f}  val_spearman={val_spearman:.5f}  best={best_val_spearman:.5f}@{best_epoch}", flush=True)

    if epochs_no_improve >= PATIENCE:
        print(f"early stop at epoch {epoch} (no improvement for {PATIENCE} epochs)", flush=True)
        break

elapsed = time.time() - start
print(f"training done in {elapsed:.1f}s, best_epoch={best_epoch}, best_val_spearman={best_val_spearman:.5f}", flush=True)

model.load_state_dict(best_state)
torch.save(model.state_dict(), "/home/david/loupe/aesthetic/aesthetic_head.pt")

with open("/home/david/loupe/aesthetic/train_history.json", "w") as f:
    json.dump({
        "best_epoch": best_epoch,
        "best_val_spearman": float(best_val_spearman),
        "elapsed_sec": elapsed,
        "history": history,
    }, f, indent=2)

print("saved aesthetic_head.pt and train_history.json")
