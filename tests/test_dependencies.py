"""Runtime dependency smoke tests.

Both outages found on 2026-08-09 were missing-dependency failures that the service
survived at startup and only expressed later, under load, in a feature nobody was
watching:

  * `sqlite_vec` -> the nightly ledger snapshot died for three days while
    `systemctl status` reported SUCCESS.
  * `ftfy`       -> `/api/search` returned nothing at all (the handler raised and
    dropped the connection) while the rest of the app looked perfectly healthy.

Both are lazy imports, which is why the service starts fine without them. These tests
import them eagerly so a missing runtime dependency fails loudly and immediately.
"""
import importlib
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "stage5"))

# Third-party packages the runtime imports lazily, with the feature each one gates.
LAZY_RUNTIME_DEPS = {
    "sqlite_vec": "vec0 row counts in the ledger snapshot (tools/ledger_snapshot.sh)",
    "ftfy": "text canonicalisation for /api/search (stage5/text_embed_cpu.py)",
    "onnxruntime": "the query-side embedding session",
    "numpy": "embedding maths",
    "PIL": "preview and thumbnail generation",
    "pillow_heif": "HEIC decoding -- most of the library is HEIC",
    # Added after it went missing unnoticed. places._geocode_recs imports it inside a
    # bare except that returns an empty record per frame, so its absence produced no
    # error anywhere: 69,071 geotagged frames simply had no city, which silently emptied
    # Trips (0 instead of 66), removed map place names, and disabled is_home -- and with
    # it the map's hide-home and away-only bursts.
    "reverse_geocoder": "offline city names: trips, map labels, is_home/hide-home",
}

# First-party modules on the lazy search path.
LAZY_PROJECT_MODULES = {
    "local_search": "semantic search entry point",
    "text_embed_cpu": "query-side text embedding (renamed from *_delta, W16)",
    "ort_env_cpu": "CPU onnxruntime session factory (renamed from *_delta, W16)",
    "recipe_siglip2": "the pinned SigLIP2 recipe",
}


class LazyDependencies(unittest.TestCase):
    def test_third_party_lazy_imports_resolve(self):
        for mod, why in sorted(LAZY_RUNTIME_DEPS.items()):
            with self.subTest(module=mod):
                try:
                    importlib.import_module(mod)
                except ImportError as e:
                    self.fail("%s is missing -- this silently breaks %s (%s)" % (mod, why, e))

    def test_project_lazy_modules_import(self):
        for mod, why in sorted(LAZY_PROJECT_MODULES.items()):
            with self.subTest(module=mod):
                try:
                    importlib.import_module(mod)
                except ImportError as e:
                    self.fail("%s failed to import -- breaks %s (%s)" % (mod, why, e))

    def test_no_module_still_named_delta(self):
        """W16. The query side has not run on delta since 2026-08-07."""
        stray = []
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "archive-pre-delete")]
            for f in files:
                if f.endswith(".py") and "delta" in f:
                    stray.append(os.path.relpath(os.path.join(root, f), REPO))
        self.assertEqual(stray, [], "modules still carrying the delta name: %s" % stray)


class VecExtension(unittest.TestCase):
    def test_vec0_actually_loads(self):
        """Importing sqlite_vec is not enough -- the ledger failure was at query time,
        on a connection where the extension had not been loaded."""
        import sqlite3
        import sqlite_vec
        con = sqlite3.connect(":memory:")
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        con.execute("CREATE VIRTUAL TABLE t USING vec0(v float[4])")
        con.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
