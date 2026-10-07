"""Shared onnxruntime session setup — pinned GPU, capped CPU threads."""

import os

import onnxruntime as ort

GPU_UUID = "GPU-00000000-0000-0000-0000-000000000000"
os.environ.setdefault("CUDA_VISIBLE_DEVICES", GPU_UUID)
os.environ.setdefault("OMP_NUM_THREADS", "4")

PROVIDERS = [("CUDAExecutionProvider", {}), "CPUExecutionProvider"]


def make_session(onnx_path):
    so = ort.SessionOptions()
    so.intra_op_num_threads = 4
    sess = ort.InferenceSession(onnx_path, sess_options=so, providers=PROVIDERS)
    used = sess.get_providers()
    if used[0] != "CUDAExecutionProvider":
        raise RuntimeError(f"{onnx_path} did not get CUDAExecutionProvider, got {used}")
    return sess
