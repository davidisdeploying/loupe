import numpy as np
import json

rng = np.random.default_rng(1729)

data = np.load("/home/david/loupe/aesthetic/pairs.npz")
X, y, ids = data["X"], data["y"], data["ids"]
N = len(y)

bucket = np.clip(np.floor(y * 10).astype(int), 0, 9)

train_idx, val_idx, test_idx = [], [], []
bucket_counts = {}

for b in range(10):
    b_idx = np.where(bucket == b)[0]
    rng.shuffle(b_idx)
    n = len(b_idx)
    n_train = int(round(n * 0.8))
    n_val = int(round(n * 0.1))
    # remainder goes to test to keep splits summing to n
    tr = b_idx[:n_train]
    va = b_idx[n_train:n_train + n_val]
    te = b_idx[n_train + n_val:]
    train_idx.append(tr)
    val_idx.append(va)
    test_idx.append(te)
    bucket_counts[b] = {"total": int(n), "train": int(len(tr)), "val": int(len(va)), "test": int(len(te))}

train_idx = np.concatenate(train_idx)
val_idx = np.concatenate(val_idx)
test_idx = np.concatenate(test_idx)

rng.shuffle(train_idx)
rng.shuffle(val_idx)
rng.shuffle(test_idx)

np.savez(
    "/home/david/loupe/aesthetic/split.npz",
    train_idx=train_idx, val_idx=val_idx, test_idx=test_idx
)

report = {
    "N": int(N),
    "train_N": int(len(train_idx)),
    "val_N": int(len(val_idx)),
    "test_N": int(len(test_idx)),
    "per_bucket": bucket_counts,
}
with open("/home/david/loupe/aesthetic/split_report.json", "w") as f:
    json.dump(report, f, indent=2)

print(json.dumps(report, indent=2))

# confirm sparse high buckets (8,9) appear in val AND test
for b in (8, 9):
    c = bucket_counts[b]
    assert c["val"] > 0, f"bucket {b} missing from val"
    assert c["test"] > 0, f"bucket {b} missing from test"
print("OK: sparse high buckets (8,9) present in val and test")
