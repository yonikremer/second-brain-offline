# tests/test_eval_pipeline.py
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Required because scripts/eval_pipeline.py imports sibling modules (helpers, constants).
# This mirrors the import style used in tests/test_pipeline.py.
sys.path.append(str(Path(__file__).parent.parent / "scripts"))
import eval_pipeline as ep

import yaml

try:
    from helpers import compute_file_hash as _compute_file_hash
except Exception:
    def _compute_file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()


class TestCoverageCheck(unittest.TestCase):
    def test_all_processed_is_ok(self):
        raw_files = [Path("/vault/raw/a.txt"), Path("/vault/raw/b.txt")]
        statuses = {
            "/vault/raw/a.txt": {"status": "processed"},
            "/vault/raw/b.txt": {"status": "processed"},
        }
        result = ep.check_coverage(raw_files, statuses)
        self.assertTrue(result["ok"])

    def test_missing_row_fails(self):
        raw_files = [Path("/vault/raw/a.txt"), Path("/vault/raw/b.txt")]
        statuses = {"/vault/raw/a.txt": {"status": "processed"}}
        result = ep.check_coverage(raw_files, statuses)
        self.assertFalse(result["ok"])
        self.assertIn("/vault/raw/b.txt", result["details"]["missing"])

    def test_pending_fails(self):
        raw_files = [Path("/vault/raw/a.txt")]
        statuses = {"/vault/raw/a.txt": {"status": "pending"}}
        result = ep.check_coverage(raw_files, statuses)
        self.assertFalse(result["ok"])

    def test_unknown_status_fails(self):
        raw_files = [Path("/vault/raw/a.txt")]
        statuses = {"/vault/raw/a.txt": {"status": "weird"}}
        result = ep.check_coverage(raw_files, statuses)
        self.assertFalse(result["ok"])
        self.assertEqual(result["details"]["unknown_status_count"], 1)

    def test_pending_status_prevents_terminal_match(self):
        raw_files = [Path("/vault/raw/a.txt"), Path("/vault/raw/b.txt")]
        statuses = {
            "/vault/raw/a.txt": {"status": "processed"},
            "/vault/raw/b.txt": {"status": "pending"},
        }
        result = ep.check_coverage(raw_files, statuses)
        self.assertFalse(result["ok"])


class TestOutputHealth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw_root = self.root / "raw"
        self.processed_root = self.root / "processed_md"
        self.instructions_root = self.root / "instructions"
        self.raw_root.mkdir()
        self.processed_root.mkdir()
        self.instructions_root.mkdir()

        (self.instructions_root / "subdomains.md").write_text(
            "# Subdomains\n\n## Allowed Subdomains\n\n1. **Tech**\n   - Tech topics\n2. **other**\n   - Other topics\n",
            encoding="utf-8",
        )
        (self.instructions_root / "document_types.md").write_text(
            "# Document Types\n\n## Allowed Document Types\n\n1. **concept**\n   - concept\n2. **other**\n   - other\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _make_frontmatter(self, src_path: Path, **overrides) -> dict:
        fm = {
            "original_path": src_path.name,
            "file_hash": _compute_file_hash(src_path),
            "subdomain": "Tech",
            "document_type": "concept",
            "truthness_score": 8,
            "truthness_justification": "trusted",
            "language": "english (skipped translation)",
            "model": "llama3",
        }
        fm.update(overrides)
        return fm

    def _write_processed(self, rel_path: str, frontmatter: dict, body: str):
        out_path = self.processed_root / Path(rel_path).with_suffix(".md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fm_text = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        out_path.write_text(f"---\n{fm_text}---\n\n{body}", encoding="utf-8")
        return out_path

    def test_load_allowed_categories(self):
        allowed = ep.load_allowed_categories(self.instructions_root / "subdomains.md")
        self.assertEqual(allowed, {"Tech", "other"})

    def test_valid_processed_file_passes(self):
        src = self.raw_root / "doc.txt"
        src.write_text("hello", encoding="utf-8")
        fm = self._make_frontmatter(src)
        out_path = self._write_processed("doc.txt", fm, "Body text here.")
        result = ep.check_processed_file(
            out_path,
            self.raw_root,
            {"Tech", "other"},
            {"concept", "other"},
        )
        self.assertEqual(result["errors"], [])

    def test_missing_frontmatter_key_fails(self):
        src = self.raw_root / "doc.txt"
        src.write_text("hello", encoding="utf-8")
        fm = self._make_frontmatter(src, model=None)
        del fm["model"]
        out_path = self._write_processed("doc.txt", fm, "Body")
        result = ep.check_processed_file(
            out_path,
            self.raw_root,
            {"Tech", "other"},
            {"concept", "other"},
        )
        self.assertTrue(any("model" in e for e in result["errors"]))

    def test_unknown_category_fails(self):
        src = self.raw_root / "doc.txt"
        src.write_text("hello", encoding="utf-8")
        fm = self._make_frontmatter(src, subdomain="Unknown")
        out_path = self._write_processed("doc.txt", fm, "Body")
        result = ep.check_processed_file(
            out_path,
            self.raw_root,
            {"Tech", "other"},
            {"concept", "other"},
        )
        self.assertTrue(any("subdomain" in e for e in result["errors"]))

    def test_score_out_of_range_fails(self):
        src = self.raw_root / "doc.txt"
        src.write_text("hello", encoding="utf-8")
        fm = self._make_frontmatter(src, truthness_score=11)
        out_path = self._write_processed("doc.txt", fm, "Body")
        result = ep.check_processed_file(
            out_path,
            self.raw_root,
            {"Tech", "other"},
            {"concept", "other"},
        )
        self.assertTrue(any("truthness_score" in e for e in result["errors"]))

    def test_hash_mismatch_fails(self):
        src = self.raw_root / "doc.txt"
        src.write_text("hello", encoding="utf-8")
        fm = self._make_frontmatter(src, file_hash="deadbeef")
        out_path = self._write_processed("doc.txt", fm, "Body")
        result = ep.check_processed_file(
            out_path,
            self.raw_root,
            {"Tech", "other"},
            {"concept", "other"},
        )
        self.assertTrue(any("mismatch" in e for e in result["errors"]))


class TestReviewQueueAndSampling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "pipeline.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE review_queue (
                id INTEGER PRIMARY KEY,
                file_hash TEXT,
                filepath TEXT,
                stage TEXT,
                trigger_type TEXT,
                context_json TEXT,
                proposed_answer TEXT,
                human_answer TEXT,
                status TEXT,
                resolution_note TEXT
            );
        """)
        conn.executemany(
            "INSERT INTO review_queue (file_hash, filepath, stage, trigger_type, status) VALUES (?, ?, ?, ?, ?)",
            [
                ("h1", "/vault/raw/a.txt", "translation", "clarification", "pending"),
                ("h2", "/vault/raw/b.txt", "truthness", "low_score", "pending"),
                ("h3", "/vault/raw/c.txt", "translation", "clarification", "stale"),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_review_queue_summary(self):
        result = ep.check_review_queue(self.db_path, total_files=10)
        self.assertFalse(result["ok"])
        self.assertEqual(result["details"]["pending_count"], 2)
        self.assertEqual(result["details"]["stale_count"], 1)
        self.assertAlmostEqual(result["details"]["review_rate"], 0.2)

    def test_sample_files(self):
        processed = [Path(f"/vault/raw/f{i}.txt") for i in range(3)]
        sample = ep.sample_files(processed, 2, seed=42)
        self.assertEqual(len(sample), 2)


if __name__ == "__main__":
    unittest.main()
