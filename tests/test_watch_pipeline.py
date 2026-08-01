import sys
import tempfile
import time
import unittest
import sqlite3
from datetime import datetime
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "scripts"))
import watch_pipeline as wp


class TestWatchSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "pipeline.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript("""
            CREATE TABLE files (filepath TEXT PRIMARY KEY, file_hash TEXT, status TEXT, updated_at TEXT);
        """)
        now = datetime.now().isoformat()
        time.sleep(0.01)
        later = datetime.now().isoformat()
        conn.executemany(
            "INSERT INTO files (filepath, status, updated_at) VALUES (?, ?, ?)",
            [
                ("/vault/raw/a.txt", "processed", now),
                ("/vault/raw/b.txt", "error", now),
                ("/vault/raw/c.txt", "pending", later),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_get_summary(self):
        summary = wp.get_summary(self.db_path)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["error"], 1)
        self.assertEqual(summary["pending"], 1)


if __name__ == "__main__":
    unittest.main()
