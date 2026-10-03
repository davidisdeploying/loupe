"""Every lazily-imported third-party module must actually be installed.

Four failures on 2026-08-09 shared one shape: a dependency imported inside a function, or
inside a try that swallows ImportError, so the service started clean and a feature was
silently absent with no error anywhere.

  sqlite_vec        three days of failed ledger snapshots while systemctl said SUCCESS
  ftfy              /api/search returned nothing at all
  reverse_geocoder  69,071 frames with no city -> Trips empty, is_home dead, map unlabelled

test_dependencies.py checks a hand-written list, which only protects what someone thought
to name -- reverse_geocoder was missing from it precisely because nobody knew to look.
This finds the imports instead of listing them: it walks every module with `ast`, collects
every import that is NOT at module scope, and tries each one.

Adding a new lazy dependency and forgetting to install it now fails here without anyone
having to remember it exists.
"""
import ast
import importlib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

SKIP_DIRS = {"__pycache__", ".git", ".insightface", "archive-pre-delete", "tests",
             "node_modules", "cull-dryrun", "cull-dryrun-v2", "frames", "work"}

# Optional by design, with a working fallback. osxphotos is the macOS Apple Photos API;
# enrichment/common.py probes for it and falls back to leo direct-decode on Linux, and
# that probe explicitly never raises. Anything added here needs the same standard: a real
# fallback, not a hope.
OPTIONAL = {"osxphotos": "macOS-only Apple Photos API; probed, with a leo-decode fallback"}

STDLIB = set(sys.stdlib_module_names)


def _local_module_names():
    out = set()
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                out.add(f[:-3])
    return out


def lazy_imports():
    """{module: [(relpath, lineno), ...]} for imports below module scope."""
    local = _local_module_names()
    found = {}
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            try:
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except Exception:
                continue
            top = {id(n) for n in tree.body}
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)) or id(node) in top:
                    continue
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                else:
                    names = [node.module.split(".")[0]] if node.module else []
                for n in names:
                    if n and n not in STDLIB and n not in local:
                        found.setdefault(n, []).append(
                            (os.path.relpath(path, REPO), node.lineno))
    return found


LAZY = lazy_imports()


class LazyImports(unittest.TestCase):
    def test_the_sweep_finds_imports(self):
        self.assertGreater(len(LAZY), 5,
                           "the AST sweep found almost nothing -- it has probably broken")

    def test_every_lazy_dependency_is_installed(self):
        missing = []
        for mod, sites in sorted(LAZY.items()):
            if mod in OPTIONAL:
                continue
            try:
                importlib.import_module(mod)
            except Exception as e:
                where = ", ".join("%s:%d" % s for s in sites[:3])
                missing.append("%s (%s) used at %s" % (mod, type(e).__name__, where))
        self.assertEqual(
            missing, [],
            "lazily-imported dependencies that are not installed -- each one is a "
            "feature that fails silently:\n  " + "\n  ".join(missing))

    def test_known_silent_failures_stay_covered(self):
        """The three that actually bit. If a refactor moves them to module scope this
        test stops covering them, and that is worth knowing."""
        for mod in ("sqlite_vec", "ftfy", "reverse_geocoder"):
            with self.subTest(module=mod):
                importlib.import_module(mod)

    def test_optional_modules_are_justified(self):
        for mod, why in OPTIONAL.items():
            self.assertTrue(why and len(why) > 20,
                            "%s is allowlisted without a real reason" % mod)


if __name__ == "__main__":
    unittest.main(verbosity=2)
