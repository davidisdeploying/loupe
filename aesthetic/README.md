# aesthetic/ — how the aesthetic head was built

This directory is a **provenance record**, not part of the running application.
Nothing in Loupe imports it. It is kept in the repository because the model the
app ships was produced here, and the measurements that justify trusting it are
worth keeping next to the artifact.

## What ships and what does not

| | Tracked | Why |
|---|---|---|
| `aesthetic_head.onnx` | yes | the artifact the app actually consumes |
| `metrics.json`, `train_history.json` | yes | the evidence for how good it is |
| `aesthetic_head.pt` | no | the PyTorch checkpoint the ONNX was exported from |
| `pairs.npz`, `split.npz` | no | training pairs derived from the owner's own library |

The training inputs are derived from a specific personal photo library, so they
stay out of version control for the same reason the library does — see
`../RELEASE-BOUNDARY.md`.

## Consequence: these scripts do not run from a fresh checkout

The paths in these files are absolute and point at the machine where the model
was trained. That is deliberate and is not worth parameterising: the inputs they
read are not in the repository, so no amount of path configuration would make
them runnable elsewhere. Read them as a record of what was done, not as a
pipeline to re-run.

## The parts worth reading

- `train.py`, `split.py`, `validate.py` — pairwise preference training and the
  held-out split it was validated against.
- `export.py` — the PyTorch → ONNX export, and the **faithfulness check**: it
  runs both models over the same inputs and records the agreement in
  `onnx_faithfulness.json`, so the exported artifact is verified to behave like
  the checkpoint rather than assumed to.
- `preftool/` — a small local UI for expressing pairwise preferences, which is
  where the training pairs came from.
- `zeroshot/` — the zero-shot baseline the trained head is compared against, so
  the improvement is measured rather than claimed.
