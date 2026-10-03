import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SemanticRefreshRuntime(unittest.TestCase):
    def test_cpu_provider_is_selectable(self):
        source = (ROOT / "tools" / "semantic_refresh.py").read_text()
        self.assertIn("LOUPE_ONNX_PROVIDER", source)
        self.assertIn("from ort_env_cpu import make_session", source)

    def test_complete_coverage_exits_before_loading_model(self):
        source = (ROOT / "tools" / "semantic_refresh.py").read_text()
        no_work = source.index("if not todo:")
        session = source.index("sess = make_session(recipe.VISUAL_ONNX)")
        self.assertLess(no_work, session)
        self.assertIn("no ONNX session loaded", source[no_work:session])


if __name__ == "__main__":
    unittest.main()
