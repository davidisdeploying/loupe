"""Execution-provider selection for the faces pipeline.

`faces_pipeline.py` defaults `--provider` to CPU, and the `faces` stage in
`run_control.py` never overrode it, so every in-app run embedded on CPU at ~0.63 s/asset
while a 16 GB card sat idle. `--provider auto` resolves to whatever onnxruntime really
offers.

Two properties matter more than the speed:

  * the stamped `faces.embed_provider` must be the provider that actually ran, not the
    one requested -- it is provenance, and "auto" is not a provider;
  * `auto` must fall back to CPU rather than failing, on a host without CUDA. A slow run
    beats no run.

Preferring CUDA is a throughput change, not a semantic one: faces.db already holds both
CUDA and CPU embeddings of the same buffalo_l model, so the project already treats them
as interchangeable for matching.
"""
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class ProviderResolution(unittest.TestCase):
    def setUp(self):
        try:
            import faces_pipeline
        except Exception as e:                     # heavy deps absent on a dev box
            self.skipTest("faces_pipeline not importable: %s" % e)
        self.fp = faces_pipeline

    def test_explicit_provider_is_passed_through_untouched(self):
        for p in ("CPUExecutionProvider", "CUDAExecutionProvider",
                  "TensorrtExecutionProvider"):
            self.assertEqual(self.fp.resolve_provider(p), p)

    def test_auto_never_returns_auto(self):
        """'auto' is stamped into embed_provider if it leaks through, which would make
        the provenance column a lie."""
        self.assertNotEqual(self.fp.resolve_provider("auto"), "auto")

    def test_auto_returns_a_real_available_provider(self):
        import onnxruntime as ort
        self.assertIn(self.fp.resolve_provider("auto"), ort.get_available_providers())

    def test_auto_prefers_cuda_when_present(self):
        import onnxruntime as ort
        if "CUDAExecutionProvider" not in ort.get_available_providers():
            self.skipTest("no CUDA provider on this host")
        self.assertEqual(self.fp.resolve_provider("auto"), "CUDAExecutionProvider")


class StageWiring(unittest.TestCase):
    def test_faces_stage_requests_auto(self):
        src = read(os.path.join(REPO, "run_control.py"))
        m = re.search(r'"argv":\s*\[FACES_PY[^\]]*faces_pipeline\.py[^\]]*\]', src)
        self.assertIsNotNone(m, "faces stage argv not found")
        self.assertIn('"auto"', m.group(0),
                      "the faces stage no longer requests provider auto -- it will embed "
                      "on CPU while the GPU idles")

    def test_provenance_is_resolved_before_the_model_loads(self):
        """If resolution happened after load_model, the stamp and the run could differ."""
        src = read(os.path.join(REPO, "faces_pipeline.py"))
        resolve_at = src.find("args.provider = resolve_provider")
        load_at = src.find("app = load_model(")
        self.assertGreater(resolve_at, -1, "provider resolution is gone")
        self.assertGreater(load_at, -1)
        self.assertLess(resolve_at, load_at,
                        "provider must be resolved before the model is loaded")


if __name__ == "__main__":
    unittest.main(verbosity=2)
