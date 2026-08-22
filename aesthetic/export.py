import numpy as np
import torch
import torch.nn as nn
import onnxruntime as ort
import json

MODEL_VERSION = "siglip2-so400m-mlp-v1-20260714"


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

dummy = torch.randn(1, 1152, dtype=torch.float32)
torch.onnx.export(
    model,
    dummy,
    "/home/david/loupe/aesthetic/aesthetic_head.onnx",
    input_names=["embedding"],
    output_names=["score"],
    dynamic_axes={"embedding": {0: "batch"}, "score": {0: "batch"}},
    opset_version=17,
)
print("exported onnx")

# faithfulness check on full test set
data = np.load("/home/david/loupe/aesthetic/pairs.npz")
X, y, ids = data["X"], data["y"], data["ids"]
split = np.load("/home/david/loupe/aesthetic/split.npz")
test_idx = split["test_idx"]
Xte = X[test_idx]

with torch.no_grad():
    torch_pred = model(torch.tensor(Xte, dtype=torch.float32)).squeeze(1).numpy()

sess = ort.InferenceSession("/home/david/loupe/aesthetic/aesthetic_head.onnx", providers=["CPUExecutionProvider"])
onnx_pred = sess.run(["score"], {"embedding": Xte.astype(np.float32)})[0].squeeze(1)

max_abs_diff = float(np.max(np.abs(torch_pred - onnx_pred)))
mean_abs_diff = float(np.mean(np.abs(torch_pred - onnx_pred)))

faithfulness = {
    "max_abs_diff": max_abs_diff,
    "mean_abs_diff": mean_abs_diff,
    "n_compared": int(len(Xte)),
}
print(json.dumps(faithfulness, indent=2))

with open("/home/david/loupe/aesthetic/onnx_faithfulness.json", "w") as f:
    json.dump(faithfulness, f, indent=2)

with open("/home/david/loupe/aesthetic/metrics.json", "w") as f:
    train_hist = json.load(open("/home/david/loupe/aesthetic/train_history.json"))
    parity = json.load(open("/home/david/loupe/aesthetic/parity_metrics.json"))
    sanity = json.load(open("/home/david/loupe/aesthetic/sanity.json"))
    split_report = json.load(open("/home/david/loupe/aesthetic/split_report.json"))
    json.dump({
        "model_version": MODEL_VERSION,
        "sanity": sanity,
        "split": split_report,
        "train": {
            "best_epoch": train_hist["best_epoch"],
            "best_val_spearman": train_hist["best_val_spearman"],
            "elapsed_sec": train_hist["elapsed_sec"],
        },
        "parity": parity,
        "onnx_faithfulness": faithfulness,
    }, f, indent=2)

print("wrote metrics.json")
