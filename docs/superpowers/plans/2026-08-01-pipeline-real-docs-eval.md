# Pipeline Real-Docs Evaluation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only evaluation harness (`eval_pipeline.py`) and live monitor (`watch_pipeline.py`) that prove the document pipeline works on real docs without modifying the pipeline itself.

**Architecture:** Two standalone scripts read from the existing `pipeline.db`, `processed_md/`, `raw/`, and `instructions/` artifacts. The evaluator runs after the pipeline and produces a Markdown report with coverage, output-health, review-queue, and sample-forensics checks. The watcher polls the same database while the pipeline runs and prints a live progress board.

**Tech Stack:** Python 3.12, `sqlite3`, `yaml` (stdlib), `rich` (already in `.venv`), `unittest` (matching the existing test suite).

---

## File structure

| File | Responsibility |
|------|----------------|
| `scripts/eval_pipeline.py` | Post-run evaluator: coverage, output health, review queue, sampling, forensics, report writing, CLI. |
| `scripts/watch_pipeline.py` | Live monitor: polls `pipeline.db`, renders progress board, estimates ETA. |
| `tests/test_eval_pipeline.py` | Unit/integration tests for the evaluator. |
| `tests/test_watch_pipeline.py` | Unit tests for the watcher summary logic. |

Both new scripts are **read-only** with respect to `pipeline.db`, `processed_md/`, `raw/`, and `review/`.

---

## Task 1: Coverage check — failing test

**Files:**
- Create: `tests/test_eval_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
import sys
import unittest
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "scripts"))
import eval_pipeline as ep


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

    def test_terminal_count_mismatch_fails(self):
        raw_files = [Path("/vault/raw/a.txt"), Path("/vault/raw/b.txt")]
        statuses = {
            "/vault/raw/a.txt": {"status": "processed"},
            "/vault/raw/b.txt": {"status": "pending"},
        }
        result = ep.check_coverage(raw_files, statuses)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_eval_pipeline.py -v`

Expected: import error or `AttributeError: module 'eval_pipeline' has no attribute 'check_coverage'`.

---

## Task 2: Coverage check — implementation

**Files:**
- Create: `scripts/eval_pipeline.py`

- [ ] **Step 1: Write minimal implementation**

```python
import sqlite3
from pathlib import Path


TERMINAL_STATUSES = {"processed", "filtered", "error", "needs_review", "skipped"}


def _db_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def get_raw_files(raw_root: Path) -> list[Path]:
    files = []
    for p in raw_root.rglob("*"):
        if p.is_file() and not p.name.startswith("."):
            files.append(p.resolve())
    return sorted(files)


def get_file_statuses(db_path: Path) -> dict[str, dict]:
    conn = _db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT filepath, file_hash, status, error_message FROM files")
        return {row["filepath"]: dict(row) for row in cursor.fetchall()}
    finally:
        conn.close()


def check_coverage(raw_files: list[Path], file_statuses: dict[str, dict]) -> dict:
    raw_strs = [str(p) for p in raw_files]
    missing = [r for r in raw_strs if r not in file_statuses]
    statuses = list(file_statuses.values())
    pending = [s for s in statuses if s.get("status") == "pending"]
    unknown = [s for s in statuses if s.get("status") not in TERMINAL_STATUSES | {"pending"}]
    terminal_count = sum(1 for s in statuses if s.get("status") in TERMINAL_STATUSES)
    ok = (
        not missing
        and not pending
        and not unknown
        and terminal_count == len(raw_files)
    )
    return {
        "name": "Coverage",
        "ok": ok,
        "critical": True,
        "details": {
            "raw_count": len(raw_files),
            "db_count": len(file_statuses),
            "missing": missing,
            "pending_count": len(pending),
            "unknown_status_count": len(unknown),
            "terminal_count": terminal_count,
        },
    }


def main():
    pass


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_eval_pipeline.py::TestCoverageCheck -v`

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add scripts/eval_pipeline.py tests/test_eval_pipeline.py
git commit -m "feat(eval): add coverage check for real-docs pipeline

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Output health — failing tests

**Files:**
- Modify: `tests/test_eval_pipeline.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_pipeline.py`:

```python
import tempfile

import yaml

from helpers import compute_file_hash


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
        fm = {
            "original_path": "doc.txt",
            "file_hash": compute_file_hash(src),
            "subdomain": "Tech",
            "document_type": "concept",
            "truthness_score": 8,
            "truthness_justification": "trusted",
            "language": "english (skipped translation)",
            "model": "llama3",
        }
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
        fm = {
            "original_path": "doc.txt",
            "file_hash": compute_file_hash(src),
            "subdomain": "Tech",
            "document_type": "concept",
            "truthness_score": 8,
            "truthness_justification": "trusted",
            "language": "english (skipped translation)",
            # missing model
        }
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
        fm = {
            "original_path": "doc.txt",
            "file_hash": compute_file_hash(src),
            "subdomain": "Unknown",
            "document_type": "concept",
            "truthness_score": 8,
            "truthness_justification": "trusted",
            "language": "english (skipped translation)",
            "model": "llama3",
        }
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
        fm = {
            "original_path": "doc.txt",
            "file_hash": compute_file_hash(src),
            "subdomain": "Tech",
            "document_type": "concept",
            "truthness_score": 11,
            "truthness_justification": "trusted",
            "language": "english (skipped translation)",
            "model": "llama3",
        }
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
        fm = {
            "original_path": "doc.txt",
            "file_hash": "deadbeef",
            "subdomain": "Tech",
            "document_type": "concept",
            "truthness_score": 8,
            "truthness_justification": "trusted",
            "language": "english (skipped translation)",
            "model": "llama3",
        }
        out_path = self._write_processed("doc.txt", fm, "Body")
        result = ep.check_processed_file(
            out_path,
            self.raw_root,
            {"Tech", "other"},
            {"concept", "other"},
        )
        self.assertTrue(any("mismatch" in e for e in result["errors"]))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_eval_pipeline.py::TestOutputHealth -v`

Expected: `AttributeError` for `load_allowed_categories` / `check_processed_file`.

---

## Task 4: Output health — implementation

**Files:**
- Modify: `scripts/eval_pipeline.py`

- [ ] **Step 1: Write minimal implementation**

Append to `scripts/eval_pipeline.py`:

```python
from collections import Counter

import yaml
from helpers import compute_file_hash, parse_allowed_values


REQUIRED_FRONTMATTER_KEYS = [
    "original_path",
    "file_hash",
    "subdomain",
    "document_type",
    "truthness_score",
    "truthness_justification",
    "language",
    "model",
]


def load_allowed_categories(instruction_path: Path) -> set[str]:
    return set(parse_allowed_values(instruction_path))


def _extract_frontmatter(out_path: Path) -> tuple[dict | None, str]:
    content = out_path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return None, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content
    try:
        fm = yaml.safe_load(parts[1])
    except Exception:
        return None, content
    return (fm if isinstance(fm, dict) else None), parts[2]


def check_processed_file(
    out_path: Path,
    raw_root: Path,
    allowed_subdomains: set[str],
    allowed_doc_types: set[str],
) -> dict:
    result = {"path": str(out_path), "errors": [], "warnings": []}
    fm, body = _extract_frontmatter(out_path)
    if fm is None:
        result["errors"].append("Missing or malformed YAML frontmatter")
        return result

    for key in REQUIRED_FRONTMATTER_KEYS:
        if key not in fm:
            result["errors"].append(f"Missing frontmatter key: {key}")

    if not body.strip():
        result["warnings"].append("Empty body")

    subdomain = fm.get("subdomain")
    if subdomain not in allowed_subdomains:
        result["errors"].append(f"Unknown subdomain: {subdomain}")

    doc_type = fm.get("document_type")
    if doc_type not in allowed_doc_types:
        result["errors"].append(f"Unknown document_type: {doc_type}")

    score = fm.get("truthness_score")
    if not isinstance(score, (int, float)) or score < 0 or score > 10:
        result["errors"].append(f"Invalid truthness_score: {score}")

    original_path = fm.get("original_path")
    file_hash = fm.get("file_hash")
    if original_path and file_hash:
        src = raw_root / original_path
        if not src.exists():
            result["errors"].append(f"original_path not found: {original_path}")
        elif compute_file_hash(src) != str(file_hash):
            result["errors"].append("file_hash mismatch (stale output)")

    return result


def check_output_health(
    raw_root: Path,
    processed_md_root: Path,
    db_path: Path,
    instructions_root: Path,
) -> dict:
    allowed_subdomains = load_allowed_categories(instructions_root / "subdomains.md")
    allowed_doc_types = load_allowed_categories(instructions_root / "document_types.md")
    statuses = get_file_statuses(db_path)
    processed_paths = [
        Path(fp) for fp, info in statuses.items() if info.get("status") == "processed"
    ]

    checked = []
    missing_outputs = []
    for raw_path in processed_paths:
        try:
            rel = raw_path.relative_to(raw_root.resolve())
        except ValueError:
            checked.append(
                {
                    "path": str(raw_path),
                    "errors": ["Raw path is not under raw_root"],
                    "warnings": [],
                }
            )
            continue
        out_path = processed_md_root / rel.with_suffix(".md")
        if not out_path.exists():
            missing_outputs.append(str(rel))
            continue
        checked.append(
            check_processed_file(
                out_path, raw_root.resolve(), allowed_subdomains, allowed_doc_types
            )
        )

    errors = missing_outputs + [
        f"{c['path']}: {e}" for c in checked for e in c["errors"]
    ]
    warnings = [f"{c['path']}: {w}" for c in checked for w in c["warnings"]]

    return {
        "name": "Output health",
        "ok": not errors,
        "critical": True,
        "details": {
            "processed_count": len(processed_paths),
            "missing_outputs": missing_outputs,
            "file_errors": checked,
        },
        "errors": errors,
        "warnings": warnings,
    }
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/test_eval_pipeline.py::TestOutputHealth -v`

Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add scripts/eval_pipeline.py tests/test_eval_pipeline.py
git commit -m "feat(eval): add output health checks

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Review queue & sampling — failing tests

**Files:**
- Modify: `tests/test_eval_pipeline.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eval_pipeline.py`:

```python
class TestReviewQueueAndSampling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "pipeline.db"
        conn = sqlite3.connect(str(self.db_path))
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_eval_pipeline.py::TestReviewQueueAndSampling -v`

Expected: `AttributeError` for `check_review_queue` / `sample_files`.

---

## Task 6: Review queue & sampling — implementation

**Files:**
- Modify: `scripts/eval_pipeline.py`

- [ ] **Step 1: Write minimal implementation**

Append to `scripts/eval_pipeline.py`:

```python
import random


def check_review_queue(db_path: Path, total_files: int, high_rate_threshold: float = 0.25) -> dict:
    conn = _db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT stage, trigger_type, status FROM review_queue")
        rows = cursor.fetchall()
    finally:
        conn.close()

    pending = [r for r in rows if r["status"] == "pending"]
    stale = [r for r in rows if r["status"] == "stale"]
    by_trigger = Counter((r["stage"], r["trigger_type"]) for r in pending)
    review_rate = len(pending) / total_files if total_files else 0.0
    high_triggers = [
        {"stage": s, "trigger": t, "count": c}
        for (s, t), c in by_trigger.items()
        if c / total_files > high_rate_threshold
    ] if total_files else []

    return {
        "name": "Review queue signal",
        "ok": review_rate <= high_rate_threshold,
        "critical": False,
        "details": {
            "pending_count": len(pending),
            "stale_count": len(stale),
            "review_rate": review_rate,
            "by_trigger": dict(by_trigger),
            "high_triggers": high_triggers,
        },
    }


def sample_files(processed_paths: list[Path], sample_size: int, seed: int | None = None) -> list[Path]:
    if seed is not None:
        random.seed(seed)
    if len(processed_paths) <= sample_size:
        return processed_paths
    return random.sample(processed_paths, sample_size)
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/test_eval_pipeline.py::TestReviewQueueAndSampling -v`

Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add scripts/eval_pipeline.py tests/test_eval_pipeline.py
git commit -m "feat(eval): add review queue and sampling checks

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Forensics — failing test

**Files:**
- Modify: `tests/test_eval_pipeline.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eval_pipeline.py`:

```python
class TestForensics(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.raw_root = self.root / "raw"
        self.processed_root = self.root / "processed_md"
        self.raw_root.mkdir()
        self.processed_root.mkdir()

        self.db_path = self.root / "pipeline.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript("""
            CREATE TABLE files (filepath TEXT PRIMARY KEY, file_hash TEXT, status TEXT);
            CREATE TABLE stage_outputs (
                file_hash TEXT,
                stage_name TEXT,
                output_text TEXT,
                model_name TEXT,
                instructions_hash TEXT
            );
        """)
        src = self.raw_root / "doc.txt"
        src.write_text("hello", encoding="utf-8")
        h = compute_file_hash(src)
        conn.execute(
            "INSERT INTO files (filepath, file_hash, status) VALUES (?, ?, ?)",
            (str(src), h, "processed"),
        )
        conn.executemany(
            "INSERT INTO stage_outputs (file_hash, stage_name, output_text) VALUES (?, ?, ?)",
            [
                (h, "docling", "extracted"),
                (h, "translation", "translated"),
                (h, "truthness", '{"score": 8}'),
            ],
        )
        conn.commit()
        conn.close()

        fm = {
            "original_path": "doc.txt",
            "file_hash": h,
            "subdomain": "Tech",
            "document_type": "concept",
            "truthness_score": 8,
            "truthness_justification": "ok",
            "language": "english (skipped translation)",
            "model": "llama3",
        }
        out = self.processed_root / "doc.md"
        out.write_text(
            f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n\nBody",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_forensics(self):
        result = ep.build_forensics(
            self.raw_root / "doc.txt",
            self.db_path,
            self.raw_root,
            self.processed_root,
        )
        self.assertEqual(result["stage_outputs"]["docling"]["output_text"], "extracted")
        self.assertEqual(result["processed_frontmatter"]["subdomain"], "Tech")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_eval_pipeline.py::TestForensics -v`

Expected: `AttributeError: module 'eval_pipeline' has no attribute 'build_forensics'`.

---

## Task 8: Forensics — implementation

**Files:**
- Modify: `scripts/eval_pipeline.py`

- [ ] **Step 1: Write minimal implementation**

Append to `scripts/eval_pipeline.py`:

```python
def get_file_hash(db_path: Path, filepath: Path) -> str | None:
    conn = _db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT file_hash FROM files WHERE filepath = ?", (str(filepath),))
        row = cursor.fetchone()
        return row["file_hash"] if row else None
    finally:
        conn.close()


def get_stage_outputs(db_path: Path, file_hash: str) -> dict[str, dict]:
    conn = _db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT stage_name, output_text, model_name, instructions_hash "
            "FROM stage_outputs WHERE file_hash = ?",
            (file_hash,),
        )
        return {row["stage_name"]: dict(row) for row in cursor.fetchall()}
    finally:
        conn.close()


def build_forensics(
    raw_path: Path,
    db_path: Path,
    raw_root: Path,
    processed_md_root: Path,
) -> dict:
    file_hash = get_file_hash(db_path, raw_path)
    rel_path = raw_path.relative_to(raw_root.resolve())
    out_path = processed_md_root / rel_path.with_suffix(".md")
    fm, _ = _extract_frontmatter(out_path) if out_path.exists() else (None, "")

    review_files = []
    review_dir = Path("review")
    if file_hash and review_dir.exists():
        short_hash = file_hash[:8]
        review_files = [str(p) for p in review_dir.glob(f"*{short_hash}*.md")]

    return {
        "raw_path": str(raw_path),
        "processed_path": str(out_path),
        "rel_path": str(rel_path.as_posix()),
        "current_hash": compute_file_hash(raw_path) if raw_path.exists() else None,
        "db_hash": file_hash,
        "stage_outputs": get_stage_outputs(db_path, file_hash) if file_hash else {},
        "processed_frontmatter": fm,
        "review_files": review_files,
    }
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_eval_pipeline.py::TestForensics -v`

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add scripts/eval_pipeline.py tests/test_eval_pipeline.py
git commit -m "feat(eval): add per-file forensic reconstruction

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Report writer & CLI — failing test

**Files:**
- Modify: `tests/test_eval_pipeline.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eval_pipeline.py`:

```python
class TestReportAndCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_report_creates_markdown(self):
        report = {
            "summary": {
                "total": 5,
                "critical_failures": 1,
                "warnings": 0,
                "review_rate": 0.2,
                "status_counts": {"processed": 4, "needs_review": 1},
            },
            "checks": [
                {"name": "Coverage", "ok": True, "detail": ""},
                {"name": "Output health", "ok": False, "detail": "1 error"},
            ],
            "review_breakdown": [],
            "samples": [],
            "forensics": [],
        }
        out_path = self.root / "report.md"
        ep.write_report(report, out_path)
        self.assertTrue(out_path.exists())
        content = out_path.read_text(encoding="utf-8")
        self.assertIn("Pipeline Real-Docs Evaluation Report", content)
        self.assertIn("Output health", content)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_eval_pipeline.py::TestReportAndCLI -v`

Expected: `AttributeError` for `write_report`.

---

## Task 10: Report writer & CLI — implementation

**Files:**
- Modify: `scripts/eval_pipeline.py`

- [ ] **Step 1: Write minimal implementation**

Replace the `main()` stub in `scripts/eval_pipeline.py` with the following functions and CLI:

```python
from datetime import datetime


def _status_table(statuses: dict[str, dict]) -> dict[str, int]:
    counts = Counter(s.get("status", "unknown") for s in statuses.values())
    return dict(counts)


def build_report(
    raw_root: Path,
    processed_md_root: Path,
    db_path: Path,
    instructions_root: Path,
    sample_size: int,
    seed: int | None,
) -> dict:
    raw_files = get_raw_files(raw_root)
    statuses = get_file_statuses(db_path)

    coverage = check_coverage(raw_files, statuses)
    output_health = check_output_health(
        raw_root, processed_md_root, db_path, instructions_root
    )
    review_queue = check_review_queue(db_path, total_files=len(raw_files))

    processed_paths = [
        Path(fp) for fp, info in statuses.items() if info.get("status") == "processed"
    ]
    samples = sample_files(processed_paths, sample_size, seed=seed)
    forensics = [
        build_forensics(p, db_path, raw_root, processed_md_root) for p in samples
    ]

    checks = [coverage, output_health, review_queue]
    critical_failures = sum(1 for c in checks if c.get("critical") and not c["ok"])
    warnings = sum(1 for c in checks if not c.get("critical") and not c["ok"])

    return {
        "summary": {
            "total": len(raw_files),
            "critical_failures": critical_failures,
            "warnings": warnings,
            "review_rate": review_queue["details"]["review_rate"],
            "status_counts": _status_table(statuses),
        },
        "checks": [
            {"name": c["name"], "ok": c["ok"], "detail": _check_detail(c)}
            for c in checks
        ],
        "review_breakdown": [
            {"stage": stage, "trigger": trigger, "count": count}
            for (stage, trigger), count in review_queue["details"]["by_trigger"].items()
        ],
        "samples": [{"raw": str(s)} for s in samples],
        "forensics": forensics,
    }


def _check_detail(check: dict) -> str:
    if check["ok"]:
        return "PASS"
    if check["name"] == "Coverage":
        d = check["details"]
        return f"missing={len(d['missing'])}, pending={d['pending_count']}, unknown={d['unknown_status_count']}"
    if check["name"] == "Output health":
        return f"errors={len(check.get('errors', []))}, warnings={len(check.get('warnings', []))}"
    if check["name"] == "Review queue signal":
        return f"rate={check['details']['review_rate']:.1%}, pending={check['details']['pending_count']}"
    return "FAIL"


def write_report(report: dict, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Pipeline Real-Docs Evaluation Report\n\n"]
    lines.append(f"Generated: {datetime.now().isoformat()}\n\n")

    summary = report["summary"]
    lines.append("## Summary\n\n")
    lines.append(f"- Total raw files: {summary['total']}\n")
    lines.append(f"- Critical failures: {summary['critical_failures']}\n")
    lines.append(f"- Warnings: {summary['warnings']}\n")
    lines.append(f"- Review rate: {summary['review_rate']:.1%}\n\n")

    lines.append("## Coverage\n\n")
    lines.append("| Status | Count |\n|---|---|\n")
    for status, count in sorted(summary["status_counts"].items()):
        lines.append(f"| {status} | {count} |\n")
    lines.append("\n")

    lines.append("## Checks\n\n")
    lines.append("| Check | Result | Detail |\n|---|---|---|\n")
    for c in report["checks"]:
        result = "PASS" if c["ok"] else "FAIL"
        lines.append(f"| {c['name']} | {result} | {c['detail']} |\n")
    lines.append("\n")

    lines.append("## Review queue breakdown\n\n")
    if report["review_breakdown"]:
        lines.append("| Stage | Trigger | Count |\n|---|---|---|\n")
        for item in report["review_breakdown"]:
            lines.append(f"| {item['stage']} | {item['trigger']} | {item['count']} |\n")
    else:
        lines.append("No pending review items.\n")
    lines.append("\n")

    lines.append("## Sample spot-check\n\n")
    if report["samples"]:
        for item in report["samples"]:
            lines.append(f"- `{item['raw']}`\n")
    else:
        lines.append("No processed files to sample.\n")
    lines.append("\n")

    lines.append("## Forensics\n\n")
    for f in report["forensics"]:
        lines.append(f"### `{f['rel_path']}`\n\n")
        lines.append(f"- Raw: `{f['raw_path']}`\n")
        lines.append(f"- Processed: `{f['processed_path']}`\n")
        lines.append(f"- DB hash: {f['db_hash']}\n")
        lines.append(f"- Current hash: {f['current_hash']}\n")
        lines.append("- Stage outputs:\n")
        for stage, data in f["stage_outputs"].items():
            preview = (data.get("output_text") or "")[:200].replace("\n", " ")
            lines.append(f"  - `{stage}`: {preview}\n")
        if f["review_files"]:
            lines.append("- Review files:\n")
            for rf in f["review_files"]:
                lines.append(f"  - `{rf}`\n")
        lines.append("\n")

    out_path.write_text("".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Evaluate the document pipeline on real docs.")
    parser.add_argument("--raw", type=Path, default=Path("raw"))
    parser.add_argument("--processed", type=Path, default=Path("processed_md"))
    parser.add_argument("--db", type=Path, default=Path("pipeline.db"))
    parser.add_argument("--instructions", type=Path, default=Path("instructions"))
    parser.add_argument("--report-dir", type=Path, default=Path("eval_reports"))
    parser.add_argument("--sample-size", type=int, default=5)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    report = build_report(
        args.raw,
        args.processed,
        args.db,
        args.instructions,
        args.sample_size,
        args.seed,
    )
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = args.report_dir / f"eval_report_{timestamp}.md"
    write_report(report, report_path)
    print(f"Report written to {report_path}")

    critical_failures = report["summary"]["critical_failures"]
    if critical_failures:
        print(f"CRITICAL FAILURES: {critical_failures}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/test_eval_pipeline.py::TestReportAndCLI -v`

Expected: PASS.

- [ ] **Step 3: Smoke test the CLI**

Run: `python scripts/eval_pipeline.py --help`

Expected: help output with all arguments listed.

- [ ] **Step 4: Commit**

```bash
git add scripts/eval_pipeline.py tests/test_eval_pipeline.py
git commit -m "feat(eval): add report writer and CLI

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 11: Watch pipeline — failing tests

**Files:**
- Create: `tests/test_watch_pipeline.py`

- [ ] **Step 1: Write the failing test**

```python
import sys
import tempfile
import unittest
import sqlite3
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent / "scripts"))
import watch_pipeline as wp


class TestWatchSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "pipeline.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript("""
            CREATE TABLE files (filepath TEXT PRIMARY KEY, file_hash TEXT, status TEXT);
        """)
        conn.executemany(
            "INSERT INTO files (filepath, status) VALUES (?, ?)",
            [
                ("/vault/raw/a.txt", "processed"),
                ("/vault/raw/b.txt", "error"),
                ("/vault/raw/c.txt", "pending"),
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_watch_pipeline.py -v`

Expected: `ModuleNotFoundError` or `AttributeError`.

---

## Task 12: Watch pipeline — implementation

**Files:**
- Create: `scripts/watch_pipeline.py`

- [ ] **Step 1: Write minimal implementation**

```python
import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table


def _db_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def get_summary(db_path: Path) -> dict:
    conn = _db_conn(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT status, COUNT(*) AS cnt FROM files GROUP BY status")
        counts = {row["status"]: row["cnt"] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT filepath, status, updated_at FROM files "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        last = cursor.fetchone()
    finally:
        conn.close()

    terminal = {"processed", "filtered", "error", "needs_review", "skipped"}
    total = sum(counts.values())
    done = sum(counts.get(s, 0) for s in terminal)
    pending = counts.get("pending", 0)
    return {
        "total": total,
        "done": done,
        "pending": pending,
        "processed": counts.get("processed", 0),
        "filtered": counts.get("filtered", 0),
        "error": counts.get("error", 0),
        "needs_review": counts.get("needs_review", 0),
        "skipped": counts.get("skipped", 0),
        "last_file": last["filepath"] if last else None,
        "last_status": last["status"] if last else None,
    }


def render_table(summary: dict, elapsed: timedelta) -> Table:
    table = Table(title="Pipeline Watch")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("Elapsed", str(elapsed).split(".")[0])
    table.add_row("Total files", str(summary["total"]))
    table.add_row("Pending", str(summary["pending"]))
    table.add_row("Processed", str(summary["processed"]))
    table.add_row("Filtered", str(summary["filtered"]))
    table.add_row("Errors", str(summary["error"]))
    table.add_row("Needs review", str(summary["needs_review"]))
    table.add_row("Skipped", str(summary["skipped"]))
    table.add_row("Done", f"{summary['done']} / {summary['total']}")
    if summary["last_file"]:
        table.add_row("Last updated", f"{Path(summary['last_file']).name} ({summary['last_status']})")
    if summary["done"] and summary["total"]:
        rate = summary["done"] / summary["total"]
        table.add_row("Progress", f"{rate:.1%}")
    return table


def watch(db_path: Path, interval: int = 5):
    console = Console()
    start = datetime.now()
    with Live(console=console, refresh_per_second=1) as live:
        while True:
            summary = get_summary(db_path)
            elapsed = datetime.now() - start
            live.update(render_table(summary, elapsed))
            if summary["pending"] == 0 and summary["total"] > 0:
                console.print("\n[green]All files reached a terminal state.[/green]")
                break
            time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="Live monitor for the document pipeline.")
    parser.add_argument("--db", type=Path, default=Path("pipeline.db"))
    parser.add_argument("--interval", type=int, default=5, help="Polling interval in seconds")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"Database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    try:
        watch(args.db, args.interval)
    except KeyboardInterrupt:
        print("\nWatcher stopped.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `python -m pytest tests/test_watch_pipeline.py -v`

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add scripts/watch_pipeline.py tests/test_watch_pipeline.py
git commit -m "feat(watch): add live pipeline monitor

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 13: Integration run and final validation

**Files:**
- None (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m unittest discover -s tests -v`

Expected: all existing tests and new tests PASS.

- [ ] **Step 2: Run a manual smoke test on the evaluator**

Create a tiny fixture set under a temp directory:

```bash
mkdir -p /tmp/eval-smoke/raw /tmp/eval-smoke/processed_md /tmp/eval-smoke/instructions /tmp/eval-smoke/review
printf "hello world" > /tmp/eval-smoke/raw/doc.txt
cat > /tmp/eval-smoke/instructions/subdomains.md <<'EOF'
# Subdomains

## Allowed Subdomains

1. **Tech**
   - Tech topics
2. **other**
   - Other topics
EOF
cat > /tmp/eval-smoke/instructions/document_types.md <<'EOF'
# Document Types

## Allowed Document Types

1. **concept**
   - concept
2. **other**
   - other
EOF
```

Seed the database manually (or run the real pipeline once if convenient). Then run:

```bash
python scripts/eval_pipeline.py --raw /tmp/eval-smoke/raw --processed /tmp/eval-smoke/processed_md --db /tmp/eval-smoke/pipeline.db --instructions /tmp/eval-smoke/instructions --sample-size 1
```

Expected: a report is written to `eval_reports/eval_report_*.md` and the script exits `0` if invariants pass or `1` if the fixture has issues.

- [ ] **Step 3: Run a manual smoke test on the watcher**

In one terminal, start a slow pipeline run (or leave a populated `pipeline.db` from the smoke test above). In another terminal:

```bash
python scripts/watch_pipeline.py --db pipeline.db --interval 2
```

Expected: a live table updates as the pipeline progresses.

- [ ] **Step 4: Commit final changes if any**

```bash
git add -A
git commit -m "test(eval): add integration smoke tests and final validation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-review

**1. Spec coverage**

| Spec requirement | Implementing task |
|------------------|-------------------|
| Coverage: every raw file in known terminal state | Task 2 |
| Output health: valid frontmatter, allowed categories, score range, non-empty body, hash match | Task 4 |
| Review queue signal: counts, rates, high-trigger flags | Task 6 |
| Sampling of processed files | Task 6 |
| Live monitoring while pipeline runs | Task 12 |
| Forensics: every stage version for sampled files | Task 8 |
| Markdown report with summary, checks, review breakdown, samples, forensics | Task 10 |
| Read-only, no pipeline modifications | All tasks (no writes to DB/processed_md/raw) |

**2. Placeholder scan**

No placeholders found. Every step includes exact file paths, function names, commands, and expected output.

**3. Type consistency**

- `check_coverage`, `check_output_health`, `check_review_queue` all return `dict` with keys `name`, `ok`, `critical`, `details`.
- File paths are passed as `pathlib.Path` internally and converted to strings only for DB keys or report output.
- `load_allowed_categories` returns `set[str]` consistently used by `check_processed_file` and `check_output_health`.
- `build_forensics` accepts a `Path` raw path and computes the processed path consistently with `check_output_health`.

No type/name mismatches detected.
