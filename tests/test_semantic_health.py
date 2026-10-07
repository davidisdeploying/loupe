import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import local_search


class SemanticHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.metadata = root / "metadata.db"
        self.embeddings = root / "embeddings.db"
        meta = sqlite3.connect(self.metadata)
        meta.execute(
            "CREATE TABLE assets(id INTEGER PRIMARY KEY, extension TEXT, mime_type TEXT)"
        )
        meta.executemany(
            "INSERT INTO assets(id,extension,mime_type) VALUES (?,?,?)",
            [
                (1, "JPG", "image/jpeg"),
                (2, "HEIC", "image/heif"),
                (3, "MOV", "video/quicktime"),
            ],
        )
        meta.commit()
        meta.close()
        vec = sqlite3.connect(self.embeddings)
        vec.execute("CREATE TABLE vec_images(asset_id INTEGER PRIMARY KEY)")
        vec.execute("CREATE TABLE processed(asset_id INTEGER, status TEXT)")
        vec.execute("INSERT INTO vec_images VALUES (1)")
        vec.commit()
        vec.close()

    def tearDown(self):
        self.tmp.cleanup()

    def health(self):
        def open_vec():
            return sqlite3.connect(self.embeddings)

        with mock.patch.object(local_search, "_open_vec_db", open_vec):
            return local_search.health(str(self.metadata), {"MOV"})

    def test_terminally_unindexable_asset_is_visible_but_not_pending(self):
        vec = sqlite3.connect(self.embeddings)
        vec.execute("INSERT INTO processed VALUES (2,'unindexable')")
        vec.commit()
        vec.close()
        result = self.health()
        self.assertTrue(result["ok"])
        self.assertEqual(result["missing_vectors"], 0)
        self.assertEqual(result["failed_assets"], 0)
        self.assertEqual(result["unindexable_assets"], 1)

    def test_retryable_failure_keeps_health_red(self):
        vec = sqlite3.connect(self.embeddings)
        vec.execute("INSERT INTO processed VALUES (2,'fail')")
        vec.commit()
        vec.close()
        result = self.health()
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_vectors"], 1)
        self.assertEqual(result["failed_assets"], 1)


if __name__ == "__main__":
    unittest.main()
