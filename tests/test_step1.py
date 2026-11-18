import sqlite3
import tempfile
import unittest
from pathlib import Path

from jobwatcher.config import load_config
from jobwatcher.storage import initialize_storage


class Step1Test(unittest.TestCase):
    def test_config_loading(self) -> None:
        config = load_config("config.yaml")
        self.assertTrue(config["sources"]["remotive"]["enabled"])
        self.assertTrue(config["sources"]["remoteok"]["enabled"])
        self.assertTrue(config["sources"]["himalayas"]["enabled"])
        self.assertFalse(config["sources"]["adzuna"]["enabled"])
        self.assertEqual(config["embeddings"]["backend"], "ollama")
        self.assertEqual(config["embeddings"]["model"], "nomic-embed-text")
        self.assertEqual(config["embeddings"]["similarity_threshold"], 0.35)

    def test_sqlite_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "jobs.db"
            connection = initialize_storage(database)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            connection.close()

            self.assertIn("jobs", tables)
            self.assertNotIn("pipeline_state", tables)


if __name__ == "__main__":
    unittest.main()
