"""Query-side onnxruntime session setup -- CPU only, by design.

Counterpart to the ingest-side ort_env.py, which pins CUDAExecutionProvider and raises
if it is not granted. This module is the query side and deliberately stays on CPU: a
single 64-token text query does not justify taking the one GPU-0 resident-model slot
that Loupe shares with Ollama, Gallery ML and the vault indexer.

Named *_delta until 2026-08-09 (W16), from when the query side ran on delta, which
had no CUDA at all. Loupe moved to charlie on 2026-08-07; charlie *does* have CUDA, so the
old name implied a hardware limitation where the real reason is resource policy. The
behaviour is unchanged -- the rename was verified byte-identical on the embedding
output (tests/test_embedding_golden.py).
"""

import onnxruntime as ort

PROVIDERS = ["CPUExecutionProvider"]


def make_session(onnx_path):
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    sess = ort.InferenceSession(onnx_path, sess_options=so, providers=PROVIDERS)
    used = sess.get_providers()
    if used[0] != "CPUExecutionProvider":
        raise RuntimeError(f"{onnx_path} did not get CPUExecutionProvider, got {used}")
    return sess
